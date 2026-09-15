"""W3-005: full application-state backup (creation side).

Pure library code -- no FastAPI/web imports, same layering convention as
switchagent/work_cleanup.py and switchagent/diagnostics.py. The HTTP-facing
wrapper lives in switchagent/web/backup_service.py; the restore/validation
counterpart lives in switchagent/restore.py.

WHAT A BACKUP CONTAINS (an allowlist, never a "copy the data dir and skip a
few things" denylist -- a denylist silently starts including whatever a
future feature drops into APP_DATA_ROOT):

  - `switchagent.db`  -- a CONSISTENT snapshot of the whole SQLite database
    (see _snapshot_database below). Deliberately the WHOLE database file's
    content, not a hand-picked subset of tables: `jobs`, `job_log`,
    `install_history` (including UI-003's user_verified_outcome/
    user_verified_at), `devices` (friendly names), `device_storage_mappings`
    (UI-007's manual overrides), `installation_batches` (UI-002),
    `library_items` and `inbox_items`. Omitting `library_items`/`inbox_items`
    would throw away the entire scanned library on restore -- the user would
    get their history back and their library silently emptied.
  - `config.yaml`     -- the user's own editable settings file (extraction
    limits, library.source_dir). Omitted from the archive entirely, rather
    than stored empty, if the user has none yet; restore then leaves the
    current one alone (see restore.py).
  - `manifest.json`   -- self-describing integrity metadata, see
    _build_manifest below. Restore refuses anything it cannot verify
    against this.

WHAT A BACKUP NEVER CONTAINS, and why it is a hard structural property
rather than a filter that could be forgotten: this module only ever writes
the two payload entries listed above, both from explicitly-passed paths. It
never walks APP_DATA_ROOT. So game payloads (NSP/NSZ/XCI/XCZ), work/'s
staged "frozen" content (potentially tens of GB -- see work_cleanup.py),
logs/, inbox/ contents, mock_switch/, and update_check_cache.json are all
excluded by construction, not by a rule.

CONSISTENCY (the reason this module exists at all rather than a two-line
`shutil.copy2(DB_PATH, ...)`): the live database runs in WAL mode (see
db.get_connection's `PRAGMA journal_mode=WAL`). Its bytes on disk at any
instant are NOT a usable database by themselves -- recent committed
transactions may live only in the `-wal` sidecar, and the `.db` file itself
may be mid-write. Copying those bytes produces a backup that is silently
stale or outright corrupt. This module instead uses SQLite's own online
backup API (sqlite3.Connection.backup), which takes a proper transactional
snapshot of a live WAL database into a single self-contained target file --
verified against this project's own sqlite build to exclude a concurrently
open, uncommitted write transaction's rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

# Bump ONLY on a breaking change to the archive layout. restore.py declares
# which versions it knows how to restore (see its
# SUPPORTED_BACKUP_FORMAT_VERSIONS) and refuses anything else outright
# rather than guessing at an unknown shape.
BACKUP_FORMAT_VERSION = 1

DB_ENTRY_NAME = "switchagent.db"
CONFIG_ENTRY_NAME = "config.yaml"
MANIFEST_ENTRY_NAME = "manifest.json"

_FILENAME_PREFIX = "SwitchAgent-Backup-"

# Every entry this module writes is stamped with a fixed regular-file mode
# instead of inheriting the source file's st_mode. Two reasons: the archive
# becomes byte-reproducible in this respect across dev machines/OSes, and
# restore.py's symlink/file-type validation then checks a value this module
# actually controls rather than whatever the host filesystem happened to
# report.
_REGULAR_FILE_EXTERNAL_ATTR = (0o100644) << 16

_HASH_CHUNK_BYTES = 1024 * 1024


class BackupError(Exception):
    """Backup could not be produced. Message is safe to show a user."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    filename: str
    size_bytes: int          # size of the .zip on disk
    manifest: dict
    created_at: str


def backup_filename(now: Optional[datetime] = None) -> str:
    """`SwitchAgent-Backup-{YYYY-MM-DD}.zip` -- the name the Web UI offers
    as the download filename (see web/backup_service.py). Date only, in
    UTC, matching `created_at`'s own timezone so the two can never disagree
    about which day a backup is from."""
    if now is None:
        now = datetime.now(timezone.utc)
    return f"{_FILENAME_PREFIX}{now.strftime('%Y-%m-%d')}.zip"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _app_version() -> str:
    # Read through the package module at CALL time, not bound at import
    # time -- `switchagent.__version__` is resolved from installed package
    # metadata (see switchagent/__init__.py) and a test may legitimately
    # override it.
    from . import __version__

    return __version__


