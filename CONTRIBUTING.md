# Contributing to dbk

Thank you for your interest in contributing to dbk!

## Development Setup

### Prerequisites

- Python 3.10 or later
- `pip` or `pipx`

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/your-org/dbk.git
   cd dbk
   ```

2. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install the package with dev dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

4. Install pre-commit hooks (recommended):
   ```bash
   pip install pre-commit
   pre-commit install
   ```

## Running Tests

Run the full test suite:
```bash
pytest -q
```

Run tests with verbose output:
```bash
pytest -v
```

Run a specific test file:
```bash
pytest tests/test_agent_tools.py
```

Run tests matching a pattern:
```bash
pytest -k "test_alert"
```

## Code Style

We use `ruff` for formatting and linting. All code must pass ruff checks before submitting a pull request.

Format and lint:
```bash
ruff format .
ruff check .
```

Ruff is configured in `pyproject.toml`. Key settings:
- Line length: 100
- Target Python: 3.10+

## Type Checking

We use `mypy` for static type analysis. Run it on the `dbk/` package:
```bash
mypy dbk/
```

Ensure all new code includes type annotations. The project targets Python 3.10+ compatibility.

## Adding Tools and Providers

### Adding a New Tool

1. Create a new file in `dbk/agent/tools/` or add to an existing module.
2. Subclass `dbk.agent.tools.BaseTool` (or appropriate base class).
3. Implement the required methods (`run`, `name`, `description`).
4. Add type annotations to all parameters and return values.
5. Register the tool in the agent's `ToolRegistry` (typically in `dbk/agent/core.py` or via the plugin system).
6. Add tests in `tests/`.

### Adding a New LLM Provider

1. Create a new file in `dbk/providers/` (e.g., `dbk/providers/google.py`).
2. Subclass `dbk.providers.base.BaseProvider`.
3. Implement required methods (`chat`, `chat_stream`, `name`).
4. Add type annotations.
5. Update `DBK_PROVIDER` environment variable handling in `dbk/providers/__init__.py` or `dbk/config.py`.
6. Add tests in `tests/`.

### Adding a New Plugin Hook

1. Add the hook method to `dbk/plugins.py` under `PluginABC`.
2. Document the hook in `doc/PLUGIN_SYSTEM.md`.
3. Update `CONTRIBUTING.md` if the hook has contributor-facing implications.
4. Add tests.

## Submitting Changes

1. Fork the repository and create a branch from `main`.
2. Run `ruff format . && ruff check . && mypy dbk/ && pytest -q` locally.
3. Ensure all new code has type annotations and tests.
4. Submit a pull request against `main`.
5. Address any review comments.

## Code of Conduct

Please be respectful and constructive in all interactions.
