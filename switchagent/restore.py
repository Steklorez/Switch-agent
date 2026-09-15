"""W3-005: restore from a backup archive -- validation + the runtime-safe
replace sequence.

Pure library code (no FastAPI/web imports), same layering convention as
switchagent/work_cleanup.py and switchagent/diagnostics.py. The HTTP-facing
wrapper is switchagent/web/backup_service.py; the creation side is
switchagent/backup.py.

This module is the destructive half of the feature, so its structure is
built around one rule:

    NOTHING about the current application state is touched until a
    candidate archive has been fully validated, in a temp directory, to
    completion.

Everything before `_atomic_replace_state` is read-only with respect to the
live state. If ANY of it fails -- a corrupt zip, a bad checksum, an entry
that is not in the archive's own manifest, a database that is not a
structurally sane SQLite file, a migration that will not run -- the live
DB/config are byte-for-byte untouched and the application stays fully
usable. If something fails AFTER the replace, the emergency backup taken in
step 4 is rolled back in.

THE TEN-STEP SEQUENCE (restore_backup below implements exactly this order):

  1. Refuse outright if any job is RUNNING -- that is a physical MTP
     transfer in flight. Swapping the database out from under
     queue_worker mid-transfer would orphan a job row that a real device
     is still receiving bytes for. Clear error, zero side effects.
  2. Pause the worker, through the SAME `worker_paused` Event that
     POST /api/worker/pause already sets (see web/context.py) -- never a
     second, parallel pause mechanism.
  3. Stop the library watcher (the existing stop_library_watcher()).
  4. Emergency backup of the CURRENT state, via backup.create_backup() --
     the same function the user-facing download uses, never a second
     backup implementation. Kept on disk after a successful restore (see
     `emergency_backup_dir`): it is the user's only way back to the state
     they just replaced.
  5. Extract the candidate into a temp directory -- never over live state.
  6. Fully validate it there (see validate_archive).
  7. Run the CURRENT db.init_db() against the candidate database, so an
     older backup's schema is properly migrated forward rather than
     silently used stale, and so a candidate whose schema cannot be
     migrated is rejected while the live state is still untouched.
  8. Atomically replace DB + config.yaml (sibling temp path + os.replace).
  9. Reopen the DB connection the running app holds.
 10. Restart watcher + worker, and lift the pause.

WHY STEP 8 NEEDS THE WORKER THREAD ACTUALLY STOPPED, not just paused:
on Windows, os.replace() over a file that another handle has open fails
with PermissionError/ERROR_ACCESS_DENIED -- verified against this project's
own environment. WebContext's worker thread holds an open sqlite3
connection for its whole lifetime, and `worker_paused` does not close it
(see _worker_loop: a paused worker just waits, connection still open). So
the runtime adapter's `quiesce_worker` hook stops that thread (releasing
its connection via _worker_loop's own `finally: conn.close()`) immediately
before the replace, and `start_worker` brings it back afterwards -- which
is also exactly what step 9 ("reopen the DB connection the running app
holds") means in this application's architecture. Both are existing
WebContext methods; nothing new is invented here.

WHY THE `-wal`/`-shm` SIDECARS ARE DELETED AFTER THE REPLACE: the live
database runs in WAL mode, so `switchagent.db-wal` may hold committed
transactions belonging to the OLD database. Leaving it next to a freshly
restored `switchagent.db` invites SQLite to apply a stale write-ahead log
to an unrelated database -- data corruption, not a cosmetic leftover. The
restored candidate is normalised to a single self-contained file (see
_prepare_candidate_db) precisely so it needs no sidecar at all.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import backup as backup_mod, config, db

# Archive layouts this module knows how to restore. An archive declaring
# anything else is refused outright rather than guessed at -- a future
# format-2 archive restored by format-1 logic is exactly the kind of silent
# data loss this feature exists to prevent.
SUPPORTED_BACKUP_FORMAT_VERSIONS = (1,)

# Tables every restorable database must already contain BEFORE init_db()
# gets a chance to create anything (step 6 runs before step 7 on purpose:
# a file that is technically openable SQLite but is not a SwitchAgent
# database -- someone's unrelated .db -- must be rejected, not silently
# "migrated" into one by executing the schema script against it).
#
# Deliberately the FOUNDATIONAL tables only. `installation_batches` and
# `device_storage_mappings` are genuinely newer (UI-002/UI-007) and are
# created by init_db()'s own schema script in step 7 anyway; requiring them
# here would reject an older-but-perfectly-restorable backup for no safety
# gain. Kept as an explicit local contract rather than importing
# diagnostics._EXPECTED_TABLES: that one answers "is this schema current?",
# this one answers "is this file a SwitchAgent database at all?", and the
# two must be free to diverge.
REQUIRED_CORE_TABLES = (
    "inbox_items", "jobs", "job_log", "library_items", "devices", "install_history",
)

# Structural sanity caps on an untrusted archive. Our own backups have
# exactly three entries; these exist so a hostile/corrupt zip cannot make
# validation itself the attack (decompression bomb, entry-count blowup)
# before any of the content checks even run.
MAX_ARCHIVE_ENTRIES = 64
MAX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024

# Extensions that must never appear inside a backup of this application's
# state, whatever the archive's own manifest claims. A SwitchAgent backup
# contains a database, a YAML config and a JSON manifest -- nothing here is
# a false positive for that, so this is a flat refusal rather than a
# heuristic.
_EXECUTABLE_EXTENSIONS = frozenset({
    ".exe", ".dll", ".com", ".scr", ".sys", ".drv", ".cpl", ".msi", ".msp",
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
    ".wsh", ".hta", ".reg", ".lnk", ".jar", ".sh", ".so", ".dylib", ".pyd",
    ".pyc", ".py", ".app", ".bin", ".elf",
})

_HASH_CHUNK_BYTES = 1024 * 1024

_EMERGENCY_BACKUP_DIR_NAME = "restore-backups"


class RestoreError(Exception):
    """Base class. Every message is safe to show a user directly."""


class RestoreRefused(RestoreError):
    """A precondition says now is not a safe moment (e.g. a job is
    RUNNING). Nothing at all was done -- not even a pause."""


class RestoreValidationError(RestoreError):
    """The candidate archive is unusable. The live state was never
    touched; the application is exactly as it was."""


class RestoreFailed(RestoreError):
    """Something failed at or after the atomic replace. `rolled_back`
    says whether the emergency backup was successfully put back;
    `emergency_backup_path` is where that archive is, so a user can always
    be pointed at it."""

    def __init__(self, message: str, *, rolled_back: bool, emergency_backup_path: Optional[Path] = None):
        super().__init__(message)
        self.rolled_back = rolled_back
        self.emergency_backup_path = emergency_backup_path


def _noop() -> None:
    return None


@dataclass(frozen=True)
class RestoreRuntimeHooks:
    """The live-application knobs restore_backup() needs, injected as plain
    callables so this module never imports the web layer (and so tests can
    assert the exact call ORDER without a FastAPI app).

    Every hook defaults to a no-op: a caller with no running worker/watcher
    at all (the CLI, or a unit test) gets the same validated, atomic
    file-level restore with no ceremony. web/backup_service.py supplies the
    WebContext-backed implementations."""

    pause_worker: Callable[[], None] = _noop      # worker_paused.set()   -- step 2
    resume_worker: Callable[[], None] = _noop     # worker_paused.clear() -- step 10
    stop_watcher: Callable[[], None] = _noop      # stop_library_watcher()  -- step 3
    start_watcher: Callable[[], None] = _noop     # start_library_watcher() -- step 10
    quiesce_worker: Callable[[], None] = _noop    # stop_worker()  -- releases the DB handle for step 8
    start_worker: Callable[[], None] = _noop      # start_worker() -- step 9 (fresh DB connection)


@dataclass(frozen=True)
class ValidatedCandidate:
    """A fully-verified, extracted candidate sitting in a temp directory.
    Reaching this means every check in validate_archive() passed."""

    manifest: dict
    db_path: Path
    config_path: Optional[Path]
    root: Path
    files: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Read-only inspection / validation (steps 5-7). Nothing here writes
# anywhere except the caller-supplied temp directory.
# ---------------------------------------------------------------------------

def _reject(message: str) -> "RestoreValidationError":
    return RestoreValidationError(message)


def _check_entry_name(name: str) -> None:
    """Ordered so each refusal reason is distinct and separately testable.
    Applied to the name as it literally appears in the zip's central
    directory -- never to a normalized/resolved version of it, which is how
    traversal checks are usually defeated."""
    if not name or name.strip() == "":
        raise _reject("backup archive contains an entry with an empty name")

    # Absolute, in either separator style, plus Windows drive-qualified
    # ("C:/x", "C:x") and UNC ("//server/share") forms.
    if name.startswith("/") or name.startswith("\\"):
        raise _reject(f"backup archive contains an absolute path entry: {name!r}")
    if ntpath.splitdrive(name)[0]:
        raise _reject(f"backup archive contains an absolute path entry: {name!r}")

    parts = [part for part in name.replace("\\", "/").split("/")]
    if any(part == ".." for part in parts):
        raise _reject(f"backup archive contains a path-traversal entry: {name!r}")

    # Our format is flat by design (three entries, no directories). A
    # nested entry is not something a genuine SwitchAgent backup can
    # contain, so it is refused rather than sanitized.
    if len(parts) > 1:
        raise _reject(f"backup archive contains an unexpected nested path entry: {name!r}")

    if Path(name).suffix.lower() in _EXECUTABLE_EXTENSIONS:
        raise _reject(f"backup archive contains an executable file entry: {name!r}")


def _check_entry_type(info: zipfile.ZipInfo) -> None:
    file_type = (info.external_attr >> 16) & 0o170000
    if file_type == stat.S_IFLNK:
        raise _reject(f"backup archive contains a symlink entry: {info.filename!r}")
    if info.is_dir():
        raise _reject(f"backup archive contains a directory entry: {info.filename!r}")
    # file_type == 0 means the producing tool recorded no unix file type at
    # all -- a DOS/Windows-created zip, or zipfile.writestr() given a plain
    # name. That is a perfectly ordinary regular file as far as the zip
    # format is concerned, and refusing it would reject archives produced
    # by common tooling. Only a type that is genuinely recorded AND is not
    # a regular file (fifo, socket, device node) is refused here; symlinks
    # are caught above, on their own, because that is the one non-regular
    # type with a real attack story behind it.
    if file_type not in (0, stat.S_IFREG):
        raise _reject(f"backup archive contains a non-regular-file entry: {info.filename!r}")


def _load_manifest(archive: zipfile.ZipFile) -> dict:
    try:
        raw = archive.read(backup_mod.MANIFEST_ENTRY_NAME)
    except KeyError as exc:
        raise _reject("backup archive has no manifest.json -- not a SwitchAgent backup") from exc

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _reject(f"backup archive's manifest.json is not valid JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise _reject("backup archive's manifest.json is not a JSON object")

    version = manifest.get("backup_format_version")
    if version is None:
        raise _reject("backup archive's manifest.json has no backup_format_version")
    if version not in SUPPORTED_BACKUP_FORMAT_VERSIONS:
        raise _reject(
            f"unsupported backup format version {version!r} -- this version of SwitchAgent "
            f"can restore {', '.join(str(v) for v in SUPPORTED_BACKUP_FORMAT_VERSIONS)}"
        )

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise _reject("backup archive's manifest.json lists no files")
    for entry in files:
        if not isinstance(entry, dict):
            raise _reject("backup archive's manifest.json has a malformed files entry")
        if not isinstance(entry.get("name"), str):
            raise _reject("backup archive's manifest.json has a files entry with no name")
        if not isinstance(entry.get("sha256"), str):
            raise _reject(
                f"backup archive's manifest.json has no sha256 for {entry.get('name')!r}"
            )
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool):
            raise _reject(
                f"backup archive's manifest.json has no size for {entry.get('name')!r}"
            )
        if size < 0:
            raise _reject(
                f"backup archive's manifest.json declares a negative size for {entry['name']!r}"
            )

    # The manifest is attacker-controllable too, so its declared sizes get
    # the same cap the zip's own central directory does -- otherwise a
    # manifest claiming an enormous size would raise _extract_verified's
    # per-entry streaming limit right along with it.
    if sum(entry["size"] for entry in files) > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise _reject("backup archive's manifest declares a total size beyond the safety limit")
    return manifest


def _extract_verified(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, expected: dict) -> None:
    """Streams one entry out to `target`, hashing as it goes, and refuses
    the moment the stream exceeds the size the manifest declared -- so a
    decompression bomb is stopped mid-stream rather than after it has
    already been written to disk in full."""
    limit = expected["size"]
    digest = hashlib.sha256()
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, target.open("wb") as sink:
        while True:
            chunk = source.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise _reject(
                    f"backup archive entry {info.filename!r} is larger than its manifest entry claims"
                )
            digest.update(chunk)
            sink.write(chunk)

    if written != limit:
        raise _reject(
            f"backup archive entry {info.filename!r} is {written} bytes, "
            f"manifest says {limit}"
        )
    if digest.hexdigest() != expected["sha256"]:
        raise _reject(f"backup archive entry {info.filename!r} failed its SHA-256 checksum check")


def _validate_sqlite_database(path: Path) -> None:
    """Proves the extracted file is a genuinely openable, structurally sane
    SQLite database that actually looks like a SwitchAgent one -- before
    anything in the live application is touched."""
    try:
        conn = sqlite3.connect(path, timeout=30)
    except sqlite3.Error as exc:  # pragma: no cover -- defensive
        raise _reject(f"backup archive's database could not be opened: {exc}") from exc

    try:
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            # "file is not a database" / "database disk image is malformed"
            raise _reject(f"backup archive's database is not a valid SQLite database: {exc}") from exc

        if not rows or rows[0][0] != "ok":
            detail = rows[0][0] if rows else "no result"
            raise _reject(f"backup archive's database failed its integrity check: {detail}")

        try:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        except sqlite3.DatabaseError as exc:  # pragma: no cover -- defensive
            raise _reject(f"backup archive's database could not be read: {exc}") from exc

        missing = [name for name in REQUIRED_CORE_TABLES if name not in tables]
        if missing:
            raise _reject(
                "backup archive's database is not a SwitchAgent database "
                f"(missing table(s): {', '.join(missing)})"
            )
    finally:
        conn.close()


def validate_archive(archive_path: Path, extract_dir: Path) -> ValidatedCandidate:
    """Runs EVERY check, to completion, extracting into `extract_dir`.
    Raises RestoreValidationError on the first refusal. Read-only with
    respect to live application state -- safe to call on its own (see
    inspect_archive) without committing to a restore."""
    archive_path = Path(archive_path)
    extract_dir = Path(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    if not archive_path.is_file():
        raise _reject(f"backup archive not found: {archive_path}")

    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise _reject(f"not a readable zip archive: {exc}") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise _reject(
                f"backup archive has {len(infos)} entries, more than the {MAX_ARCHIVE_ENTRIES} a "
                "SwitchAgent backup can legitimately contain"
            )
        declared_total = sum(max(info.file_size, 0) for info in infos)
        if declared_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise _reject("backup archive's uncompressed size exceeds the safety limit")

        # Structural checks on EVERY entry first (name safety + file type),
        # before the manifest is even trusted to exist -- a malicious entry
        # must be refused whether or not the manifest mentions it.
        for info in infos:
            _check_entry_name(info.filename)
            _check_entry_type(info)

        manifest = _load_manifest(archive)
        listed = {entry["name"]: entry for entry in manifest["files"]}

        # Both directions. An entry the manifest does not name is refused
        # (that is how an attacker would smuggle an unchecked file in), and
        # a file the manifest names but the archive lacks is refused too.
        present = {info.filename for info in infos}
        allowed = set(listed) | {backup_mod.MANIFEST_ENTRY_NAME}
        unexpected = sorted(present - allowed)
        if unexpected:
            raise _reject(
                f"backup archive contains file(s) not listed in its own manifest: {', '.join(unexpected)}"
            )
        missing = sorted(set(listed) - present)
        if missing:
            raise _reject(
                f"backup archive is missing file(s) its manifest lists: {', '.join(missing)}"
            )

        if backup_mod.DB_ENTRY_NAME not in listed:
            raise _reject(
                f"backup archive's manifest does not list {backup_mod.DB_ENTRY_NAME} -- "
                "nothing to restore"
            )

        for info in infos:
            if info.filename == backup_mod.MANIFEST_ENTRY_NAME:
                continue
            _extract_verified(archive, info, extract_dir / info.filename, listed[info.filename])

    db_path = extract_dir / backup_mod.DB_ENTRY_NAME
    _validate_sqlite_database(db_path)

    config_path = extract_dir / backup_mod.CONFIG_ENTRY_NAME
    return ValidatedCandidate(
        manifest=manifest,
        db_path=db_path,
        config_path=config_path if backup_mod.CONFIG_ENTRY_NAME in listed else None,
        root=extract_dir,
        files=list(manifest["files"]),
    )


def inspect_archive(archive_path: Path) -> dict:
    """Read-only "would this restore?" check -- validates a candidate in a
    throwaway temp directory and reports the verdict without touching live
    state or committing to anything."""
    staging = Path(tempfile.mkdtemp(prefix="switchagent-inspect-"))
    try:
        candidate = validate_archive(Path(archive_path), staging / "candidate")
        return {
            "valid": True,
            "backup_format_version": candidate.manifest.get("backup_format_version"),
            "app_version": candidate.manifest.get("app_version"),
            "created_at": candidate.manifest.get("created_at"),
            "files": [dict(entry) for entry in candidate.files],
            "size": candidate.manifest.get("size"),
            "includes_config": candidate.config_path is not None,
        }
    except RestoreValidationError as exc:
        return {"valid": False, "reason": str(exc)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# The destructive half (steps 1-10).
# ---------------------------------------------------------------------------

def _running_job_ids(db_path: Path) -> list[int]:
    """Step 1's check, on its OWN short-lived connection which is closed
    again immediately -- deliberately not a caller-supplied one, because a
    connection still open at step 8 is exactly what makes the atomic
    replace fail on Windows."""
    if not db_path.exists():
        return []
    try:
        conn = db.get_connection(db_path)
    except sqlite3.Error:  # pragma: no cover -- defensive
        return []
    try:
        return [int(row["id"]) for row in db.list_jobs(conn, status="RUNNING")]
    except sqlite3.DatabaseError:
        # An unreadable current database cannot report a RUNNING job. That
        # is not a reason to refuse a restore -- restoring is the obvious
        # remedy for exactly that situation.
        return []
    finally:
        conn.close()


def _prepare_candidate_db(candidate_db: Path) -> None:
    """Step 7. Runs the application's CURRENT migration/validation logic
    (db.init_db) against the candidate, so an OLDER backup's schema is
    upgraded properly instead of being used stale -- and a candidate whose
    schema cannot be migrated is rejected here, while the live state is
    still completely untouched.

    Then normalises the candidate to a SINGLE self-contained file:
    checkpoint the WAL and switch the journal mode to DELETE, so the file
    copied into place at step 8 carries no `-wal`/`-shm` dependency with
    it."""
    try:
        conn = db.get_connection(candidate_db)
    except sqlite3.Error as exc:  # pragma: no cover -- defensive
        raise _reject(f"backup archive's database could not be opened: {exc}") from exc
    try:
        try:
            db.init_db(conn)
        except sqlite3.DatabaseError as exc:
            raise _reject(
                f"backup archive's database could not be migrated to the current schema: {exc}"
            ) from exc
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()
    _remove_sqlite_sidecars(candidate_db)


def _remove_sqlite_sidecars(db_path: Path) -> None:
    """See this module's docstring: a stale `-wal` next to a freshly
    replaced database is a corruption risk, not a leftover."""
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def _replace_file(source: Path, target: Path) -> None:
    """Write to a SIBLING temp path, then os.replace over the original --
    the rename is atomic within a filesystem, so a crash mid-replace can
    never leave a half-written file at `target`; it leaves either the old
    file or the new one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    incoming = target.with_name(target.name + ".restore-incoming")
    try:
        shutil.copy2(source, incoming)
        os.replace(incoming, target)
    except BaseException:
        incoming.unlink(missing_ok=True)
        raise


