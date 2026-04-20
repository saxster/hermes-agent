"""`hermes skills` subcommand handler.

F-C1 step 2 — second subcommand extracted out of the 5,318-line
``hermes_cli/main.py``. Skills dispatch is a small two-branch
router: the ``config`` action goes to ``hermes_cli.skills_config``
(interactive TTY wizard), everything else goes to
``hermes_cli.skills_hub`` (search / browse / install).

The handler used to live as a nested ``def`` inside main.py's
argparse-wiring block; pulling it to module scope matches the
pattern set by F-C1 step 1 (``profile_handler.py``) and keeps
main.py as entrypoint + dispatch table only.

The shared helper ``_require_tty`` remains in main.py — it is used
by many handlers, so F-C1 leaves it where it is and imports it.
"""

from __future__ import annotations


def cmd_skills(args):
    """Skills management — search / browse / install / enable / disable."""
    # Route 'config' action to skills_config module
    if getattr(args, "skills_action", None) == "config":
        from hermes_cli.main import _require_tty

        _require_tty("skills config")
        from hermes_cli.skills_config import skills_command as skills_config_command

        skills_config_command(args)
    else:
        from hermes_cli.skills_hub import skills_command

        skills_command(args)
