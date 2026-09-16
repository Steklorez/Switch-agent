"""PERF-001..004: synthetic scale/stress tests.

Deliberately separate from the per-feature test files -- these are about
"does it stay usable at realistic-worst-case scale", not correctness of
any single feature (already covered elsewhere). Budgets below are
generous on purpose (this project's own docstrings already say library/
queue sizes are expected to be "personal collection" scale, hundreds to
low thousands) -- the goal is to catch a genuine O(N^2)-or-worse blowup,
not to chase micro-optimizations on a healthy O(N) implementation.
"""

from __future__ import annotations

import threading
import time

import pytest

from switchagent import config, db
from switchagent.web import services
from switchagent.web.context import build_mock_context


def _bulk_insert_library_items(conn, rows: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO library_items (
            absolute_path, item_type, file_type, content_type, package_format,
            size, mtime, content_hash, title_id, title_id_source, title_id_confident,
            status, suggested_action, suggested_target, note, error, first_seen_at, last_scanned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def _seed_perf_library(conn, *, base_count: int = 2000) -> dict:
    """Mixed synthetic library: base games (each with an update), a DLC on
    every 4th base, an atmosphere mod on every 10th base, a duplicate copy
    on every 50th base, plus a block of ERROR and NEEDS_REVIEW (missing
    title_id) rows -- every category the mandate names by name. Comfortably
    exceeds 5,000 total rows for base_count=2000 (2000 base + 2000 update +
    500 dlc + 200 mods + 40 duplicates + 300 error + 300 needs_review =
    5340)."""
    rows = []
    ts = "2026-01-01T00:00:00+00:00"
    counts = {"base": 0, "update": 0, "dlc": 0, "mod": 0, "duplicate": 0, "error": 0, "needs_review": 0}

    for i in range(base_count):
        base_value = 0x0100000000010000 + i * 0x100000
        base_hex = f"{base_value:016X}"
        update_hex = f"{base_value + 0x800:016X}"

        rows.append((
            f"D:\\Download\\Game{i} [{base_hex}][v0].nsp", "FILE", "NSP", "GAME_PACKAGE", "NSP",
            1_000_000, 0.0, f"hash-base-{i}", base_hex, "filename", 1,
            "AVAILABLE", "INSTALL_VIA_DBI", "SD_INSTALL", None, None, ts, ts,
        ))
        counts["base"] += 1
        rows.append((
            f"D:\\Download\\Game{i} Update [{update_hex}][v65536].nsp", "FILE", "NSP", "GAME_PACKAGE", "NSP",
            50_000, 0.0, f"hash-update-{i}", update_hex, "filename", 1,
            "AVAILABLE", "INSTALL_VIA_DBI", "SD_INSTALL", None, None, ts, ts,
        ))
        counts["update"] += 1

        if i % 4 == 0:
            dlc_hex = f"{base_value + 0x1000 + 0x001:016X}"
            rows.append((
                f"D:\\Download\\Game{i} DLC [{dlc_hex}][v0].nsp", "FILE", "NSP", "GAME_PACKAGE", "NSP",
                20_000, 0.0, f"hash-dlc-{i}", dlc_hex, "filename", 1,
                "AVAILABLE", "INSTALL_VIA_DBI", "SD_INSTALL", None, None, ts, ts,
            ))
            counts["dlc"] += 1

        if i % 10 == 0:
            rows.append((
                f"D:\\Download\\Mods\\Game{i}Mod\\atmosphere\\contents\\{base_hex}", "MOD_FOLDER", "", "ATMOSPHERE_MOD", None,
                5_000, 0.0, f"hash-mod-{i}", base_hex, "structure", 1,
                "AVAILABLE", "COPY_MERGE", "SD_CARD", None, None, ts, ts,
            ))
            counts["mod"] += 1

        if i % 50 == 0:
            rows.append((
                f"D:\\Download\\Game{i} (redownload) [{base_hex}][v0].nsp", "FILE", "NSP", "GAME_PACKAGE", "NSP",
                1_000_000, 0.0, f"hash-base-{i}", base_hex, "filename", 1,  # SAME hash -> duplicate content
                "AVAILABLE", "INSTALL_VIA_DBI", "SD_INSTALL", None, None, ts, ts,
            ))
            counts["duplicate"] += 1

    for i in range(300):
        rows.append((
            f"D:\\Download\\Corrupt{i}.nsp", "FILE", "NSP", None, None,
            0, 0.0, None, None, None, 0,
            "ERROR", None, None, None, f"extraction failed: bad archive #{i}", ts, ts,
        ))
        counts["error"] += 1
    for i in range(300):
        rows.append((
            f"D:\\Download\\Mystery{i}.nsp", "FILE", "NSP", "GAME_PACKAGE", "NSP",
            10_000, 0.0, f"hash-mystery-{i}", None, None, 0,
            "NEEDS_REVIEW", None, None, "could not determine TITLE_ID", None, ts, ts,
        ))
        counts["needs_review"] += 1

    _bulk_insert_library_items(conn, rows)
    counts["total"] = len(rows)
    return counts


@pytest.fixture
def perf_ctx(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    library_dir = tmp_path / "library"
    work_dir = tmp_path / "work"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "WORK_DIR", work_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")

    db_path = tmp_path / "perf.db"
    with db.open_db(db_path) as conn:
        pass  # just run schema/migrations once, same as test_web_api.py's web_ctx fixture

    ctx = build_mock_context(db_path)
    with db.open_db(db_path) as conn:
        ctx.refresh_devices(conn)
    yield ctx


# ---------------------------------------------------------------------------
# PERF-001: library scale
# ---------------------------------------------------------------------------

def test_perf001_library_view_handles_5000_plus_mixed_rows_within_budget(perf_ctx):
    with db.open_db(perf_ctx.db_path) as conn:
        counts = _seed_perf_library(conn, base_count=2000)
        assert counts["total"] >= 5000  # sanity: the mandate's own floor

        # A slice of jobs (not PERF-002's job-scale itself) targeting real
        # devices, specifically to exercise services.device_label()'s
        # per-item DB lookup inside _library_entry_view() at scale.
        items = db.list_library_items(conn)
        for row in items[:500]:
            db.create_job(
                conn, library_item_id=row["id"], action="INSTALL_VIA_DBI",
                target_storage="SD_INSTALL", target_device_id="mock-switch-parent",
            )

        t0 = time.monotonic()
        flat = services.list_library(conn)
        t1 = time.monotonic()
        grouped = services.list_library_view(conn, kind="games")
        t2 = time.monotonic()

    assert len(flat) == counts["total"]
    assert len(grouped["games"]) > 0

    flat_seconds = t1 - t0
    grouped_seconds = t2 - t1
    # Generous budget: catches a genuine O(N^2)-class blowup (which would
    # show up as many seconds, not a fraction of one), not a micro-perf
    # regression. See module docstring.
    assert flat_seconds < 5.0, f"list_library() took {flat_seconds:.2f}s for {counts['total']} rows"
    assert grouped_seconds < 5.0, f"list_library_view() took {grouped_seconds:.2f}s for {counts['total']} rows"


def test_perf001_library_search_and_filter_still_correct_at_scale(perf_ctx):
    """Scale must not silently break correctness -- filters/search still
    return exactly what they should, not just "something fast"."""
    with db.open_db(perf_ctx.db_path) as conn:
        _seed_perf_library(conn, base_count=2000)

        errors = services.list_library(conn, status_filter="needs_review")
        needs_review = [e for e in errors if e["status"] == "NEEDS_REVIEW"]
        assert len(needs_review) == 300

        # The exact TITLE_ID of item i=7's base -- unambiguous, unlike a
        # bare "Game7" (which would also match Game70..Game799 etc; note
        # services.list_library()'s search needle is .strip()'d, so a
        # trailing space in the search text does not disambiguate the way
        # it might first appear to). The update variant has a DIFFERENT
        # title_id (base + 0x800) and its filename never embeds the base's
        # hex string, so exactly one row -- the base itself -- must match.
        target_title_id = f"{0x0100000000010000 + 7 * 0x100000:016X}"
        found = services.list_library(conn, search=target_title_id)
        assert len(found) == 1
        assert found[0]["title_id"] == target_title_id


# ---------------------------------------------------------------------------
# W3-003: the new search / filter / sort capability, re-tested at the SAME
# 5,000+ row scale PERF-001 established (reusing _seed_perf_library above
# rather than a second generator). The specific regression this guards is
# PERF-001's own: queue_worker.find_family_base_name_source() re-querying
# and re-classifying the WHOLE library table once per row. The new
# family-level filters must likewise read install_history ONCE per call,
# never once per family.
# ---------------------------------------------------------------------------

def _seed_perf_activity(conn, *, row_count: int = 600) -> None:
    """install_history rows spread over both mock devices and every outcome
    the new filters key off -- so the activity index actually has work to
    do rather than trivially short-circuiting on an empty table."""
    items = db.list_library_items(conn)
    outcomes = ["DONE", "DONE_UNVERIFIED", "DONE_UNVERIFIED", "FAILED", "INTERRUPTED", "DESTINATION_CONFLICT"]
    devices = ["mock-switch-parent", "mock-switch-child"]
    for i, row in enumerate(items[:row_count]):
        if not row["title_id"]:
            continue
        device_id = devices[i % 2]
        outcome = outcomes[i % len(outcomes)]
        job_id = db.create_job(
            conn, library_item_id=row["id"], action="INSTALL_VIA_DBI",
            target_storage="SD_INSTALL", target_device_id=device_id,
        )
        db.update_job_status(conn, job_id, outcome)
        history_id = db.record_install_history(
            conn, job_id=job_id, title_id=row["title_id"], display_name=f"perf-{i}",
            target_device_id=device_id, target_storage="SD_INSTALL", outcome=outcome, bytes_total=1,
        )
        # Every third DONE_UNVERIFIED gets a user confirmation, so the
        # "unconfirmed" filter has to actually discriminate.
        if outcome == "DONE_UNVERIFIED" and i % 3 == 0:
            db.set_user_verified_outcome(conn, history_id, "SUCCESS" if i % 6 == 0 else "FAILED")


def test_w3003_search_filter_and_sort_stay_within_budget_at_5000_plus_rows(perf_ctx):
    """Wall-clock ceilings, in the same style (and with the same generous
    budget) as PERF-001's own assertions above -- catching an O(N^2)-class
    blowup, which would show up as many seconds, not a fraction of one."""
    with db.open_db(perf_ctx.db_path) as conn:
        counts = _seed_perf_library(conn, base_count=2000)
        assert counts["total"] >= 5000  # the mandate's own floor, unchanged
        _seed_perf_activity(conn)

        timings = {}
        for label, kwargs in (
            ("search_name", {"search": "Game1234"}),
            ("search_base_title_id", {"search": f"{0x0100000000010000 + 7 * 0x100000:016X}"}),
            ("filter_duplicates", {"group_filter": "duplicates"}),
            ("filter_base", {"group_filter": "base"}),
            ("sort_size", {"sort": "size"}),
            ("sort_last_scanned", {"sort": "last_scanned"}),
            ("sort_name", {"sort": "name"}),
            ("combined", {
                "search": "Game", "group_filter": "duplicates", "sort": "size",
            }),
        ):
            t0 = time.monotonic()
            view = services.list_library_view(conn, kind="games", **kwargs)
            timings[label] = time.monotonic() - t0
            assert "games" in view

    # Printed (visible under `pytest -s`) so a future regression's real
    # numbers are recoverable without re-instrumenting anything.
    print("\nW3-003 list_library_view timings (%d rows):" % counts["total"])
    for label, seconds in timings.items():
        print(f"  {label:<28} {seconds:.3f}s")
    for label, seconds in timings.items():
        assert seconds < 5.0, f"list_library_view({label}) took {seconds:.2f}s for {counts['total']} rows"


def test_w3003_filters_never_rescan_the_library_or_history_per_family(perf_ctx):
    """The concrete PERF-001 regression guard, asserted structurally rather
    than only by wall clock: rendering the WHOLE grouped view must read
    library_items a small, constant number of times -- never once per
    family (there are ~2,300 families in this corpus, so a per-family read
    would show up instantly as a count in the thousands)."""
    from switchagent import queue_worker

    with db.open_db(perf_ctx.db_path) as conn:
        _seed_perf_library(conn, base_count=2000)
        _seed_perf_activity(conn)

        calls = {"library_items": 0, "family_name_source": 0}
        real_list_library_items = db.list_library_items
        real_find_source = queue_worker.find_family_base_name_source

        def counting_library_items(c):
            calls["library_items"] += 1
            return real_list_library_items(c)

        def counting_find_source(c, family_title_id, *, library_items=None):
            calls["family_name_source"] += 1
            # The regression PERF-001 fixed was this function silently
            # re-reading the entire table when a bulk caller forgot to pass
            # library_items -- fail loudly here if that ever comes back.
            assert library_items is not None, (
                "find_family_base_name_source() must be given the already-fetched library_items "
                "by any caller iterating the whole library (PERF-001)"
            )
            return real_find_source(c, family_title_id, library_items=library_items)

        db.list_library_items = counting_library_items
        queue_worker.find_family_base_name_source = counting_find_source
        # services.py imports these as module attributes of db/queue_worker,
        # so patching them there is what the call sites actually see.
        try:
            services.list_library_view(conn, kind="games", group_filter="base", sort="size")
        finally:
            db.list_library_items = real_list_library_items
            queue_worker.find_family_base_name_source = real_find_source

    assert calls["library_items"] <= 2, f"library_items read {calls['library_items']} times"
    # Lower bound: an upper-bound-only assertion here would also pass if the
    # monkeypatched instrumentation above silently stopped intercepting the
    # real call (e.g. a future refactor changing services.py to
    # `from ..db import list_library_items` instead of the current
    # `db.list_library_items(...)` module-attribute call the monkeypatch
    # relies on) -- the counter would stay at 0 and this test would keep
    # "passing" while measuring nothing. Prove it's actually wired to a real
    # call site.
    assert calls["library_items"] >= 1, "library_items instrumentation never fired -- monkeypatch silently disarmed"
    assert calls["family_name_source"] > 0, "find_family_base_name_source instrumentation never fired -- monkeypatch silently disarmed"


# ---------------------------------------------------------------------------
# PERF-002: queue scale
# ---------------------------------------------------------------------------

def _seed_perf_queue(conn, ctx, *, job_count: int = 400) -> dict:
    """Hundreds of jobs spread across batches, both mock devices, a mix of
    terminal/waiting/failed statuses, and real install_history rows --
    everything the mandate names by name."""
    ts = "2026-01-01T00:00:00+00:00"
    item_rows = []
    for i in range(job_count):
        item_rows.append((
            f"D:\\Download\\QItem{i} [{0x0100000000020000 + i * 0x100000:016X}][v0].nsp",
            "FILE", "NSP", "GAME_PACKAGE", "NSP", 1000, 0.0, f"qhash-{i}",
            f"{0x0100000000020000 + i * 0x100000:016X}", "filename", 1,
            "AVAILABLE", "INSTALL_VIA_DBI", "SD_INSTALL", None, None, ts, ts,
        ))
    _bulk_insert_library_items(conn, item_rows)
    item_ids = [row["id"] for row in db.list_library_items(conn)]

    devices = ["mock-switch-parent", "mock-switch-child"]
    statuses_cycle = [
        "CONFIRMED", "WAITING_FOR_DEVICE", "WAITING_FOR_BASE", "RUNNING",
        "DONE", "DONE_UNVERIFIED", "FAILED", "DEVICE_UNAVAILABLE",
    ]
    batch_ids = [db.create_installation_batch(conn, target_device_id=devices[i % 2]) for i in range(job_count // 10 + 1)]

    for i, item_id in enumerate(item_ids):
        job_id = db.create_job(
            conn, library_item_id=item_id, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
            target_device_id=devices[i % 2], batch_id=batch_ids[i % len(batch_ids)],
        )
        status = statuses_cycle[i % len(statuses_cycle)]
        db.update_job_status(conn, job_id, status)
        if status in ("DONE", "DONE_UNVERIFIED", "FAILED"):
            db.record_install_history(
                conn, job_id=job_id, title_id=f"{0x0100000000020000 + i * 0x100000:016X}",
                display_name=f"QItem{i}", target_device_id=devices[i % 2], target_storage="SD_INSTALL",
                outcome=status if status != "FAILED" else "FAILED", bytes_total=1000,
            )
    return {"job_count": job_count}


def test_perf002_queue_grouped_and_history_grouped_handle_hundreds_of_jobs(perf_ctx):
    with db.open_db(perf_ctx.db_path) as conn:
        seeded = _seed_perf_queue(conn, perf_ctx, job_count=400)

        t0 = time.monotonic()
        groups = services.list_queue_grouped(conn)
        t1 = time.monotonic()
        history = services.list_history_grouped(conn, limit=1000)
        t2 = time.monotonic()

    total_jobs_in_groups = sum(g["total"] for g in groups)
    assert total_jobs_in_groups > 0
    assert len(history) > 0

    queue_seconds = t1 - t0
    history_seconds = t2 - t1
    assert queue_seconds < 5.0, f"list_queue_grouped() took {queue_seconds:.2f}s for {seeded['job_count']} jobs"
    assert history_seconds < 5.0, f"list_history_grouped() took {history_seconds:.2f}s for {seeded['job_count']} jobs"


def test_perf002_dependency_query_stays_fast_with_many_outstanding_jobs(perf_ctx):
    """PERF-002 calls out dependency queries specifically -- _has_outstanding_base_job()
    runs an IN(...) scan over all non-terminal jobs for a single device on
    EVERY confirmed-jobs pass; must not degrade badly as that set grows."""
    from switchagent import queue_worker

    with db.open_db(perf_ctx.db_path) as conn:
        _seed_perf_queue(conn, perf_ctx, job_count=400)

        t0 = time.monotonic()
        result = queue_worker._has_outstanding_base_job(conn, "mock-switch-parent", "0100000000020000")
        t1 = time.monotonic()

    assert isinstance(result, bool)
    assert (t1 - t0) < 1.0


# ---------------------------------------------------------------------------
# PERF-003: watcher event storm
# ---------------------------------------------------------------------------

def test_perf003_watcher_survives_an_event_storm_with_single_flight_and_clean_shutdown(perf_ctx, monkeypatch):
    """Real watchdog Observer, real filesystem writes -- a storm of 500
    rapid EVENTS (repeated rewrites of a small, fixed set of files, not
    500 distinct files) in config.LIBRARY_DIR in quick succession, the
    actual event source, not a synthetic call into a private trigger.
    Deliberately a SMALL number of distinct files: scanner.is_file_stable()
    does a real, intentional ~2s-per-candidate-file sleep by design (see
    test_web_api.py's test_scan_reports_current_filename_progress_end_to_end
    for this project's own established convention of accepting that real
    cost rather than working around it) -- this test's own subject is
    debounce/single-flight collapsing an EVENT storm, which is orthogonal
    to per-file scan cost and would otherwise be swamped by it. Checks:
    debounce collapses ~500 events into a small, bounded number of actual
    scans, the final state is fully scanned (nothing lost), and shutdown
    afterward is clean with no thread-count explosion or leak."""
    scan_calls = []
    scan_lock = threading.Lock()
    real_scan_library_once = None

    def _counting_scan_once(conn, **kwargs):
        with scan_lock:
            scan_calls.append(time.monotonic())
        return real_scan_library_once(conn, **kwargs)

    from switchagent import scanner as scanner_mod

    real_scan_library_once = scanner_mod.scan_library_once
    monkeypatch.setattr(scanner_mod, "scan_library_once", _counting_scan_once)

    perf_ctx.library_watch_debounce_seconds = 0.3
    file_count = 10
    rewrites_per_file = 50  # 10 x 50 = 500 raw filesystem events

    threads_before = threading.active_count()
    perf_ctx.start_library_watcher()
    try:
        paths = [
            config.LIBRARY_DIR / f"Storm{i} [{0x0100000000030000 + i * 0x1000:016X}][v0].nsp"
            for i in range(file_count)
        ]
        for _ in range(rewrites_per_file):
            for i, path in enumerate(paths):
                path.write_bytes(f"revision-{_}".encode())

        # Wait for quiescence (debounce fires ~0.3s after the LAST event)
        # plus real scan time for `file_count` files (~2s/file by design,
        # see docstring above) -- generously bounded well past that.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            with db.open_db(perf_ctx.db_path) as conn:
                if len(db.list_library_items(conn)) >= file_count:
                    break
            time.sleep(0.2)
    finally:
        perf_ctx.stop_library_watcher()

    with db.open_db(perf_ctx.db_path) as conn:
        assert len(db.list_library_items(conn)) == file_count  # nothing lost despite the storm

    threads_after = threading.active_count()
    assert len(scan_calls) < 20, f"expected debounce to collapse the storm, got {len(scan_calls)} scans"
    assert threads_after <= threads_before + 1  # no thread-count explosion or leak after shutdown
    assert not perf_ctx.library_watcher_running  # clean shutdown


# ---------------------------------------------------------------------------
# PERF-004: SQLite concurrency
# ---------------------------------------------------------------------------

def test_perf004_parallel_readers_and_a_writer_never_hit_database_is_locked(perf_ctx):
    """Simulates the mandate's own list at once: queue polling, devices
    polling, settings diagnostics, history, and worker DB writes, all
    hitting the SAME on-disk DB file from separate threads/connections
    concurrently. WAL mode + a real busy timeout (see db.get_connection)
    should absorb this without ever raising 'database is locked'."""
    from switchagent import queue_worker

    with db.open_db(perf_ctx.db_path) as conn:
        _seed_perf_queue(conn, perf_ctx, job_count=100)
        # _seed_perf_queue's CONFIRMED/WAITING_FOR_DEVICE/WAITING_FOR_BASE/
        # RUNNING rows are synthetic status labels with no real staged
        # manifest.json behind them (unlike a job created through the real
        # confirm flow, see below) -- fine for PERF-002's read-only view,
        # but a real worker thread picking one up here would hit a genuine
        # FileNotFoundError trying to load a manifest that was never
        # staged, which is not a state reachable through this app's own
        # code and would only mask the "database is locked" question this
        # test actually asks. Neutralized to an inert terminal status so
        # list_confirmed_jobs() never selects them.
        for row in db.list_jobs(conn):
            if row["status"] in ("CONFIRMED", "WAITING_FOR_DEVICE", "WAITING_FOR_BASE", "RUNNING"):
                db.update_job_status(conn, row["id"], "DEVICE_UNAVAILABLE", error="perf test setup")

        # A handful of genuinely real, properly-manifested jobs (via the
        # real confirm flow, exactly like the Web UI would create them) so
        # the concurrent worker thread has real work -- real writes, not a
        # no-op loop -- while the reader threads hammer the same DB file.
        real_item_ids = []
        for i in range(3):
            path = config.LIBRARY_DIR / f"RealJob{i} [{0x0100000000040000 + i * 0x100000:016X}][v0].nsp"
            path.write_bytes(b"real payload")
            item_id = db.upsert_library_item(
                conn, absolute_path=str(path), item_type="FILE", file_type="NSP",
                size=path.stat().st_size, mtime=path.stat().st_mtime, content_hash=f"real-{i}",
                title_id=f"{0x0100000000040000 + i * 0x100000:016X}", title_id_source="filename",
                status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
                content_type="GAME_PACKAGE", package_format="NSP",
            )
            real_item_ids.append(item_id)
        services.create_and_confirm_jobs(conn, real_item_ids, "mock-switch-parent")

    errors: list[str] = []
    stop = threading.Event()

    def _reader_loop(fn):
        while not stop.is_set():
            try:
                with db.open_db(perf_ctx.db_path) as conn:
                    fn(conn)
            except Exception as exc:  # noqa: BLE001 -- capturing for the assertion below
                errors.append(f"{fn.__name__}: {exc}")
                return

    def _queue_poll(conn):
        services.list_queue_grouped(conn)

    def _devices_poll(conn):
        services.list_devices(conn, perf_ctx)

    def _history_poll(conn):
        services.list_history_grouped(conn, limit=200)

    def _diagnostics_poll(conn):
        services.get_diagnostics_report(conn, perf_ctx)

    def _worker_writes():
        try:
            for _ in range(20):
                with db.open_db(perf_ctx.db_path) as conn:
                    queue_worker.run_worker_once(conn, perf_ctx.registry)
        except Exception as exc:  # noqa: BLE001 -- capturing for the assertion below
            errors.append(f"_worker_writes: {exc}")

    threads = [
        threading.Thread(target=_reader_loop, args=(_queue_poll,)),
        threading.Thread(target=_reader_loop, args=(_devices_poll,)),
        threading.Thread(target=_reader_loop, args=(_history_poll,)),
        threading.Thread(target=_reader_loop, args=(_diagnostics_poll,)),
        threading.Thread(target=_worker_writes),
    ]
    for t in threads:
        t.start()
    threads[-1].join(timeout=30)  # the writer thread is finite -- wait for it
    stop.set()
    for t in threads[:-1]:
        t.join(timeout=5)

    assert errors == [], f"concurrent DB access raised: {errors}"
