"""emuiibo on each connected console: reading its state and removing amiibo,
done on the worker thread, a slice at a time.

Why here and not in a request handler: every MTP call has to come from the
worker thread (see WebContext's class-level COM note -- a request thread's
COM call on a live backend once corrupted a real transfer). Why in slices:
a console with the real 766-amiibo collection is ~800 folder listings, most
of a minute over MTP, and an install the user starts meanwhile must not
wait behind it. The worker gives this module a time budget whenever it has
no job to run (WebContext._worker_loop); a read or a removal carries on from
exactly where it stopped on the next pass, because each is a generator that
yields after every single MTP request.

HTTP threads only ever call request_read(), request_removal() and
snapshot(): plain dict operations under a lock, never COM.

A removal is not a queue job: it has no Library item to be one of (the
jobs table requires one), and nothing about it waits on anything else. It
is still explicit (the user picks amiibo and confirms), checked against the
console's own last read (an amiibo with save data is refused unless that
was confirmed too), limited to emuiibo/amiibo/ (emuiibo.is_inside_amiibo_dir,
checked again per object), done one object at a time with every deletion
verified by a fresh listing, and logged object by object.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional

from .. import addons, db, emuiibo
from ..mtp.errors import InvalidOperationError, MtpError
from ..transfer import STORAGE_SD_CARD

log = logging.getLogger("switchagent.web.emuiibo")

# How long one worker pass may spend here before handing the thread back.
DEFAULT_SLICE_SECONDS = 1.0


class RemovalRefused(Exception):
    """A removal the service will not start -- the message says why."""


class EmuiiboService:
    def __init__(self, *, slice_seconds: float = DEFAULT_SLICE_SECONDS):
        self.slice_seconds = slice_seconds
        self._lock = threading.Lock()
        self._wanted: set[str] = set()                 # devices whose state should be read again
        self._readers: dict[str, Iterator] = {}
        self._reading: dict[str, dict] = {}            # device -> {"folders", "started", "amiibo"}
        self._errors: dict[str, str] = {}              # device -> why its last read failed
        self._tasks: list[dict] = []                   # removals, oldest first
        self._finished: dict[str, dict] = {}           # task id -> the finished task (for its page)
        self._live: set[str] = set()

    # -- HTTP threads ------------------------------------------------------

    def request_read(self, device_id: str) -> None:
        with self._lock:
            self._wanted.add(device_id)
            self._readers.pop(device_id, None)  # a fresh read, not the rest of an old one
            self._errors.pop(device_id, None)

    def request_removal(self, conn, device_id: str, paths: list[str], *, confirm_save_data: bool) -> dict:
        """Queues the removal of amiibo (or whole folders of them) from
        emuiibo/amiibo/ on one console. Raises RemovalRefused when it must
        not start: the console is not connected, a path is not inside
        emuiibo/amiibo/, nothing of it is on the console as last read, or it
        holds save data the request did not explicitly accept losing."""
        clean = []
        for path in dict.fromkeys(p.strip().strip("/") for p in paths):
            if not path or not emuiibo.is_inside_amiibo_dir(f"{emuiibo.AMIIBO_DIR}/{path}"):
                raise RemovalRefused(f"not an amiibo path: {path!r}")
            clean.append(path)
        if not clean:
            raise RemovalRefused("nothing selected")
        with self._lock:
            if device_id not in self._live:
                raise RemovalRefused("this Switch is not connected")
        known = db.get_device_emuiibo(conn, device_id)
        if known is None:
            raise RemovalRefused("this Switch's amiibo have not been read yet")
        _state, amiibo, _read_at = known
        affected = [a for a in amiibo if any(_covers(p, a["path"]) for p in clean)]
        if not affected:
            raise RemovalRefused("none of this is on the Switch any more")
        with_save = [a["path"] for a in affected if a.get("save_data")]
        if with_save and not confirm_save_data:
            raise RemovalRefused(
                f"{len(with_save)} of these amiibo hold game save data -- confirm that it may be lost"
            )
        task = {
            "id": uuid.uuid4().hex[:12], "device_id": device_id, "paths": clean,
            "amiibo": len(affected), "save_data": len(with_save),
            "state": "waiting", "deleted": 0, "removed": [], "error": None,
            "created": time.time(), "finished": None,
        }
        with self._lock:
            self._tasks.append(task)
        log.info("amiibo removal queued: device=%s paths=%s (%d amiibo, %d with save data)",
                 _short(device_id), clean, len(affected), len(with_save))
        return _public(task)

    def snapshot(self, device_id: str) -> dict:
        with self._lock:
            reading = dict(self._reading.get(device_id) or {})
            tasks = [_public(t) for t in self._tasks if t["device_id"] == device_id]
            finished = [_public(t) for t in self._finished.values() if t["device_id"] == device_id]
            return {
                "reading": bool(reading) or device_id in self._wanted,
                "folders_read": reading.get("folders", 0),
                "amiibo_read": reading.get("amiibo", 0),
                "read_error": self._errors.get(device_id),
                "removals": tasks,
                "finished_removals": sorted(finished, key=lambda t: -(t["finished"] or 0))[:5],
                "connected": device_id in self._live,
            }

    def busy_devices(self) -> dict:
        """device -> what is being done to its emuiibo right now, for the
        Library's activity panel."""
        with self._lock:
            out = {d: "Reading amiibo" for d in self._reading}
            for t in self._tasks:
                out[t["device_id"]] = "Removing amiibo"
            return out

    # -- worker thread -----------------------------------------------------

    def on_devices(self, live_ids: set[str], newly_live: set[str]) -> None:
        """From refresh_devices(): a console just connected is read; one
        that went away stops being read or changed."""
        with self._lock:
            self._live = set(live_ids)
            self._wanted |= newly_live
            for gone in [d for d in self._readers if d not in live_ids]:
                self._readers.pop(gone, None)
                self._reading.pop(gone, None)
            for task in [t for t in self._tasks if t["device_id"] not in live_ids]:
                task["state"] = "failed"
                task["error"] = task["error"] or "the Switch was disconnected before this finished"
                self._finish_locked(task)

    def note_job(self, conn, job_id: Optional[int]) -> None:
        """After a job: one that wrote emuiibo or amiibo changed what the
        console holds, so it is read again."""
        if job_id is None:
            return
        job = db.get_job(conn, job_id)
        if job is None or job["library_item_id"] is None:
            return
        item = db.get_library_item_by_id(conn, job["library_item_id"])
        if item is not None and item["content_type"] in ("EMUIIBO", "AMIIBO", "ADDON"):
            self.request_read(job["target_device_id"])

    def step(self, conn, registry) -> bool:
        """Spends at most one slice on removals first (the user asked for
        those and is watching), then on reads. True while work remains."""
        deadline = time.monotonic() + self.slice_seconds
        with self._lock:
            task = next((t for t in self._tasks if t["device_id"] in self._live), None)
        if task is not None:
            self._run_removal(conn, registry, task, deadline)
        with self._lock:
            wanted = [d for d in self._wanted if d in self._live]
        for index, device_id in enumerate(wanted):
            # Every pass moves at least one read forward, however small the
            # slice: a read must finish at MTP speed, not starve.
            if index and time.monotonic() >= deadline:
                break
            self._run_read(conn, registry, device_id, deadline)
        with self._lock:
            return bool([t for t in self._tasks if t["device_id"] in self._live]
                        or [d for d in self._wanted if d in self._live])

    # -- internals ---------------------------------------------------------

    def _run_read(self, conn, registry, device_id: str, deadline: float) -> None:
        backend = registry.get(device_id)
        if backend is None:
            with self._lock:
                self._wanted.discard(device_id)
            return
        with self._lock:
            reader = self._readers.get(device_id)
            if reader is None:
                known = db.get_device_emuiibo(conn, device_id)
                previous = {a["path"].lower(): emuiibo.DeviceAmiibo.from_dict(a) for a in known[1]} if known else {}
                progress = self._reading.setdefault(device_id, {"folders": 0, "amiibo": 0, "started": time.time()})

                def on_progress(folders, _total, _progress=progress):
                    _progress["folders"] = folders

                stored_addons = db.get_device_addons(conn, device_id)
                known_addons = addons.ConsoleAddons.from_dict(stored_addons[0]) if stored_addons else None
                reader = _read_console(backend, previous, on_progress, known_addons)
                self._readers[device_id] = reader
        state = None
        finished = False
        try:
            while True:
                kind, found = next(reader)
                if kind == "emuiibo":
                    state = found
                    with self._lock:
                        if device_id in self._reading:
                            self._reading[device_id]["amiibo"] = len(state.amiibo)
                    if state.complete:
                        # Saved the moment it is known: the Amiibo page does
                        # not wait for the add-ons part of the read.
                        self._save_emuiibo(conn, device_id, state)
                elif found.complete:
                    db.set_device_addons(conn, device_id, found.to_dict())
                    log.info("add-ons read off device=%s: %d catalog file(s) present%s", _short(device_id),
                             len(found.files), ", kefir" if found.kefir else "")
                    finished = True
                    break
                if time.monotonic() >= deadline:
                    break
        except StopIteration:
            finished = True
        except (MtpError, InvalidOperationError) as exc:
            log.warning("reading emuiibo off device=%s failed: %s", _short(device_id), exc)
            with self._lock:
                self._errors[device_id] = str(exc)
                self._readers.pop(device_id, None)
                self._reading.pop(device_id, None)
                self._wanted.discard(device_id)
            return
        if finished:
            with self._lock:
                self._readers.pop(device_id, None)
                self._reading.pop(device_id, None)
                self._wanted.discard(device_id)
                # A read that went through answers the question the last
                # failure left open -- the page must not keep showing it.
                self._errors.pop(device_id, None)

    @staticmethod
    def _save_emuiibo(conn, device_id: str, state) -> None:
        db.set_device_emuiibo(conn, device_id, state.to_dict(), [a.to_dict() for a in state.amiibo])
        log.info("emuiibo read off device=%s: installed=%s overlay=%s amiibo=%d",
                 _short(device_id), state.installed, state.overlay_version, len(state.amiibo))

    def _run_removal(self, conn, registry, task: dict, deadline: float) -> None:
        backend = registry.get(task["device_id"])
        if backend is None:
            return
        steps = task.get("_steps")
        if steps is None:
            steps = task["_steps"] = _removal_steps(backend, task)
            task["state"] = "running"
        try:
            while True:
                next(steps)
                if time.monotonic() >= deadline:
                    break
        except StopIteration:
            task["state"] = "done"
        except (MtpError, InvalidOperationError, ValueError) as exc:
            task["state"] = "failed"
            task["error"] = str(exc)
            log.warning("amiibo removal on device=%s stopped: %s", _short(task["device_id"]), exc)
        if task["state"] in ("done", "failed"):
            _forget_removed(conn, task["device_id"], task["removed"])
            with self._lock:
                self._finish_locked(task)
            log.info("amiibo removal on device=%s %s: %d object(s) deleted, removed %s",
                     _short(task["device_id"]), task["state"], task["deleted"], task["removed"])

    def _finish_locked(self, task: dict) -> None:
        task["finished"] = time.time()
        task.pop("_steps", None)
        if task in self._tasks:
            self._tasks.remove(task)
        self._finished[task["id"]] = task
        if len(self._finished) > 20:
            oldest = min(self._finished.values(), key=lambda t: t["finished"])
            self._finished.pop(oldest["id"], None)


