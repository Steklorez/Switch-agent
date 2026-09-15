"""Runtime context shared by every route handler and the background worker
thread -- constructed once at process startup (see switchagent/cli.py's
`web` command) and stored on `app.state.ctx`.

Two backend "modes", chosen at construction time, never mixed at runtime:
  - real (default): devices are discovered live via
    switchagent.mtp.windows.enumerate_devices() and backed by
    RealMtpBackend instances, created on first sighting.
  - mock (--mock / MOCK_MTP=true, matching the original project-wide mock
    mode convention): a fixed, pre-registered set of MockMtpBackend
    instances -- no real hardware, no pywin32 calls, safe for development
    and for the automated test suite.

SQLite connections are not shared across threads: each request opens its
own short-lived connection (see switchagent/web/services.py's `with
db.open_db(ctx.db_path) as conn:` pattern), and the worker thread owns one
long-lived connection of its own. WAL mode (already enabled by
db.get_connection()) makes this safe for SQLite's usual
one-writer/many-readers model.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import config, db, queue_worker
from ..mtp.base import DeviceInfo, MtpBackend
from ..mtp.errors import DeviceNotFoundError

log = logging.getLogger("switchagent.web")

# How long the worker thread sleeps between passes when it found nothing
# processable (see _worker_loop below) -- i.e. the worst-case delay before
# a freshly-CONFIRMED job is even noticed. Lowered from an initial 5.0s
# after a real-hardware install (docs/REAL-HARDWARE-TEST-*.md) measured a
# 4s gap between job creation and RUNNING, which combined with the Queue
# page's own polling made a genuinely fast (~15s total) install nearly
# invisible in the UI. This does not touch RealMtpBackend or any transfer/
# verification timing -- purely how often run_worker_once() gets a chance
# to notice new work.
DEFAULT_WORKER_POLL_INTERVAL_SECONDS = 2.0

# Same debounce value as cli.py's `scan --watch` (a separate, older,
# INBOX_DIR-watching CLI process -- see start_library_watcher()'s own
# docstring for why that one is untouched rather than shared code): a
# burst of filesystem events (e.g. a multi-file copy, or a downloader
# still writing) coalesces into exactly one rescan, fired this many
# seconds after the LAST observed event, not the first.
DEFAULT_LIBRARY_WATCH_DEBOUNCE_SECONDS = 5.0

# UI-007: how often refresh_devices() actually calls the (COM-expensive,
# on a real backend) list_storages() per connected device, as opposed to
# the plain connect()/overrides refresh it already does every single
# worker tick (~worker_poll_interval_seconds, cheap, no extra COM beyond
# what refresh_devices() already did anyway). Storage free/total space
# does not need second-by-second freshness -- this trades a little
# staleness for meaningfully less COM traffic on every tick.
DEFAULT_STORAGE_REFRESH_INTERVAL_SECONDS = 30.0


@dataclass
class ScanState:
    running: bool = False
    summary: Optional[dict] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    current_filename: Optional[str] = None


class WebContext:
    def __init__(
        self,
        *,
        db_path: Path,
        registry: queue_worker.DeviceRegistry,
        discover_devices: Callable[[queue_worker.DeviceRegistry], None],
        worker_poll_interval_seconds: float = DEFAULT_WORKER_POLL_INTERVAL_SECONDS,
        library_watch_debounce_seconds: float = DEFAULT_LIBRARY_WATCH_DEBOUNCE_SECONDS,
        storage_refresh_interval_seconds: float = DEFAULT_STORAGE_REFRESH_INTERVAL_SECONDS,
    ):
        """`discover_devices(registry)` is the one mode-specific hook: it
        must register any newly-visible device_id's MtpBackend into
        `registry` (real mode: enumerate + RealMtpBackend(device_id=...);
        mock mode: a no-op, since mock backends are all pre-registered
        before the context is even constructed -- see build_real_context()/
        build_mock_context() below)."""
        self.db_path = db_path
        from .preparation import PreparationQueue
        self.preparations = PreparationQueue(db_path)
        from ..covers import CoverQueue
        self.covers = CoverQueue()
        self.registry = registry
        self._discover_devices = discover_devices
        self.worker_poll_interval_seconds = worker_poll_interval_seconds
        self.library_watch_debounce_seconds = library_watch_debounce_seconds
        self.storage_refresh_interval_seconds = storage_refresh_interval_seconds

        self.worker_paused = threading.Event()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        # UI-006 "Restart worker when safe" -- see request_worker_restart()
        # below for exactly what this can and cannot do.
        self._worker_restart_requested = threading.Event()

        self.scan_lock = threading.Lock()
        self.scan_state = ScanState()

        # See start_library_watcher()/stop_library_watcher() below.
        # Deliberately NOT started here -- exactly like _worker_thread
        # above, so constructing a WebContext (e.g. the test suite's
        # web_ctx fixture) never has a side effect of watching the
        # filesystem; only cli.py's `switch-agent web` command turns it on.
        self._watcher_observer = None
        self._watcher_stop_event: Optional[threading.Event] = None
        self._watcher_thread: Optional[threading.Thread] = None

        # See refresh_devices()/get_known_devices() below -- a small,
        # COM-free cache the worker thread writes to and every HTTP
        # request thread only ever reads from. Its own lock is deliberately
        # separate from (and much cheaper than) anything that could ever be
        # held for the duration of a real MTP transfer.
        self._device_cache: list[DeviceInfo] = []
        self._device_cache_lock = threading.Lock()

        # UI-007: worker-owned storage snapshot -- populated by
        # refresh_devices() (below), read by every HTTP request thread via
        # get_known_storages(), same COM-safety split as _device_cache
        # above. Refreshed only every storage_refresh_interval_seconds,
        # not every tick (see that constant's own comment).
        self._storage_cache: dict[str, list] = {}  # device_id -> list[StorageInfo]
        self._storage_cache_lock = threading.Lock()
        self._last_storage_refresh_monotonic: float = 0.0

        # Installed-games cross-reference (Library UI's "on this Switch"
        # highlight) -- same COM-safety split and refresh cadence as
        # _storage_cache above (see refresh_devices()'s should_refresh_storage
        # gate): populated worker-thread-only from
        # MtpBackend.list_installed_title_ids(), read by every HTTP request
        # thread via get_known_installed_title_ids(), never queried live
        # from a request thread. A device that stops reporting a set (DBI
        # setting turned off, transient hiccup) keeps its last-known
        # snapshot rather than being wiped -- identical "stale but honest"
        # convention as _storage_cache.
        self._installed_games_cache: dict[str, set[str]] = {}  # device_id -> base_title_ids
        self._installed_games_cache_lock = threading.Lock()

        # Worker heartbeat (UI-001/UI-006): updated once per _worker_loop
        # iteration, whether or not that iteration found any work -- this
        # is "the worker thread is alive and ticking", a different signal
        # from any individual job's own last_progress_at. Read-only from
        # every HTTP request thread, exactly like _device_cache above.
        self._worker_heartbeat_lock = threading.Lock()
        self._worker_last_heartbeat_at: Optional[str] = None

    # -- devices --------------------------------------------------------
    #
    # IMPORTANT (found via a real-hardware incident, see docs/STATE.md's
    # Quake II investigation): refresh_devices() below calls
    # backend.connect() on live RealMtpBackend/win32com objects, which are
    # NOT thread-safe -- Shell.Application's COM objects are apartment-
    # threaded. This method must be called ONLY from the single worker
    # thread (_worker_loop). It used to also be called directly from every
    # HTTP request thread (via services.list_devices()) on every page load
    # and every /api/devices poll -- that let a request thread's connect()
    # call race the worker thread's own connect()/send_file() call on the
    # SAME RealMtpBackend instance mid-transfer, invalidating its cached
    # COM object references. Confirmed root cause: a `AttributeError:
    # <unknown>.Items` inside _get_storage_item() while a large real
    # transfer was in progress, with a concurrent /api/devices request in
    # the server log at the same moment. Fixed by having request threads
    # only ever read get_known_devices() (a plain, lock-protected list --
    # no COM, no cross-thread object access at all).

    def refresh_devices(self, conn) -> list[DeviceInfo]:
        """Discovery (mode-specific) + liveness probe (mode-agnostic: try
        connect() on every known backend, exactly the same reachability
        check queue_worker.run_worker_once() already relies on) + devices
        table bookkeeping. Called once per worker loop tick, from the
        worker thread only -- see the class-level note above. Also updates
        the cache get_known_devices() reads.

        UI-007: for every reachable device, also (a) loads its manual
        storage-name overrides from device_storage_mappings and hands them
        to the backend (cheap -- a dict of small strings, no COM involved
        in the load itself; see MtpBackend.set_storage_overrides()'s own
        docstring on why the BACKEND never queries SQLite directly) every
        single tick, and (b) refreshes the storage snapshot cache
        get_known_storages() reads, but only every
        storage_refresh_interval_seconds -- that part DOES call
        list_storages(), which on a real backend is a live, comparatively
        expensive COM round-trip per storage (System.FreeSpace/
        System.Capacity), so it is deliberately not done on every tick."""
        self._discover_devices(self.registry)
        with self._device_cache_lock:
            previously_live = {device.device_id for device in self._device_cache}
        live: list[DeviceInfo] = []
        should_refresh_storage = (
            time.monotonic() - self._last_storage_refresh_monotonic >= self.storage_refresh_interval_seconds
        )
        storage_snapshot: dict[str, list] = {}
        installed_games_snapshot: dict[str, set[str]] = {}
        for device_id in self.registry.known_device_ids():
            backend = self.registry.get(device_id)
            try:
                info = backend.connect()
            except DeviceNotFoundError:
                continue
            live.append(info)
            db.upsert_device_seen(conn, info.device_id, info.name)

            overrides = {
                row["raw_storage_name"]: row["logical_name"]
                for row in db.list_device_storage_mappings(conn, device_id)
            }
            backend.set_storage_overrides(overrides)

            if should_refresh_storage or device_id not in previously_live:
                try:
                    storage_snapshot[device_id] = backend.list_storages()
                except Exception:  # noqa: BLE001 -- one bad device must not skip the rest
                    log.exception("failed to refresh storage snapshot for a device")
                installed_games_snapshot[device_id] = set()
                try:
                    ids = backend.list_installed_title_ids()
                    if ids is not None:
                        installed_games_snapshot[device_id] = ids
                except Exception:  # noqa: BLE001 -- one bad device must not skip the rest
                    log.exception("failed to refresh installed-games snapshot for a device")
        with self._device_cache_lock:
            self._device_cache = live
        if should_refresh_storage:
            self._last_storage_refresh_monotonic = time.monotonic()
        if storage_snapshot:
            with self._storage_cache_lock:
                # Only devices actually seen live this pass are updated --
                # a device that dropped out keeps its last-known snapshot
                # rather than being wiped, same "stale but honest, never
                # silently gone" convention as _device_cache/devices list
                # elsewhere in this class.
                self._storage_cache.update(storage_snapshot)
        with self._installed_games_cache_lock:
            live_ids = {device.device_id for device in live}
            self._installed_games_cache = {key: value for key, value in self._installed_games_cache.items() if key in live_ids}
            self._installed_games_cache.update(installed_games_snapshot)
        return live

    def get_known_installed_title_ids(self) -> set[str]:
        """Union, across every currently-known device's most recent
        snapshot, of BASE title ids (title_id.classify_title_variant) DBI's
        own "InstalledApplications.csv" (see MtpBackend.
        list_installed_title_ids()) reports as installed right now -- exact
        TITLE_ID data, not a name guess. A plain cache read, no COM, safe
        from any HTTP request thread (see refresh_devices()'s own class-
        level threading note above it). Empty set if no connected device
        currently exposes that node/file (mock mode, a real device with the
        DBI setting off, or nothing connected at all) -- library rendering
        must treat that as "no information", never as "nothing is
        installed". Purely an ADDITIVE "confirmed present on a console
        right now" signal layered on top of the existing job-history-based
        INSTALLED/INSTALLED_UNVERIFIED status (services.py's
        _JOB_STATUS_TO_DISPLAY) -- never a replacement for it, the two
        answer genuinely different questions (what SwitchAgent itself once
        sent, vs. what DBI reports is there right now)."""
        with self._installed_games_cache_lock:
            return {tid for ids in self._installed_games_cache.values() for tid in ids}

    def get_known_storages(self, device_id: str) -> list:
        """What every HTTP request thread calls to read a device's
        storages -- a plain cache read, no COM involved, exactly like
        get_known_devices() below. At most
        ~storage_refresh_interval_seconds stale. Empty list if this device
        has never been successfully refreshed yet (never guesses)."""
        with self._storage_cache_lock:
            return list(self._storage_cache.get(device_id, []))

    def get_known_devices(self) -> list[DeviceInfo]:
        """What every HTTP request thread calls instead of
        refresh_devices() -- a plain read of the worker thread's last
        observation, no COM involved. At most ~worker_poll_interval_seconds
        stale; while a transfer is in progress the worker thread is busy
        and this simply keeps returning whatever it last saw (the device
        was reachable right before the transfer started) rather than
        blocking or guessing -- a real disconnect mid-transfer still shows
        up promptly and correctly via that job's own INTERRUPTED status,
        not via this cache."""
        with self._device_cache_lock:
            return list(self._device_cache)

    # -- background worker ------------------------------------------------

    def start_worker(self) -> None:
        if self._worker_thread is not None:
            return
        self._stop_event.clear()
        self.preparations.stop.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="switchagent-worker", daemon=True)
        self._worker_thread.start()

    def stop_worker(self) -> None:
        self.preparations.stop.set()
        self._stop_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=10.0)
            self._worker_thread = None

    @property
    def worker_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    def _touch_worker_heartbeat(self) -> None:
        with self._worker_heartbeat_lock:
            self._worker_last_heartbeat_at = db.now_iso()

    @property
    def worker_last_heartbeat_at(self) -> Optional[str]:
        with self._worker_heartbeat_lock:
            return self._worker_last_heartbeat_at

    def request_worker_restart(self) -> None:
        """UI-006 'Restart worker when safe'. NEVER interrupts an in-flight
        COM/MTP call -- run_worker_once() always runs at most one job to
        one of its own terminal outcomes (or finds nothing to do) before
        returning control to _worker_loop, and this flag is only ever
        checked at the very TOP of the loop's NEXT iteration, after that
        return has already happened. No Thread.kill()/TerminateThread,
        nothing forcibly torn down -- just a clean, in-place reopen of the
        worker's own DB connection at its own next safe point.

        If the worker is genuinely wedged inside a single blocking COM
        call that never returns at all, this flag will simply never be
        reached -- there is no way to safely interrupt that (real MTP/Shell
        COM calls have no cancellation mechanism this project's backend
        uses, see mtp/windows.py's module docstring on PerformOperations()'s
        own blocking behavior). is_worker_restart_pending() lets the UI
        honestly show "queued, waiting for a safe point" rather than
        implying this always resolves quickly; a manual application
        restart may still be needed in that case, and the UI must say so,
        never claim this button fixes a truly hung call."""
        self._worker_restart_requested.set()

    @property
    def is_worker_restart_pending(self) -> bool:
        return self._worker_restart_requested.is_set()

    def _worker_loop(self) -> None:
        conn = db.get_connection(self.db_path)
        db.init_db(conn)
        recovered = db.recover_stale_running_jobs(conn)
        if recovered:
            log.info("worker startup: recovered %d stale RUNNING job(s) -> INTERRUPTED", recovered)
        try:
            while not self._stop_event.is_set():
                if self._worker_restart_requested.is_set():
                    self._worker_restart_requested.clear()
                    log.info("worker restart requested -- reopening DB connection at a safe point")
                    conn.close()
                    conn = db.get_connection(self.db_path)
                    db.init_db(conn)
                self._touch_worker_heartbeat()
                try:
                    self.refresh_devices(conn)
                    if self.worker_paused.is_set():
                        self._stop_event.wait(1.0)
                        continue
                    outcome = queue_worker.run_worker_once(conn, self.registry)
                except Exception:  # noqa: BLE001 -- one bad iteration must not kill the worker thread
                    log.exception("worker loop iteration failed")
                    outcome = None
                if outcome is None:
                    self._stop_event.wait(self.worker_poll_interval_seconds)
        finally:
            conn.close()

    # -- library scan (point 16: must not block the UI) --------------------

    def run_scan_in_background(self) -> bool:
        """Returns False without starting anything if a scan is already
        running -- callers (the /api/scan route) surface that as "scan
        already in progress", never queue up a second concurrent scan."""
        with self.scan_lock:
            if self.scan_state.running:
                return False
            self.scan_state = ScanState(running=True, started_at=db.now_iso())

        def _on_file(path: str) -> None:
            with self.scan_lock:
                self.scan_state.current_filename = path

        def _run():
            from .. import scanner
            try:
                conn = db.get_connection(self.db_path)
                db.init_db(conn)
                try:
                    summary = scanner.scan_library_once(conn, on_file=_on_file)
                finally:
                    conn.close()
                with self.scan_lock:
                    self.scan_state.summary = summary
                    self.scan_state.error = summary.get("error")
            except Exception as exc:  # noqa: BLE001 -- must not crash the thread silently
                log.exception("library scan failed")
                with self.scan_lock:
                    self.scan_state.error = str(exc)
            finally:
                with self.scan_lock:
                    self.scan_state.running = False
                    self.scan_state.finished_at = db.now_iso()
                    self.scan_state.current_filename = None

        threading.Thread(target=_run, name="switchagent-scan", daemon=True).start()
        return True

    def scan_status_snapshot(self) -> dict:
        """The full GET /api/scan/status payload (point 11). Adds an
        explicit, derived `status` ("idle"/"running"/"completed"/"failed" --
        ScanState itself only tracks the raw booleans/timestamps a scan
        naturally produces, not a single summary label) and `elapsed_seconds`
        (computed against "now" while still running, fixed once finished) on
        top of the pre-existing raw fields, which stay unchanged for
        whatever already reads them directly (e.g. library.js's
        pollScanStatus, which only ever looked at `running`/`error`/
        `summary`)."""
        with self.scan_lock:
            state = self.scan_state
            running, error = state.running, state.error
            started_at, finished_at = state.started_at, state.finished_at
            summary, current_filename = state.summary, state.current_filename

        if started_at is None:
            status = "idle"
        elif running:
            status = "running"
        elif error:
            status = "failed"
        else:
            status = "completed"

        elapsed_seconds = None
        if started_at is not None:
            start_dt = datetime.fromisoformat(started_at)
            end_dt = datetime.fromisoformat(finished_at) if finished_at else datetime.now(timezone.utc)
            elapsed_seconds = (end_dt - start_dt).total_seconds()

        return {
            "status": status, "running": running, "summary": summary, "error": error,
            "started_at": started_at, "finished_at": finished_at,
            "current_filename": current_filename, "elapsed_seconds": elapsed_seconds,
        }

    # -- library filesystem watcher (point 12) ------------------------------
    #
    # cli.py's `switch-agent scan --watch` already does exactly this same
    # watchdog + debounce trick, but for config.INBOX_DIR (the older,
    # separate Stage-2 CLI pipeline, via scanner.scan_once()) as a
    # standalone blocking process with its own DB connection -- it is
    # deliberately left untouched here (a working, independent feature;
    # nothing about it needs to change). The Web UI's own Library folder
    # (config.LIBRARY_DIR) had NO automatic watcher at all before this --
    # only the manual Rescan button (run_scan_in_background(), point 16) --
    # found while auditing this exact point. This reuses BOTH the proven
    # watchdog+debounce pattern AND (unlike the CLI path) Phase 8's actual
    # async scan machinery instead of a second, separate scan call: a
    # debounced filesystem change simply calls run_scan_in_background(),
    # the exact same method POST /api/scan calls -- so "scanning never
    # creates a job" and "never blocks other pages" (points 13/16) hold
    # here for free, already proven by that method's own tests, and if a
    # manual Rescan is already in flight when the debounce fires,
    # run_scan_in_background()'s own "already running -> False" guard
    # applies exactly as it would to a second concurrent manual click.

    def start_library_watcher(self) -> None:
        """No-op if already running (mirrors start_worker()'s own
        idempotence) or if config.LIBRARY_DIR doesn't currently exist
        (mirrors scan_library_once()'s own tolerance of a missing
        directory -- watchdog's Observer.schedule() itself has no such
        tolerance and would raise, so this must check first rather than
        let that surprise a server startup)."""
        if self._watcher_observer is not None:
            return
        from .. import preferences
        if not preferences.load()["auto_scan"]:
            return
        roots = [root for root in config.library_dirs() if root.is_dir()]
        if not roots:
            log.warning("library watcher: %s does not exist yet, not watching", config.LIBRARY_DIR)

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        state = {"last_event": 0.0, "pending": False}

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # noqa: ANN001 -- watchdog's own signature
                state["last_event"] = time.time()
                state["pending"] = True

        observer = Observer()
        for root in roots:
            observer.schedule(_Handler(), str(root), recursive=True)
        observer.start()
        self._watcher_observer = observer
        self._watcher_stop_event = threading.Event()

        def _debounce_loop():
            stop_event = self._watcher_stop_event
            last_scan = time.monotonic()
            while not stop_event.is_set():
                stop_event.wait(1.0)
                if stop_event.is_set():
                    break
                due = time.monotonic() - last_scan >= preferences.load()["scan_interval"]
                changed = state["pending"] and (time.time() - state["last_event"]) >= self.library_watch_debounce_seconds
                if due or changed:
                    if self.run_scan_in_background():
                        state["pending"] = False
                        last_scan = time.monotonic()

        self._watcher_thread = threading.Thread(target=_debounce_loop, name="switchagent-watcher", daemon=True)
        self._watcher_thread.start()

    def stop_library_watcher(self) -> None:
        if self._watcher_observer is not None:
            self._watcher_observer.stop()
            self._watcher_observer.join()
            self._watcher_observer = None
        if self._watcher_stop_event is not None:
            self._watcher_stop_event.set()
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=5.0)
            self._watcher_thread = None
        self._watcher_stop_event = None

    @property
    def library_watcher_running(self) -> bool:
        return self._watcher_observer is not None