def _atomic_replace_state(
    candidate: ValidatedCandidate, *, db_path: Path, config_path: Path,
    progress: Optional[list[str]] = None,
) -> list[str]:
    """Step 8. The database first, config.yaml second.

    `progress` is appended to as each file actually lands, so a caller can
    tell "failed before changing anything" from "failed halfway". That
    distinction matters: the first case needs no rollback at all (and must
    not report a failed one), the second always does.

    HONEST LIMITATION: two separate files cannot be replaced in one
    filesystem-atomic operation. Each individual replace is atomic, and a
    failure of the second triggers the caller's rollback of BOTH from the
    emergency backup -- but there is a sub-millisecond window in which the
    new DB is in place and the old config still is. That window is
    harmless here (config.yaml holds extraction limits and the library
    folder; neither is read mid-restore, and the worker is stopped), which
    is why it is documented rather than engineered around with a
    filesystem-level transaction this project has no way to obtain."""
    replaced = progress if progress is not None else []
    _replace_file(candidate.db_path, db_path)
    replaced.append(backup_mod.DB_ENTRY_NAME)
    # Only now that the new database is in place -- a stale WAL from the
    # PREVIOUS database must never be applied to it.
    _remove_sqlite_sidecars(db_path)

    if candidate.config_path is not None:
        _replace_file(candidate.config_path, config_path)
        replaced.append(backup_mod.CONFIG_ENTRY_NAME)
    return replaced


