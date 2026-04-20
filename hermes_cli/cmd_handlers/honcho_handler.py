"""`hermes honcho` subcommand handler.

F-C1 step 4 — fourth subcommand extracted out of the 5,318-line
``hermes_cli/main.py``. Honcho dispatch is a one-line pass-through
to ``honcho_integration.cli.honcho_command``; the argparse block in
main.py still owns subparser shape, this module only owns the
dispatcher function.
"""

from __future__ import annotations


def cmd_honcho(args):
    """Hermes Honcho bridge — delegates to honcho_integration.cli."""
    from honcho_integration.cli import honcho_command

    honcho_command(args)
