"""SwitchAgent -- local Switch MTP file manager for DBI.

`__version__` is the single source of truth for the application's
version, everywhere it's displayed (`switch-agent --version`, the Web UI's
Settings page, `/api/health`, the packaged EXE's version resource, and the
installer/release filenames) -- see docs/ARCHITECTURE.md and
pyproject.toml's `[project] version`, which is what actually defines it.
Read via `importlib.metadata` (the standard way to get a package's own
installed version without hand-maintaining a second copy of the number
anywhere in source) rather than a second hardcoded constant. Falls back to
a clearly-marked placeholder only if metadata is genuinely unavailable
(e.g. running from a source checkout that was never `pip install`-ed, not
even with `-e .`) -- never raises just because this optional lookup
failed.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("switchagent")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