def _covers(selected: str, amiibo_path: str) -> bool:
    s, a = selected.lower(), amiibo_path.lower()
    return a == s or a.startswith(s + "/")


def _removal_steps(backend, task: dict) -> Iterator[None]:
    """One MTP deletion per step. A folder is emptied bottom-up and removed
    last; everything is re-checked against emuiibo/amiibo/ right before it
    goes. Afterwards the overlay's favorites lose the lines that pointed at
    what was removed -- every other line is kept exactly as it was."""
    for path in task["paths"]:
        sd_path = f"{emuiibo.AMIIBO_DIR}/{path}"
        if not emuiibo.is_inside_amiibo_dir(sd_path):
            raise ValueError(f"refusing to remove {sd_path!r}")
        listing = backend.list_directory(STORAGE_SD_CARD, sd_path)
        objects = [sd_path] if listing is None else emuiibo.removal_order(backend, STORAGE_SD_CARD, sd_path)
        yield
        for obj in objects:
            if not emuiibo.is_inside_amiibo_dir(obj):
                raise ValueError(f"refusing to remove {obj!r}")
            backend.delete(STORAGE_SD_CARD, obj)
            task["deleted"] += 1
            log.info("removed %s from device=%s", obj, _short(task["device_id"]))
            yield
        task["removed"].append(path)
    if not task["removed"]:
        return
    data = backend.read_file(STORAGE_SD_CARD, emuiibo.FAVORITES_FILE, max_bytes=emuiibo.FAVORITES_MAX_BYTES)
    yield
    rewritten = emuiibo.favorites_without(data, task["removed"]) if data else None
    if rewritten is not None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "favorites.txt"
            local.write_bytes(rewritten)
            backend.send_file(STORAGE_SD_CARD, emuiibo.FAVORITES_FILE, local, overwrite=True)
        log.info("favorites.txt on device=%s no longer lists what was removed", _short(task["device_id"]))
        yield


