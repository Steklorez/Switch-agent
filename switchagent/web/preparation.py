"""Small preparation queue, independent of the single MTP worker.

Only job manifests/payload references need to survive restart. An interrupted
preparation is not automatically re-extracted or confirmed on startup.
"""
from __future__ import annotations

import copy
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .. import config, db, extractor

RESERVE_BYTES = 512 * 1024 * 1024

# How long a resolved (Overridden-and-now-DONE, or Skipped) item stays
# visible in a "Failed" batch's panel before PreparationQueue.snapshot()
# stops including it -- same grace period submit() already gives a fully
# successful batch (see its own comment), applied per-item here instead of
# to the whole batch, since sibling "Not started" items in the same batch
# may still need the user's own attention indefinitely.
RESOLVED_ITEM_GRACE_SECONDS = 10.0


def _resolved_long_enough(row) -> bool:
    if not (row['abandoned'] or row['status'] in ('DONE', 'DONE_UNVERIFIED')):
        return False
    finished_at = row['finished_at']
    if not finished_at:
        return False
    from datetime import datetime, timezone
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(finished_at)).total_seconds()
    return elapsed >= RESOLVED_ITEM_GRACE_SECONDS


def remove_extraction(path):
    if path is None:
        return
    path = Path(path)
    root = config.WORK_DIR.resolve()
    # safe_extract creates a random directory immediately under work.
    # Never accept the work root itself, a link, or a Library directory.
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("Extraction directory is outside the controlled work root")
    if path.exists():
        shutil.rmtree(path)


def preflight(conn, item_ids, progress=None):
    errors = []
    required = 0
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    for item_id in item_ids:
        row = db.get_library_item_by_id(conn, item_id)
        try:
            if row is None:
                raise ValueError("library item not found")
            if row["status"] != "AVAILABLE":
                raise ValueError(f"not installable in its current state: {row['status']}")
            path = Path(row["absolute_path"])
            if progress:
                progress(item_id=item_id, phase="Analyzing", name=path.name)
            if not path.exists():
                raise ValueError("source file is missing")
            if path.suffix.lower() in (".zip", ".7z", ".rar"):
                entries = extractor.list_archive_entries(path)
                required += sum(max(0, entry.size) for entry in entries if not entry.is_dir)
            if progress:
                progress(item_id=item_id, phase="Waiting")
        except (OSError, ValueError, extractor.ArchiveError) as exc:
            errors.append({"library_item_id": item_id, "error": str(exc)})
            if progress:
                progress(item_id=item_id, phase="Failed", error=str(exc))
    free = shutil.disk_usage(config.WORK_DIR).free
    if progress:
        progress(workspace=str(config.WORK_DIR.resolve()), free_bytes=free,
                 required_bytes=required, reserve_bytes=RESERVE_BYTES)
    if required + RESERVE_BYTES > free:
        errors.append({"library_item_id": None, "error":
                       f"Insufficient workspace space: need {required + RESERVE_BYTES} bytes, free {free}"})
    return errors


