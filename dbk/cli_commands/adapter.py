"""IPC/HTTP adapter for DBK CLI commands.

Provides a lightweight HTTP interface that executes CLI commands and
returns CommandResult-serialised JSON.  This is the adapter layer
for future TUI or TypeScript frontends (plan.md 5.2e / 5.3).

Usage (server mode)
-------------------
    python -m dbk.cli_commands.adapter --port 7321

Usage (one-shot)
---------------
    from dbk.cli_commands.adapter import execute_command
    result = execute_command(["validate", "--format", "json"])
    print(result.as_dict())

JSON-RPC 2.0 interface
-----------------------
POST /rpc with {"method": "dbk.<subcommand>", "params": [...], "id": 1}
Response: {"id": 1, "result": <CommandResult dict>}

REST interface
--------------
POST /cmd/<subcommand> with JSON body (merged into args)
Response: CommandResult JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

from dbk.cli_commands.base import CommandResult


# ---------------------------------------------------------------------------
# Command executor (shared between REST and JSON-RPC)
# ---------------------------------------------------------------------------


def execute_command(argv: list[str]) -> CommandResult:
    """Execute a DBK CLI command with the given argument list.

    This is the core of the IPC adapter — it calls into the same
    parser/command infrastructure that the CLI uses, returning
    the raw CommandResult before formatting.

    Parameters
    ----------
    argv : list[str]
        E.g. ["validate"] or ["--format", "json", "config", "show"]

    Returns
    -------
    CommandResult
        The unformatted result.  Callers format as needed (text / json).
    """
    from dbk.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(argv)

    # args.func is the command handler set by set_defaults
    handler = getattr(args, "func", None)
    if handler is None:
        return CommandResult.usage_error("No handler for command")

    result = handler(args)

    # Support both CommandResult and legacy int returns
    if isinstance(result, CommandResult):
        return result
    if isinstance(result, int):
        from dbk.cli_commands.base import ExitCode
        return CommandResult(code=ExitCode(result))
    return CommandResult.ok(data=result)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _AdapterHandler(BaseHTTPRequestHandler):
    """HTTP handler for the command adapter.

    Supports two modes:
      POST /cmd/<subcommand>  — REST: pass JSON body as args
      POST /rpc               — JSON-RPC 2.0
      GET  /health            — health check
    """

    protocol_version = "HTTP/1.1"

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _command_result_response(self, result: CommandResult, fmt: str = "json") -> None:
        if fmt == "json" or fmt == "json-lines":
            self._json_response(result.as_dict())
        else:
            # text: just the message
            code = int(result.code)
            self.send_response(200 if code == 0 else code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(result.message.encode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response({"status": "ok", "adapter": "dbk-cli"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/rpc":
            self._handle_rpc()
        elif self.path.startswith("/cmd/"):
            self._handle_rest()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_rpc(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            request = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            self._json_response({"error": {"code": -32700, "message": f"Parse error: {exc}"}}, 400)
            return

        method: str = request.get("method", "")
        rpc_id: Any = request.get("id")
        params: list = request.get("params", [])

        if not method.startswith("dbk."):
            self._json_response(
                {"error": {"code": -32601, "message": f"Method not found: {method}"}, "id": rpc_id},
                200,
            )
            return

        subcommand = method[4:]  # strip "dbk."
        argv = [subcommand] + params
        result = execute_command(argv)
        self._json_response({"id": rpc_id, "result": result.as_dict()})

    def _handle_rest(self) -> None:
        subcommand = self.path[5:]  # strip "/cmd/"
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            extra = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            self._json_response({"error": {"code": -32700, "message": "Invalid JSON"}}, 400)
            return

        # Build argv: subcommand + flag-formatted extra args
        argv = [subcommand]
        for key, val in extra.items():
            if isinstance(val, bool):
                if val:
                    argv.append(f"--{key}")
            elif isinstance(val, (int, float, str)):
                argv.extend([f"--{key}", str(val)])
            else:
                argv.extend([f"--{key}", json.dumps(val)])

        result = execute_command(argv)
        fmt = extra.get("format", "json")
        self._command_result_response(result, fmt)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"[adapter] {format % args}\n")


def _run_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), _AdapterHandler)
    print(f"DBK CLI adapter listening on http://0.0.0.0:{port}", file=sys.stderr)
    print(f"  REST:  POST /cmd/<subcommand>  → CommandResult JSON", file=sys.stderr)
    print(f"  RPC:   POST /rpc              → JSON-RPC 2.0", file=sys.stderr)
    print(f"  Health: GET /health", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Adapter server stopped.", file=sys.stderr)
        server.shutdown()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dbk.adapter", description="DBK CLI IPC/HTTP adapter")
    parser.add_argument("--port", type=int, default=7321, help="Port to listen on (default: 7321)")
    args = parser.parse_args(argv)
    _run_server(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
