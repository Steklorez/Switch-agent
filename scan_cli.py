"""Backwards-compatible shim for the stage-3 CLI entry point.

    python scan_cli.py            -> same as `switch-agent scan`
    python scan_cli.py --watch    -> same as `switch-agent scan --watch`

New commands (preview, etc.) live in switchagent/cli.py -- see
    python scan_cli.py preview <path>
or, once installed (`pip install -e .`), the `switch-agent` command itself.
"""

from switchagent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
