"""Runtime path model: separates read-only bundled resources from writable,
persistent application data, and distinguishes three runtime modes.

Why this module exists: switchagent/config.py used to compute every path
(data/, work/, inbox/, the SQLite DB, config.yaml) relative to
`Path(__file__).resolve().parent.parent` -- i.e. wherever the switchagent
package itself lives. That is correct for a source checkout, but breaks
under a packaged build: a PyInstaller bundle's own directory may be
read-only (installed under Program Files-like locations) or is not the
right place for persistent user data to survive an application update
either way. This module is the one place that decides, for the current
process, where read-only resources live vs. where persistent data must be
written -- everything else (config.py, and through it the rest of the
package) builds on top of it rather than repeating the decision.

Three runtime modes:
  - "dev": running from a source checkout / editable install (the only
    mode that existed before this module) -- `sys.frozen` is not set.
  - "installed": a PyInstaller-frozen build with no `portable.flag` next
    to the executable -- persistent data goes to %LOCALAPPDATA%\\SwitchAgent.
  - "portable": a PyInstaller-frozen build with `portable.flag` present
    next to the executable -- persistent data stays next to the exe, so
    the whole directory tree remains self-contained and movable.

Read-only bundled resources (Jinja templates, static JS/CSS, package
metadata) are looked up through ordinary package-relative `Path(__file__)`
lookups wherever that already works (e.g. switchagent/web/app.py's
_WEB_DIR) -- those keep working unmodified inside a PyInstaller onedir
bundle without needing to know about `sys._MEIPASS` at all, and
deliberately are not routed through this module. `resource_root()` below
exists only for the rarer case of a genuinely top-level-relative bundled
resource lookup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PORTABLE_MARKER_NAME = "portable.flag"

APP_DIR_NAME = "SwitchAgent"


def is_frozen() -> bool:
    """True inside a PyInstaller-built executable (onedir or onefile),
    False for a normal `python -m switchagent` / installed-via-pip run."""
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    """Directory containing the running executable. Only meaningful when
    is_frozen() is True -- in dev mode sys.executable is the interpreter,
    not something callers of this function should treat as an app root."""
    return Path(sys.executable).resolve().parent


def _dev_project_root() -> Path:
    # switchagent/paths.py -> switchagent/ -> project root. Same
    # computation config.py's old PROJECT_ROOT used, kept identical so dev
    # mode's resolved paths do not change.
    return Path(__file__).resolve().parent.parent


def is_portable() -> bool:
    """True only for a frozen build with a `portable.flag` marker file
    next to the executable (see the Portable ZIP packaging step) -- never
    true in dev mode, and never guessed from anything other than that
    marker's presence."""
    if not is_frozen():
        return False
    return (executable_dir() / PORTABLE_MARKER_NAME).exists()


def runtime_mode() -> str:
    """One of "dev" / "installed" / "portable" -- see module docstring."""
    if not is_frozen():
        return "dev"
    return "portable" if is_portable() else "installed"


def resource_root() -> Path:
    """Read-only bundled resource root. PyInstaller (onedir or onefile)
    sets `sys._MEIPASS` to the bundle directory; every other mode uses the
    checked-out project root, exactly like the old config.PROJECT_ROOT."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", executable_dir()))
    return _dev_project_root()


def _local_appdata_dir() -> Path:
    """%LOCALAPPDATA% via the environment variable Windows itself
    populates -- never a hardcoded username-bearing path. Falls back to
    `~/AppData/Local` (still resolved through the current user's actual
    home directory, not a literal username) only if the environment
    variable is somehow unset."""
    value = os.environ.get("LOCALAPPDATA")
    if value:
        return Path(value)
    return Path.home() / "AppData" / "Local"


def app_data_root() -> Path:
    """Persistent, writable application data root -- the one path that
    must never live inside a PyInstaller bundle directory.

      - dev mode: the project root (unchanged from the pre-paths.py
        behavior -- data/, work/, inbox/ next to the checked-out code).
      - portable mode: next to the running executable.
      - installed mode: %LOCALAPPDATA%\\SwitchAgent.
    """
    mode = runtime_mode()
    if mode == "dev":
        return _dev_project_root()
    if mode == "portable":
        return executable_dir()
    return _local_appdata_dir() / APP_DIR_NAME