def _rollback_from_emergency(emergency_path: Path, *, db_path: Path, config_path: Path) -> None:
    """Puts back the state captured in step 4, via the same validation the
    incoming archive went through -- the emergency archive is our own, but
    "our own" is not a reason to skip verifying it before trusting it with
    a recovery."""
    staging = Path(tempfile.mkdtemp(prefix="switchagent-rollback-"))
    try:
        candidate = validate_archive(emergency_path, staging / "candidate")
        _prepare_candidate_db(candidate.db_path)
        _atomic_replace_state(candidate, db_path=db_path, config_path=config_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _emergency_backup_path(directory: Path, *, now: datetime) -> Path:
    """Never returns a path that already exists. Two restores within the
    same second are entirely possible -- notably "restore, look at it,
    restore the emergency backup instead" -- and a colliding name would
    have this step OVERWRITE the very archive the user is about to restore
    from, with the state they are trying to get away from. Caught by
    tests/test_restore.py's emergency-backup round-trip."""
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    candidate = directory / f"SwitchAgent-Backup-{stamp}-pre-restore.zip"
    suffix = 1
    while candidate.exists():
        candidate = directory / f"SwitchAgent-Backup-{stamp}-pre-restore-{suffix}.zip"
        suffix += 1
    return candidate


def restore_backup(
    archive_path: Path,
    *,
    db_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    hooks: Optional[RestoreRuntimeHooks] = None,
    emergency_backup_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    """The full ten-step sequence documented at the top of this module.

    Returns a summary dict on success. Raises RestoreRefused (nothing
    happened), RestoreValidationError (nothing happened -- the live state
    is byte-for-byte as it was), or RestoreFailed (something went wrong at
    or after the replace; `rolled_back` says whether the emergency backup
    was put back successfully).

    `db_path`/`config_path` default to config.DB_PATH/config.CONFIG_YAML_PATH,
    resolved at CALL time so they follow APP_DATA_ROOT correctly in every
    runtime mode (dev/installed/portable, see switchagent/paths.py)."""
    archive_path = Path(archive_path)
    if db_path is None:
        db_path = config.DB_PATH
    if config_path is None:
        config_path = config.CONFIG_YAML_PATH
    if hooks is None:
        hooks = RestoreRuntimeHooks()
    if now is None:
        now = datetime.now(timezone.utc)
    if emergency_backup_dir is None:
        emergency_backup_dir = config.APP_DATA_ROOT / _EMERGENCY_BACKUP_DIR_NAME

    # -- step 1: refuse outright while a physical transfer is in flight ---
    running = _running_job_ids(db_path)
    if running:
        raise RestoreRefused(
            "Restore refused: "
            f"{len(running)} job(s) are currently RUNNING (job id(s): "
            f"{', '.join(str(i) for i in running)}). A transfer to a device is in progress -- "
            "wait for it to finish, or cancel it, and try again. Nothing was changed."
        )

    staging = Path(tempfile.mkdtemp(prefix="switchagent-restore-"))
    emergency_path: Optional[Path] = None
    replaced: list[str] = []
    worker_quiesced = False

    try:
        hooks.pause_worker()   # -- step 2 (the existing worker_paused Event)
        try:
            hooks.stop_watcher()   # -- step 3
            try:
                # -- step 4: emergency backup of the CURRENT state, before
                # anything at all is touched.
                emergency_backup_dir = Path(emergency_backup_dir)
                emergency_backup_dir.mkdir(parents=True, exist_ok=True)
                emergency_path = _emergency_backup_path(emergency_backup_dir, now=now)
                try:
                    backup_mod.create_backup(
                        emergency_path, db_path=db_path, config_path=config_path, now=now,
                    )
                except backup_mod.BackupError as exc:
                    raise RestoreError(
                        f"Restore aborted: could not create a safety backup of the current state "
                        f"first ({exc}). Nothing was changed."
                    ) from exc

                # -- steps 5 + 6: extract into a temp dir and fully
                # validate it there. Live state still untouched.
                candidate = validate_archive(archive_path, staging / "candidate")

                # -- step 7: migrate the candidate with the CURRENT logic.
                _prepare_candidate_db(candidate.db_path)

                # -- release the running app's DB handle so step 8's
                # os.replace can actually succeed (see module docstring).
                hooks.quiesce_worker()
                worker_quiesced = True

                try:
                    # -- step 8: atomic replace.
                    _atomic_replace_state(
                        candidate, db_path=db_path, config_path=config_path, progress=replaced,
                    )
                except Exception as exc:
                    if not replaced:
                        # The FIRST replace failed, so nothing was mutated
                        # at all and there is nothing to roll back -- doing
                        # one anyway would only produce a second, scarier
                        # failure. On Windows the usual cause is something
                        # still holding the database file open (see this
                        # module's docstring on why the worker thread must
                        # be quiesced, not merely paused).
                        raise RestoreFailed(
                            f"Restore failed before any change was made ({exc}). Nothing was "
                            "changed -- the application is exactly as it was. On Windows this "
                            "usually means another process or thread still has the database open.",
                            rolled_back=True,
                            emergency_backup_path=emergency_path,
                        ) from exc
                    rolled_back = True
                    try:
                        _rollback_from_emergency(
                            emergency_path, db_path=db_path, config_path=config_path,
                        )
                    except Exception:  # noqa: BLE001 -- reported, never swallowed
                        rolled_back = False
                    raise RestoreFailed(
                        f"Restore failed while replacing application state ({exc})."
                        + (
                            " The previous state was restored from the safety backup."
                            if rolled_back else
                            f" The previous state could NOT be restored automatically -- "
                            f"the safety backup is at {emergency_path}."
                        ),
                        rolled_back=rolled_back,
                        emergency_backup_path=emergency_path,
                    ) from exc
            finally:
                # -- steps 9 + 10: the worker comes back with a FRESH DB
                # connection (that is what "reopen the DB connection the
                # running app holds" means here), and the watcher restarts.
                # In `finally` on purpose: a restore that failed validation
                # must leave the application just as usable as one that
                # succeeded.
                if worker_quiesced:
                    hooks.start_worker()
                hooks.start_watcher()
        finally:
            hooks.resume_worker()   # -- step 10: lift the pause from step 2
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "restored": True,
        "replaced": replaced,
        "backup_format_version": candidate.manifest.get("backup_format_version"),
        "app_version": candidate.manifest.get("app_version"),
        "created_at": candidate.manifest.get("created_at"),
        "emergency_backup_path": str(emergency_path),
        "config_restored": backup_mod.CONFIG_ENTRY_NAME in replaced,
    }
