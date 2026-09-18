"""Central paths and constants for SwitchAgent. No logic here beyond the
config.yaml loaders below (unchanged in spirit from before switchagent/paths.py
existed -- only where the paths come from has changed).

Every path constant below is built on top of switchagent/paths.py's
dev/installed/portable runtime-mode model rather than assuming "wherever
this package lives" is also "where persistent data should be written" --
see that module's docstring for why. In dev mode every value here resolves
identically to before paths.py existed (PROJECT_ROOT == the checked-out
project root), so existing development workflows and tests (which
monkeypatch these as plain module attributes -- see tests/conftest.py) are
unaffected.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import known_folders, paths

# Kept as a module-level name for backward compatibility -- historically
# "the project root", now more precisely "the read-only resource root"
# (see paths.resource_root()'s docstring). Nothing in this codebase writes
# under PROJECT_ROOT anymore; see APP_DATA_ROOT below for that.
PROJECT_ROOT = paths.resource_root()

# The writable, persistent application data root -- %LOCALAPPDATA%\SwitchAgent
# when installed, next to the executable when portable, the project root
# in dev mode (unchanged behavior). Every path below that must survive a
# process restart and an application upgrade is anchored here, never under
# PROJECT_ROOT/a PyInstaller bundle directory.
APP_DATA_ROOT = paths.app_data_root()

RUNTIME_MODE = paths.runtime_mode()  # "dev" | "installed" | "portable"

INBOX_DIR = APP_DATA_ROOT / "inbox"
DATA_DIR = APP_DATA_ROOT / "data"
WORK_DIR = APP_DATA_ROOT / "work"  # safe-extraction scratch dir, one subfolder per job-id
LOGS_DIR = APP_DATA_ROOT / "logs"
MOCK_SWITCH_DIR = APP_DATA_ROOT / "mock_switch"
MOCK_SD_CARD_DIR = MOCK_SWITCH_DIR / "SD Card"

CONFIG_YAML_PATH = APP_DATA_ROOT / "config.yaml"

DB_PATH = DATA_DIR / "switchagent.db"

# Mock mode's OWN database, never the one above. Real incident (2026-09-17):
# a freshly built dist\SwitchAgent\SwitchAgent.exe was launched by hand in
# mock mode to eyeball the UI. dist/ carries no portable.flag, so
# paths.runtime_mode() correctly reported "installed" and APP_DATA_ROOT
# resolved to the REAL %LOCALAPPDATA%\SwitchAgent -- the same database the
# user's actual installation uses. web/context.py's build_mock_context()
# then registered its two fixture devices there, and "Parent's Switch
# (mock)" / "Child's Switch (mock)" stayed in the user's Devices page for
# good (nothing in the app can forget a device it has seen... see
# services.forget_device(), added after this). Isolating mock mode by
# FILENAME rather than by refusing to start is deliberate: the smoke tests
# (packaging/smoke_test.py) legitimately run a frozen, installed-mode build
# and isolate themselves by overriding %LOCALAPPDATA% instead, so a
# runtime_mode()-based refusal would break them while still leaving a
# hand-started dist/ build free to do exactly what happened here.
MOCK_DB_PATH = DATA_DIR / "switchagent-mock.db"


def use_mock_database() -> None:
    """Redirect every later `config.DB_PATH` reader to MOCK_DB_PATH -- call
    this at an entrypoint the moment mock mode is decided, BEFORE anything
    opens a connection. Reassigning the module attribute (rather than
    threading a path through call after call) is what makes the redirect
    total: db.get_connection(), backup.py and restore.py all deliberately
    re-read config.DB_PATH at CALL time precisely so an override like this
    one takes effect everywhere at once -- see get_connection()'s own note
    on why it is not a parameter default."""
    global DB_PATH
    DB_PATH = MOCK_DB_PATH


# Recognized installable package types (Atmosphere/DBI side)
PACKAGE_EXTENSIONS = {".nsp", ".nsz", ".xci", ".xcz"}

# Archives we may need to peek into
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}

ALL_TRACKED_EXTENSIONS = PACKAGE_EXTENSIONS | ARCHIVE_EXTENSIONS

ATMOSPHERE_LEAF_NAMES = {"romfs", "exefs", "exefs_patches", "cheats"}


def _fallback_library_dir() -> Path:
    """Inert placeholder used as LIBRARY_DIR's import-time default, before
    any config.yaml has been loaded. Deliberately NOT a live Windows API
    call (that happens only inside load_library_dir()/library_dir_info(),
    when explicitly invoked -- see below) and deliberately NOT a
    hardcoded, machine-specific path like this project's own dev
    config.yaml's "D:\\shared\\Download" (that value lives in config.yaml,
    not in code, and is never treated as a universal default). This
    placeholder doesn't exist yet, which every consumer of LIBRARY_DIR
    already tolerates gracefully (scan_library_once(),
    start_library_watcher())."""
    return APP_DATA_ROOT / "Library"


# Mutable module attribute so tests can monkeypatch it exactly like
# INBOX_DIR/WORK_DIR above -- see tests/conftest.py's isolated_db fixture
# for the established pattern. Resolved at import time to the inert
# fallback above; the CLI/web entrypoint re-resolves it from config.yaml
# on startup via load_library_dir(), so a config.yaml edit takes effect on
# next run without needing a code change.
LIBRARY_DIR = _fallback_library_dir()


def library_dirs() -> tuple[Path, ...]:
    """Current primary folder plus configured additional folders."""
    import yaml
    data = yaml.safe_load(CONFIG_YAML_PATH.read_text(encoding="utf-8")) if CONFIG_YAML_PATH.exists() else {}
    extra = (data or {}).get("library", {}).get("source_dirs", []) or []
    return tuple(dict.fromkeys([LIBRARY_DIR, *(Path(p) for p in extra)]))


def set_library_source_dirs(new_paths: list[Path]) -> None:
    """Preserve unrelated config lines and the legacy primary-folder key."""
    import json
    import re
    set_library_source_dir(new_paths[0], CONFIG_YAML_PATH)
    lines = CONFIG_YAML_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if re.match(r"^library:\s*$", line))
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[^\s#]", lines[i])), len(lines))
    section = "".join(lines[start + 1:end])
    section = re.sub(r"(?m)^  source_dirs:[^\n]*\n(?:[ \t]+-[^\n]*\n)*", "", section)
    line = "  source_dirs: " + json.dumps([str(p) for p in new_paths], ensure_ascii=False) + "\n"
    lines[start + 1:end] = [line, section]
    CONFIG_YAML_PATH.write_text("".join(lines), encoding="utf-8")

# A file is considered "still being written" if we can't open it exclusively,
# or if its size changed between two checks spaced this far apart.
STABLE_CHECK_INTERVAL_SECONDS = 2.0

TITLE_ID_RE = r"[0-9A-Fa-f]{16}"


@dataclass(frozen=True)
class ExtractionLimits:
    max_extracted_size_bytes: int
    max_file_count: int
    max_directory_depth: int


_DEFAULT_EXTRACTION_LIMITS = ExtractionLimits(
    max_extracted_size_bytes=40_000_000_000,
    max_file_count=20_000,
    max_directory_depth=24,
)


def load_extraction_limits(yaml_path: Path = CONFIG_YAML_PATH) -> ExtractionLimits:
    """Reads the `extraction:` section of config.yaml. Falls back to safe
    built-in defaults if the file or the section is missing, rather than
    crashing -- this is a limits config, not a required file."""
    if not yaml_path.exists():
        return _DEFAULT_EXTRACTION_LIMITS

    import yaml

    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    section = data.get("extraction", {})
    return ExtractionLimits(
        max_extracted_size_bytes=int(section.get(
            "max_extracted_size_bytes", _DEFAULT_EXTRACTION_LIMITS.max_extracted_size_bytes
        )),
        max_file_count=int(section.get(
            "max_file_count", _DEFAULT_EXTRACTION_LIMITS.max_file_count
        )),
        max_directory_depth=int(section.get(
            "max_directory_depth", _DEFAULT_EXTRACTION_LIMITS.max_directory_depth
        )),
    )


@dataclass(frozen=True)
class LibraryDirInfo:
    path: Path
    configured: bool  # True if config.yaml explicitly declares library.source_dir
    exists: bool


def library_dir_info(yaml_path: Path = CONFIG_YAML_PATH) -> "LibraryDirInfo":
    """Richer counterpart to load_library_dir(), for the Settings page
    (see web/services.py's get_settings()) -- distinguishes "user
    explicitly configured this in config.yaml" from "auto-detected/
    fallback", so the UI can show an honest state instead of silently
    rendering a directory nobody actually chose (Packaging Stage 2's
    showing the user an understandable state" requirement)."""
    configured_value: Optional[str] = None
    if yaml_path.exists():
        import yaml

        with yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        configured_value = data.get("library", {}).get("source_dir")

    if configured_value:
        path = Path(configured_value)
    else:
        path = known_folders.downloads_dir() or _fallback_library_dir()

    return LibraryDirInfo(path=path, configured=bool(configured_value), exists=path.is_dir())


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def set_library_source_dir(new_path: Path, yaml_path: Path = CONFIG_YAML_PATH) -> None:
    """W3-002: persists a new `library.source_dir` into config.yaml IN
    PLACE, preserving every other line (comments, extraction limits, any
    other section) byte-for-byte -- a full YAML parse+re-dump would strip
    user comments, which switchagent/firstrun.py's own docstring already
    treats as unacceptable for this specific, user-editable file
    ("SwitchAgent never overwrites this file once it exists" beyond the
    one value this function is explicitly asked to change). If
    config.yaml doesn't exist yet, creates one first via the existing
    firstrun machinery (never invents a second config-writing path)."""
    if not yaml_path.exists():
        from . import firstrun

        firstrun.ensure_app_config(yaml_path, downloads_resolver=lambda: None)

    import re

    text = yaml_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    library_line_idx: Optional[int] = None
    source_dir_line_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if re.match(r"^library:\s*$", line):
            library_line_idx = i
            continue
        if library_line_idx is not None and source_dir_line_idx is None:
            if re.match(r"^\s+source_dir:\s*.*$", line):
                source_dir_line_idx = i
            elif re.match(r"^\S", line):  # dedented -- left the library: section
                break

    new_line = f'  source_dir: "{_yaml_escape(str(new_path))}"\n'
    if source_dir_line_idx is not None:
        lines[source_dir_line_idx] = new_line
    elif library_line_idx is not None:
        lines.insert(library_line_idx + 1, new_line)
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("\nlibrary:\n")
        lines.append(new_line)

    yaml_path.write_text("".join(lines), encoding="utf-8")


def load_library_dir(yaml_path: Path = CONFIG_YAML_PATH) -> Path:
    """Reads config.yaml's `library: source_dir:` (Web UI spec point 43 --
    "make the source directory configurable through the existing config
    mechanism") -- an explicit value here always wins. Falls back to the
    current user's real Windows Downloads folder
    (known_folders.downloads_dir(), which reflects a folder the user
    relocated via Explorer -- unlike a guessed "%USERPROFILE%\\Downloads"
    string) if config.yaml or the library section/source_dir key is
    missing, and to an inert, doesn't-exist-yet placeholder under
    APP_DATA_ROOT if even that cannot be determined -- never a hardcoded,
    machine-specific path baked into the code. In the packaged app,
    switchagent/firstrun.py normally writes an explicit source_dir into a
    brand-new config.yaml on first run, before this fallback would ever be
    reached at all."""
    return library_dir_info(yaml_path).path
