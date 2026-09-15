"""First-run application configuration bootstrap.

Creates a fresh, commented config.yaml in the application's writable data
root (see switchagent/paths.py) the first time SwitchAgent runs with none
present there yet -- e.g. right after a fresh install, or a first Portable
ZIP extraction. Never touches an existing config.yaml: an upgrade over a
previous install, or a user who already edited config.yaml, is always left
completely alone. This is a hard requirement, not a nicety -- silently
overwriting a user's configured library.source_dir on every app update
would be a real regression, not a packaging detail.

Deliberately does NOT hardcode a machine-specific download directory (this
project's own dev config.yaml's "D:\\shared\\Download" is this
developer's own real folder, not a sane default for a stranger's machine).
Instead tries to resolve the current user's actual Windows Downloads
folder (switchagent/known_folders.py) and only writes it in if that
resolves to a real, existing directory. If it cannot be resolved, the
written config simply omits library.source_dir -- config.load_library_dir()
falls back the same way at load time, and the Web UI's Settings page
surfaces the "not configured" state explicitly (see
switchagent/web/services.py's get_settings()) rather than silently
pointing at a directory nobody chose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from . import known_folders

_DEFAULT_MAX_EXTRACTED_SIZE_BYTES = 40_000_000_000
_DEFAULT_MAX_FILE_COUNT = 20_000
_DEFAULT_MAX_DIRECTORY_DEPTH = 24

_TEMPLATE = """\
# SwitchAgent configuration -- auto-generated on first run.
# Safe to edit; SwitchAgent never overwrites this file once it exists.

extraction:
  # Total uncompressed bytes an archive is allowed to produce in work/<job-id>/.
  max_extracted_size_bytes: {max_extracted_size_bytes}

  # Total number of files (not directories) an archive is allowed to contain.
  max_file_count: {max_file_count}

  # Maximum path depth (number of path components) for any single archive entry.
  max_directory_depth: {max_directory_depth}

library:
{library_section}
"""

_LIBRARY_SECTION_DETECTED = (
    "  # Auto-detected Windows Downloads folder.\n"
    '  source_dir: "{escaped_path}"\n'
)

_LIBRARY_SECTION_UNDETECTED = (
    "  # Could not auto-detect your Downloads folder. Set this to the folder\n"
    "  # where your Switch content (NSP/NSZ/XCI/XCZ/mods) lives, e.g.:\n"
    "  # source_dir: \"D:\\\\Games\\\\Switch\"\n"
)


def ensure_app_config(
    config_path: Path,
    *,
    downloads_resolver: Callable[[], Optional[Path]] = known_folders.downloads_dir,
) -> bool:
    """Writes a fresh config.yaml at `config_path` if (and only if) one
    does not already exist there. Returns True if a new file was written,
    False if an existing file was left untouched (the common case on
    every run after the very first)."""
    if config_path.exists():
        return False

    downloads = downloads_resolver()
    if downloads is not None:
        library_section = _LIBRARY_SECTION_DETECTED.format(escaped_path=_yaml_escape(str(downloads)))
    else:
        library_section = _LIBRARY_SECTION_UNDETECTED

    content = _TEMPLATE.format(
        max_extracted_size_bytes=_DEFAULT_MAX_EXTRACTED_SIZE_BYTES,
        max_file_count=_DEFAULT_MAX_FILE_COUNT,
        max_directory_depth=_DEFAULT_MAX_DIRECTORY_DEPTH,
        library_section=library_section,
    )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")
    return True


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
