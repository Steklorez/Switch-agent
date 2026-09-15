"""PyInstaller entry-point script -- imports and runs the desktop
launcher (switchagent/desktop.py). Kept separate from that module so it
stays a normal, importable package module (usable via
`python -m switchagent.desktop` or the `switch-agent-gui` console script)
with no PyInstaller-specific bootstrapping mixed into it.
"""

import sys

from switchagent.desktop import main

if __name__ == "__main__":
    sys.exit(main())
