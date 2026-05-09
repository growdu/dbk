"""DBK CLI commands — extracted from cli.py into domain-organized modules.

Structure
---------
commands/
  __init__.py       — registers all commands with the parser builder
  base.py           — Command / CommandGroup base classes
  init_.py          — 'dbk init' and 'dbk validate'
  config.py         — 'dbk config' group  (show / init / get / set)
  collect.py        — 'dbk collect' group (health / daemon start|stop|status|list|run)
  runtime.py        — 'dbk runtime' group (cleanup / cleanup-daemon / report)
                      also 'dbk metrics' and 'dbk trace'
  diagnose.py       — 'dbk diagnose latency'
  alert.py          — 'dbk alert' group (rules / daemon / history / prometheus)
  run.py            — 'dbk run'
  api_server.py     — 'dbk api-server'

Adding a new command
--------------------
1. Create commands/<name>.py with a class implementing Command.configure() + execute().
2. Import and add to the list in register_all().
3. build_parser() in cli.py calls register_all() automatically.

This keeps cli.py focused on parser construction without embedding
command logic, and makes each command independently testable.
"""
from __future__ import annotations

import argparse
from typing import Callable

# Import each command module to register its class.
from . import init_      # noqa: F401
from . import config     # noqa: F401
from . import collect    # noqa: F401
from . import runtime    # noqa: F401
from . import diagnose   # noqa: F401
from . import alert      # noqa: F401
from . import run        # noqa: F401
from . import api_server # noqa: F401


def register_all(subparsers) -> None:
    """Register all CLI commands with the root subparsers.

    Called once from cli.build_parser() to attach every command group.
    """
    init_.InitCommand().configure(subparsers)
    config.ValidateCommand().configure(subparsers)
    config.ConfigCommand().configure(subparsers)
    collect.CollectCommand().configure(subparsers)
    runtime.RuntimeCommand().configure(subparsers)
    diagnose.DiagnoseCommand().configure(subparsers)
    alert.AlertCommand().configure(subparsers)
    run.RunCommand().configure(subparsers)
    api_server.APIServerCommand().configure(subparsers)