def _forget_removed(conn, device_id: str, removed: list[str]) -> None:
    """The console's last read, minus what was just removed and proven gone
    -- no need to walk hundreds of folders again to learn that."""
    if not removed:
        return
    known = db.get_device_emuiibo(conn, device_id)
    if known is None:
        return
    state, amiibo, read_at = known
    kept = [a for a in amiibo if not any(_covers(r, a["path"]) for r in removed)]
    state["favorites"] = [f for f in state.get("favorites") or [] if not any(_covers(r, f) for r in removed)]
    db.set_device_emuiibo(conn, device_id, state, kept, read_at=read_at)


def _read_console(backend, previous, on_progress, known_addons) -> Iterator[tuple[str, object]]:
    """One read of a console: emuiibo and its amiibo, then the Add-ons
    catalog's utilities -- ("emuiibo", state) / ("addons", state) after
    every MTP request."""
    for state in emuiibo.read_device(backend, STORAGE_SD_CARD, known=previous, progress=on_progress):
        yield "emuiibo", state
    for found in addons.read_device(backend, STORAGE_SD_CARD, addons.installable_catalog(), known=known_addons):
        yield "addons", found


def _public(task: dict) -> dict:
    return {k: v for k, v in task.items() if not k.startswith("_") and k != "device_id"}


def _short(device_id: str) -> str:
    from ..mtp.windows import device_fingerprint
    try:
        return device_fingerprint(device_id)
    except Exception:  # noqa: BLE001 -- a log label must never fail anything
        return "?"


# ---------------------------------------------------------------------------
# emuiibo from GitHub: download, add to the Library, install
# ---------------------------------------------------------------------------

# emuiibo's "download from GitHub and install" is one case of installing
# from the Add-ons catalog (web/addons_service.py); the old names stay.
from .addons_service import AddonInstaller as ReleaseDownloader, DownloadRefused  # noqa: E402,F401