class PreparationQueue:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.serial = threading.Lock()
        self.states = {}
        self.blocked = False
        self.stop = threading.Event()

    @contextmanager
    def maintenance(self):
        from ..restore import RestoreRefused
        with self.lock:
            if self.blocked or any(s["phase"] in ("Waiting", "Preparing") for s in self.states.values()):
                raise RestoreRefused("Wait for batch preparation to finish before restoring a backup")
            self.blocked = True
        try:
            yield
        finally:
            with self.lock:
                self.blocked = False

    def submit(self, item_ids, target):
        task_id = uuid.uuid4().hex
        item_ids = list(dict.fromkeys(item_ids))
        from .. import queue_worker
        items = {}
        with db.open_db(self.db_path) as conn:
            for position, item_id in enumerate(item_ids):
                row = db.get_library_item_by_id(conn, item_id)
                # The SAME resolver display_name_for_job() uses once this
                # item becomes a real job (full filename, extension and
                # bracket tags included) -- not services._resolve_entry_name
                # (the Library page's own .stem-based, extension-dropping
                # convention), so a queued item's name never changes the
                # moment it's confirmed into a job.
                name = queue_worker.resolve_library_item_display_name(conn, row) if row else 'Missing library file'
                if row and row['item_type'] == 'MOD_FOLDER':
                    name += ' — Mod'
                items[str(item_id)] = {'name': name, 'phase': 'Waiting', 'order': position, 'job_ids': []}
        with self.lock:
            if self.blocked:
                raise ValueError("Backup restore is in progress; try again when it finishes")
            # A finished ("Ready") preparation is meant to clear itself the
            # instant it completes (see the `run()` closure below) -- this
            # is a backstop for any that didn't (e.g. the process was
            # killed mid-run) plus every "Failed" one, which stays visible
            # until the user starts something new rather than vanishing on
            # its own (it's explaining why sibling items never started).
            # Unconditional now: this used to only fire once 50+ states had
            # piled up, so in normal use ("install a batch, wait, install
            # another") it never ran at all -- completed batches just sat
            # in the panel forever, looking like nothing ever finished.
            for key in list(self.states):
                if self.states[key]["phase"] in ("Ready", "Failed"):
                    del self.states[key]
            self.states[task_id] = {"id": task_id, "phase": "Waiting", "started": time.time(),
                                    "items": items}

        def update(item_id=None, **fields):
            with self.lock:
                state = self.states[task_id]
                if item_id is not None:
                    fields.pop('name', None)  # Keep the resolved family name, especially for mods.
                    state["items"][str(item_id)].update(fields)
                else:
                    state.update(fields)

        def clear_if_still_ready():
            # By explicit request: Queue must not keep showing a batch once
            # it's fully installed -- a short grace period (not instant)
            # so a poll landing right after completion still gets to render
            # "N / N finished" at least once, rather than the row just
            # disappearing out from under a mid-render page. Re-checks the
            # phase is still "Ready" (not overwritten by a newer submit())
            # before deleting, so this can never remove the wrong state.
            with self.lock:
                state = self.states.get(task_id)
                if state is not None and state.get("phase") == "Ready":
                    del self.states[task_id]

        def run():
            with self.serial:
                update(phase="Preparing")
                try:
                    with db.open_db(self.db_path) as conn:
                        result = self._install_sequentially(conn, item_ids, target, update, task_id)
                    update(phase="Failed" if result["errors"] else "Ready", result=result, finished=time.time())
                    if not result["errors"]:
                        timer = threading.Timer(10.0, clear_if_still_ready)
                        timer.daemon = True  # trivial in-memory cleanup -- never delay app shutdown
                        timer.start()
                except Exception as exc:
                    update(phase="Failed", error=str(exc), finished=time.time())

        # A preparation must finish writing manifests before normal process exit.
        threading.Thread(target=run, name="switchagent-preparation", daemon=False).start()
        return task_id

    def _install_sequentially(self, conn, item_ids, target, update, task_id):
        from .services import create_and_confirm_jobs
        from ..work_cleanup import cleanup_batch_if_all_done
        from ..manifest import batch_work_dir
        import json
        # One bounded-history log per Install click; never one per archive/job.
        log_dir = config.LOGS_DIR / 'installs'
        log_dir.mkdir(parents=True, exist_ok=True)
        logs = sorted(log_dir.glob('install-*.log'), key=lambda p: p.stat().st_mtime)
        for old in logs[:-9]:
            if old.is_file() and not old.is_symlink():
                old.unlink()
        result = {'created': [], 'errors': [], 'batch_id': None}
        with (log_dir / f'install-{task_id}.log').open('w', encoding='utf-8') as log:
            def record(**fields):
                log.write(json.dumps({'time': db.now_iso(), **fields}, ensure_ascii=False) + '\n')
                log.flush()
            record(event='Install started', items=item_ids)
            try:
                for item_id in item_ids:
                    if self.stop.is_set():
                        raise RuntimeError('Application stopped; remaining items were not prepared')
                    record(event='Preparing', item=item_id)
                    def preparing(item_id=None, **fields):
                        if fields.get('phase') == 'Ready':
                            fields['phase'] = 'Prepared'
                        update(item_id=item_id, **fields)
                    part = create_and_confirm_jobs(conn, [item_id], target, progress=preparing)
                    result['created'].extend(part['created'])
                    result['errors'].extend(part['errors'])
                    result['batch_id'] = part['batch_id']
                    update(item_id=item_id, job_ids=[j['job_id'] for j in part['created']])
                    record(event='Prepared', result=part)
                    if part['errors']:
                        break
                    batch = part['batch_id']
                    if batch is None:
                        continue
                    update(item_id=item_id, phase='Installing')
                    previous = None
                    while True:
                        jobs = db.list_jobs_by_batch(conn, batch)
                        statuses = [(j['id'], j['status'], j['error']) for j in jobs]
                        if statuses != previous:
                            record(event='Jobs', jobs=statuses)
                            previous = statuses
                        # Every status a job can get stuck in without ever
                        # reaching DONE/DONE_UNVERIFIED on its own -- kept in
                        # sync with services._RETRYABLE_JOB_STATUSES (the
                        # canonical "needs a user action" set) plus CANCELLED
                        # (dead status string, never actually set, kept here
                        # defensively) and WAITING_FOR_BASE (this run's own
                        # item racing a DIFFERENT outstanding base job).
                        # DESTINATION_CONFLICT was the original gap: missing
                        # from this set meant a real conflict mid-batch just
                        # spun this poll loop every .5s forever -- reachable,
                        # reproduced with a real device (the exact WAITING
                        # state after it never resolved on its own).
                        if any(j['status'] in (
                            'FAILED', 'INTERRUPTED', 'CANCELLED', 'WAITING_FOR_BASE',
                            'DESTINATION_CONFLICT', 'SOURCE_CHANGED', 'DEVICE_UNAVAILABLE', 'BLOCKED_BY_DEPENDENCY',
                        ) for j in jobs):
                            raise RuntimeError('Installation stopped; resolve the current jobs before starting remaining items')
                        if jobs and all(j['status'] in ('DONE', 'DONE_UNVERIFIED') for j in jobs):
                            break
                        if self.stop.wait(.5):
                            raise RuntimeError('Application stopped; remaining items were not prepared')
                    update(item_id=item_id, phase='Cleaning')
                    cleanup_batch_if_all_done(conn, batch)
                    if batch_work_dir(batch).exists():
                        raise RuntimeError('Temporary payload cleanup failed; next archive was not extracted')
                    update(item_id=item_id, phase='Ready')
                    record(event='Installed and cleaned', item=item_id)
            except Exception as exc:
                update(item_id=item_id, phase='Failed', error=str(exc))
                record(event='Stopped', error=str(exc))
                raise
            record(event='Install finished', errors=result['errors'])
        return result

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(list(self.states.values()))
        for state in result:
            state["elapsed"] = int(state.get("finished", time.time()) - state["started"])
        from .services import _job_view, _resolve_latest_retry
        with db.open_db(self.db_path) as conn:
            for state in result:
                resolved_item_ids = []
                for item_id, item in state['items'].items():
                    # A stored job_id is frozen at creation time -- if that
                    # job was since Overridden/Retried (see
                    # _resolve_latest_retry's own docstring), show the
                    # current attempt, not the superseded original.
                    rows = [_resolve_latest_retry(conn, row) for job_id in item.get('job_ids', [])
                            if (row := conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())]
                    # By explicit request: once THIS item's own conflict has
                    # been resolved (Override succeeded, or Skip was
                    # clicked) and stayed that way for 10s -- the same
                    # grace period submit()'s own auto-clear already gives
                    # a fully successful batch -- its row is no longer
                    # useful clutter, even while sibling items in the same
                    # (already-dead, never-resuming) batch are still stuck
                    # "Not started" and genuinely still need the user's own
                    # action elsewhere. `rows and ...`: an item that never
                    # got a job at all (job_ids empty) must never be swept
                    # up here -- all() on an empty list is vacuously True.
                    if rows and all(_resolved_long_enough(row) for row in rows):
                        resolved_item_ids.append(item_id)
                        continue
                    item['jobs'] = [_job_view(conn, row) for row in rows]
                for item_id in resolved_item_ids:
                    del state['items'][item_id]
        return result
