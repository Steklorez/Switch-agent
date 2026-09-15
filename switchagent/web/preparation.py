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
            # Bound completed display history, retaining all running work.
            for key in list(self.states):
                if len(self.states) < 50:
                    break
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

        def run():
            with self.serial:
                update(phase="Preparing")
                try:
                    with db.open_db(self.db_path) as conn:
                        result = self._install_sequentially(conn, item_ids, target, update, task_id)
                    update(phase="Failed" if result["errors"] else "Ready", result=result, finished=time.time())
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
                        if any(j['status'] in ('FAILED', 'INTERRUPTED', 'CANCELLED', 'WAITING_FOR_BASE') for j in jobs):
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
        from .services import _job_view
        with db.open_db(self.db_path) as conn:
            for state in result:
                for item in state['items'].values():
                    item['jobs'] = [_job_view(conn, row) for job_id in item.get('job_ids', [])
                                    if (row := conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())]
        return result
