"""`hermes tools` subcommand handler.

F-C1 step 3 — third subcommand extracted out of the 5,318-line
``hermes_cli/main.py``. The tools dispatcher is a small two-branch
router: the ``list / disable / enable`` actions go to the headless
CLI helper (``tools_disable_enable_command``), everything else goes
to the interactive TTY wizard (``tools_command``) and therefore
gates on ``_require_tty``.

Both targets live in ``hermes_cli.tools_config``. ``_require_tty``
remains in ``main.py`` as a shared helper; the extracted handler
imports it lazily so we don't reshape it under F-C1.
"""

from __future__ import annotations


def cmd_tools(args):
    """Tools management — list / enable / disable / interactive config."""
    action = getattr(args, "tools_action", None)
    if action in ("list", "disable", "enable"):
        from hermes_cli.tools_config import tools_disable_enable_command

        tools_disable_enable_command(args)
    else:
        from hermes_cli.main import _require_tty

        _require_tty("tools")
        from hermes_cli.tools_config import tools_command

        tools_command(args)
