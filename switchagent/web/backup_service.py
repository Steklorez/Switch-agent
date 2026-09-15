"""W3-005: the HTTP-facing wrapper for backup/restore.

Deliberately its own module rather than more weight on the already-large
web/services.py, and deliberately THIN: request/response shaping, temp-file
lifecycle for the download/upload, and wiring WebContext's existing worker/
watcher controls into restore.py's hook interface. Every actual decision --
what a backup contains, what makes an archive valid, the order of the
restore sequence -- lives in switchagent/backup.py and
switchagent/restore.py, which know nothing about FastAPI.

WHY THE ROUTES MUST NOT TAKE app.get_conn's DEPENDENCY: that dependency
holds an open sqlite3 connection for the whole request. On Windows,
os.replace() over a file with an open handle fails outright (see
restore.py's module docstring), so a restore request that held one would
reliably fail at the last step. Both routes here manage their own
short-lived connections instead -- which is also why they are the only
routes in web/app.py without `conn=Depends(get_conn)`.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import backup as backup_mod, config, restore as restore_mod
from .context import WebContext

log = logging.getLogger("switchagent.web.backup")

# Enough for a database plus a config file; a backup archive is small
# (payloads are never in it, see backup.py). A larger upload is refused
# before it is streamed to disk rather than after.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

_UPLOAD_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class BackupDownload:
    """A freshly-created archive in its own temp directory, plus the
    cleanup that must run once the response has finished streaming."""

    path: Path
    filename: str
    size_bytes: int
    _staging: Path

    def cleanup(self) -> None:
        shutil.rmtree(self._staging, ignore_errors=True)


def create_backup_download(ctx: WebContext) -> BackupDownload:
    """GET /api/backup: always a FRESH backup, taken at request time --
    never a cached or previously-written archive, so what the user
    downloads is the state they have right now.

    Written into a temp directory (not into APP_DATA_ROOT) because it is
    purely in-flight data for one HTTP response -- the app does not keep a
    backup library of its own. The caller attaches cleanup() as the
    response's background task."""
    staging = Path(tempfile.mkdtemp(prefix="switchagent-backup-dl-"))
    try:
        filename = backup_mod.backup_filename()
        result = backup_mod.create_backup(
            staging / filename, db_path=ctx.db_path, config_path=config.CONFIG_YAML_PATH,
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BackupDownload(
        path=result.path, filename=result.filename, size_bytes=result.size_bytes, _staging=staging,
    )


class _WebContextRestoreHooks:
    """Adapts WebContext's EXISTING worker/watcher controls to restore.py's
    hook interface. Nothing new is invented here:

      - pause/resume use the same `worker_paused` Event that
        POST /api/worker/pause and /api/worker/resume already set/clear;
      - the watcher uses the same stop_library_watcher()/
        start_library_watcher() that W3-002's library-folder change
        already uses;
      - quiesce/start use WebContext's own stop_worker()/start_worker().

    stop_worker() is needed on top of the pause because a PAUSED worker
    still holds its sqlite3 connection open (see context._worker_loop --
    a paused iteration just waits), and an open handle makes the atomic
    replace fail on Windows. `_did_quiesce` makes the restart conditional:
    a context whose worker was never running (the test suite's web_ctx
    fixture, or a CLI caller) must not have one silently STARTED by a
    restore."""

    def __init__(self, ctx: WebContext):
        self._ctx = ctx
        self._did_quiesce = False
        self._watcher_was_running = False

    def as_hooks(self) -> restore_mod.RestoreRuntimeHooks:
        return restore_mod.RestoreRuntimeHooks(
            pause_worker=self.pause_worker,
            resume_worker=self.resume_worker,
            stop_watcher=self.stop_watcher,
            start_watcher=self.start_watcher,
            quiesce_worker=self.quiesce_worker,
            start_worker=self.start_worker,
        )

    def pause_worker(self) -> None:
        self._ctx.worker_paused.set()

    def resume_worker(self) -> None:
        self._ctx.worker_paused.clear()

    def stop_watcher(self) -> None:
        self._watcher_was_running = self._ctx.library_watcher_running
        self._ctx.stop_library_watcher()  # safe no-op if it was not running

    def start_watcher(self) -> None:
        if self._watcher_was_running:
            self._ctx.start_library_watcher()

    def quiesce_worker(self) -> None:
        if self._ctx.worker_running:
            self._ctx.stop_worker()
            self._did_quiesce = True

    def start_worker(self) -> None:
        if self._did_quiesce:
            self._ctx.start_worker()
            self._did_quiesce = False


def save_upload(upload_file, staging: Path) -> Path:
    """Streams an uploaded archive to disk in bounded chunks -- never
    `.read()` of the whole body into memory, and never past
    MAX_UPLOAD_BYTES."""
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / "uploaded-backup.zip"
    total = 0
    with target.open("wb") as sink:
        while True:
            chunk = upload_file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise restore_mod.RestoreValidationError(
                    "Uploaded file is too large to be a SwitchAgent backup."
                )
            sink.write(chunk)
    if total == 0:
        raise restore_mod.RestoreValidationError("No file was uploaded, or the file was empty.")
    return target


def restore_from_upload(ctx: WebContext, upload_file) -> dict:
    with ctx.preparations.maintenance():
        return _restore_from_upload(ctx, upload_file)


def _restore_from_upload(ctx: WebContext, upload_file) -> dict:
    """POST /api/restore: run the full ten-step restore sequence against an
    uploaded archive. Exceptions are deliberately NOT caught here -- the
    route maps restore.py's exception types onto status codes, so the
    distinction between "refused, nothing happened" (409), "invalid
    archive, nothing happened" (400) and "failed mid-restore" (500)
    survives all the way to the client."""
    staging = Path(tempfile.mkdtemp(prefix="switchagent-restore-upload-"))
    try:
        archive_path = save_upload(upload_file, staging)
        hooks = _WebContextRestoreHooks(ctx)
        log.info("restore requested -- validating uploaded archive")
        result = restore_mod.restore_backup(
            archive_path,
            db_path=ctx.db_path,
            config_path=config.CONFIG_YAML_PATH,
            hooks=hooks.as_hooks(),
        )
        log.info("restore completed -- replaced %s", ", ".join(result["replaced"]))
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)