def _snapshot_database(db_path: Path, target_path: Path) -> None:
    """A consistent point-in-time copy of a LIVE, WAL-mode SQLite database
    into a single self-contained file at `target_path`.

    Uses sqlite3.Connection.backup() -- SQLite's own online backup API --
    rather than copying the `.db` bytes. See this module's docstring for
    why the raw bytes are not a valid backup.

    The source connection is deliberately a FRESH, dedicated one rather
    than any connection the application already holds: calling .backup() on
    a connection that itself has an open write transaction blocks
    indefinitely (confirmed against this project's own sqlite build),
    whereas a separate reader snapshots a live WAL database cleanly and
    concurrently -- readers are not blocked by a writer in WAL mode, and
    the snapshot correctly contains only COMMITTED data."""
    if not db_path.exists():
        raise BackupError(f"database not found: {db_path}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        source = sqlite3.connect(db_path, timeout=30)
    except sqlite3.Error as exc:  # pragma: no cover -- defensive
        raise BackupError(f"could not open the database: {exc}") from exc
    try:
        target = sqlite3.connect(target_path, timeout=30)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise BackupError(f"database snapshot failed: {exc}") from exc
    finally:
        source.close()


def _build_manifest(entries: list[tuple[str, Path]], *, created_at: str) -> dict:
    """Self-describing integrity metadata. `files` is what restore.py
    validates EVERY archive entry against -- both directions: no listed
    file may be missing, and no archive entry may exist that is not listed
    here (see restore.validate_archive)."""
    files = []
    total = 0
    for name, path in entries:
        size = path.stat().st_size
        total += size
        files.append({"name": name, "size": size, "sha256": _sha256_file(path)})
    return {
        "backup_format_version": BACKUP_FORMAT_VERSION,
        "app_version": _app_version(),
        "created_at": created_at,
        "files": files,
        # Total UNCOMPRESSED payload size (the sum of `files` sizes above),
        # not the size of the .zip -- the zip's own compressed size is a
        # property of the container, visible from the filesystem, and would
        # be circular to store inside the container itself.
        "size": total,
    }


def create_backup(
    destination: Path,
    *,
    db_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> BackupResult:
    """Produce a complete, self-verifying backup archive at `destination`.

    `db_path`/`config_path` default to the live application's own
    (config.DB_PATH / config.CONFIG_YAML_PATH) -- resolved at CALL time,
    never baked into a parameter default, so they correctly follow
    APP_DATA_ROOT in every runtime mode (dev/installed/portable, see
    switchagent/paths.py) and so tests can monkeypatch them the same way
    every other feature's tests already do.

    The archive is assembled in a staging directory next to `destination`
    and only moved into place once it is complete -- a caller (or a failed
    run) never sees a half-written .zip at the destination path.
    """
    if db_path is None:
        db_path = config.DB_PATH
    if config_path is None:
        config_path = config.CONFIG_YAML_PATH
    if now is None:
        now = datetime.now(timezone.utc)

    created_at = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix="switchagent-backup-", dir=str(destination.parent)))
    try:
        snapshot_path = staging / DB_ENTRY_NAME
        _snapshot_database(db_path, snapshot_path)

        entries: list[tuple[str, Path]] = [(DB_ENTRY_NAME, snapshot_path)]

        # A user who has never had a config.yaml written yet (see
        # firstrun.ensure_app_config) simply has no config entry -- never a
        # zero-byte placeholder, which restore would then happily write
        # over a perfectly good config.yaml on the target machine.
        if config_path.exists():
            staged_config = staging / CONFIG_ENTRY_NAME
            shutil.copy2(config_path, staged_config)
            entries.append((CONFIG_ENTRY_NAME, staged_config))

        manifest = _build_manifest(entries, created_at=created_at)

        staged_zip = staging / "archive.zip"
        with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in entries:
                _write_entry(archive, name, path.read_bytes(), now=now)
            _write_entry(
                archive, MANIFEST_ENTRY_NAME,
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"), now=now,
            )

        # os.replace is atomic within a filesystem, and `staging` was
        # created inside destination.parent precisely so this never crosses
        # one.
        os.replace(staged_zip, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return BackupResult(
        path=destination,
        filename=backup_filename(now),
        size_bytes=destination.stat().st_size,
        manifest=manifest,
        created_at=created_at,
    )


def _write_entry(archive: zipfile.ZipFile, name: str, payload: bytes, *, now: datetime) -> None:
    info = zipfile.ZipInfo(name, date_time=now.timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = _REGULAR_FILE_EXTERNAL_ATTR
    archive.writestr(info, payload)
