"""Windows Known Folder resolution -- currently just the Downloads folder.

Used by first-run config bootstrap (switchagent/firstrun.py) and by
config.load_library_dir()'s fallback (switchagent/config.py) to pick a sane
initial `library.source_dir` without guessing a string path. The commonly
hardcoded "%USERPROFILE%\\Downloads" string silently breaks the moment a
user relocates their Downloads folder via Explorer's Properties dialog (a
real, supported Windows feature) -- SHGetKnownFolderPath(FOLDERID_Downloads)
reflects that relocation correctly.

Every function here is best-effort and MUST NOT raise: any failure (wrong
platform, missing API, no such folder) returns None, never an exception --
callers treat that as "could not be determined", never as an error.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Optional

# KNOWNFOLDERID for the Downloads folder -- stable across every Windows
# version that supports SHGetKnownFolderPath (Vista and later). See
# Microsoft's KNOWNFOLDERID enumeration docs.
_FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def downloads_dir() -> Optional[Path]:
    """The current user's real Downloads folder. Returns None on any
    non-Windows platform, any API failure, or if the resolved path does
    not actually exist as a directory (e.g. deleted, or on a disconnected
    network drive) -- never raises."""
    if sys.platform != "win32":
        return None
    try:
        path = _sh_get_known_folder_path(_FOLDERID_DOWNLOADS)
    except (OSError, ValueError):
        return None
    if path is None:
        return None
    return path if path.is_dir() else None


def _sh_get_known_folder_path(guid_str: str) -> Optional[Path]:
    """Raw SHGetKnownFolderPath call, isolated so downloads_dir() can be
    tested (via monkeypatching this function, or downloads_dir() itself)
    without a real Windows COM/shell environment. Not part of the public
    contract of this module -- downloads_dir() is."""
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = _GUID.from_buffer_copy(uuid.UUID(guid_str).bytes_le)
    buf = ctypes.c_wchar_p()
    hresult = ctypes.windll.shell32.SHGetKnownFolderPath(  # type: ignore[attr-defined]
        ctypes.byref(guid), 0, None, ctypes.byref(buf)
    )
    if hresult != 0 or not buf.value:
        return None
    try:
        return Path(buf.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(buf)  # type: ignore[attr-defined]