def _discover_real_devices(registry: queue_worker.DeviceRegistry) -> None:
    from ..mtp.windows import RealMtpBackend, enumerate_devices

    for info in enumerate_devices():
        if registry.get(info.device_id) is None:
            registry.register(info.device_id, RealMtpBackend(device_id=info.device_id))


def build_real_context(db_path: Path) -> WebContext:
    return WebContext(db_path=db_path, registry=queue_worker.DeviceRegistry(), discover_devices=_discover_real_devices)


def build_mock_context(db_path: Path, *, device_ids: Optional[list[str]] = None) -> WebContext:
    """Development/test mode -- no real hardware, no pywin32. Pre-registers
    a small fixed set of MockMtpBackend instances (default: two, named the
    same way tests/test_queue_worker.py already does, for a consistent
    "parent + child Switch" story across the codebase) so discover_devices
    has nothing to do at runtime -- the registry is already complete."""
    from ..mtp.mock import MockMtpBackend

    registry = queue_worker.DeviceRegistry()
    ids = device_ids or ["mock-switch-parent", "mock-switch-child"]
    names = {"mock-switch-parent": "Parent's Switch (mock)", "mock-switch-child": "Child's Switch (mock)"}
    for device_id in ids:
        backend = MockMtpBackend(device_id=device_id, device_name=names.get(device_id, device_id))
        backend.add_storage("SD_CARD")
        backend.add_storage("SD_INSTALL")
        registry.register(device_id, backend)

    def _noop_discover(_registry: queue_worker.DeviceRegistry) -> None:
        return None

    return WebContext(db_path=db_path, registry=registry, discover_devices=_noop_discover)
