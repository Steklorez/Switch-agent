"""Regression tests for the two real-hardware bugs found investigating a
Quake II install (see docs/STATE.md for the full incident writeup):

1. An unexpected, non-MtpError exception deep inside a transfer (in the
   real incident: a win32com AttributeError from a COM object invalidated
   by an unrelated thread -- see switchagent/web/context.py's fix) used to
   leave the job stuck at RUNNING forever, invisible in History. Fixed in
   queue_worker.py::_process_job with a broad safety-net exception
   handler.
2. Nothing stopped an Update/DLC/Mod job from becoming RUNNING (and
   actually transferring) before its Base Game job had completed --
   in the real incident, the base game silently never got installed while
   its update did, leaving a broken partial install on the console. Fixed
   in queue_worker.py::run_worker_once via a new install-order dependency
   check (_dependency_status), using title_id.classify_title_variant() +
   install_history.

All tests use MockMtpBackend -- no real hardware.
"""

from __future__ import annotations

import pytest

from switchagent import db, preview, queue_worker
from switchagent.mtp import MockMtpBackend


def _two_backends():
    parent = MockMtpBackend(device_id="mock-switch-parent", device_name="Parent's Switch")
    parent.add_storage("SD_CARD")
    parent.add_storage("SD_INSTALL")
    child = MockMtpBackend(device_id="mock-switch-child", device_name="Child's Switch")
    child.add_storage("SD_CARD")
    child.add_storage("SD_INSTALL")
    registry = queue_worker.DeviceRegistry()
    registry.register("mock-switch-parent", parent)
    registry.register("mock-switch-child", child)
    return parent, child, registry


def _make_library_item(conn, library_dir, name: str, content: bytes, title_id: str) -> int:
    (library_dir / name).write_bytes(content)
    return db.upsert_library_item(
        conn, absolute_path=str(library_dir / name), item_type="FILE", file_type="NSP",
        size=len(content), mtime=0.0, content_hash=f"hash-{name}",
        title_id=title_id, title_id_source="filename", status="AVAILABLE",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        content_type="GAME_PACKAGE", package_format="NSP",
    )


def _make_confirmed_job(conn, library_dir, name: str, item_id: int, device_id: str) -> int:
    report = preview.preview_path(library_dir / name)
    job_id = queue_worker.create_job_from_report(
        conn, report, library_item_id=item_id, action="INSTALL_VIA_DBI", target_device_id=device_id,
    )
    db.confirm_job(conn, job_id)
    return job_id


def _set_created_at(conn, job_id: int, iso: str) -> None:
    """Test-only: forces a deterministic creation order regardless of how
    fast the test itself ran (db.now_iso() only has second resolution)."""
    conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (iso, job_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Bug 1: unexpected exception must never leave a job silently stuck
# ---------------------------------------------------------------------------

def test_unexpected_exception_marks_job_interrupted_not_stuck_forever(isolated_db, monkeypatch):
    """Replays the actual Quake II incident: a raw AttributeError (not an
    MtpError) raised from inside ensure_directory(), the very first call
    _run_job_transfer makes for a file. Must end INTERRUPTED, with a
    History record, never left at RUNNING."""
    conn, library_dir = isolated_db
    from switchagent import config
    parent, _child, registry = _two_backends()

    def _boom(*a, **k):
        raise AttributeError("<unknown>.Items")  # exact real-world error text

    monkeypatch.setattr(parent, "ensure_directory", _boom)

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_library_item(conn, config.LIBRARY_DIR, name, b"content", "0100000000010000")
    job_id = _make_confirmed_job(conn, config.LIBRARY_DIR, name, item_id, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.job_id == job_id
    assert outcome.status == "INTERRUPTED"
    assert "AttributeError" in outcome.error
    assert "Items" in outcome.error

    row = db.get_job(conn, job_id)
    assert row["status"] == "INTERRUPTED"  # never left at RUNNING
    assert row["status"] != "RUNNING"

    history = db.list_install_history(conn)
    assert len(history) == 1
    assert history[0]["outcome"] == "INTERRUPTED"
    assert history[0]["job_id"] == job_id


def test_unexpected_exception_does_not_stop_the_worker_from_reaching_the_next_job(isolated_db, monkeypatch):
    """The worker must recover and keep processing other, independent
    jobs after one unexpectedly blows up -- exactly what let the Quake II
    update job run at all (correctly, on its own) after the base crashed;
    this confirms that continuation is safe now that the crashed job is
    properly terminal instead of silently stuck."""
    conn, library_dir = isolated_db
    from switchagent import config
    parent, _child, registry = _two_backends()

    call_count = {"n": 0}
    real_ensure_directory = parent.ensure_directory

    def _boom_once(*a, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise AttributeError("<unknown>.Items")
        return real_ensure_directory(*a, **k)

    monkeypatch.setattr(parent, "ensure_directory", _boom_once)

    name_a = "GameA [0100000000010000][v0].nsp"
    name_b = "GameB [0100000000020000][v0].nsp"
    item_a = _make_library_item(conn, config.LIBRARY_DIR, name_a, b"A", "0100000000010000")
    item_b = _make_library_item(conn, config.LIBRARY_DIR, name_b, b"B", "0100000000020000")
    job_a = _make_confirmed_job(conn, config.LIBRARY_DIR, name_a, item_a, "mock-switch-parent")
    _set_created_at(conn, job_a, "2026-01-01T00:00:00+00:00")
    job_b = _make_confirmed_job(conn, config.LIBRARY_DIR, name_b, item_b, "mock-switch-parent")
    _set_created_at(conn, job_b, "2026-01-01T00:00:01+00:00")

    outcome1 = queue_worker.run_worker_once(conn, registry)
    assert outcome1.job_id == job_a
    assert outcome1.status == "INTERRUPTED"

    outcome2 = queue_worker.run_worker_once(conn, registry)
    assert outcome2.job_id == job_b
    assert outcome2.status == "DONE"


# ---------------------------------------------------------------------------
# Bug 2: install-order dependency -- Base Game -> Update/DLC/Mod
# ---------------------------------------------------------------------------

_QUAKE2_BASE_TITLE_ID = "010048F0195E8000"
_QUAKE2_UPDATE_TITLE_ID = "010048F0195E8800"  # base | 0x800
_QUAKE2_BASE_NAME = "Quake II [010048F0195E8000][v0].nsz"
_QUAKE2_UPDATE_NAME = "Quake II Update [010048F0195E8800][v196608].nsz"


def test_update_never_becomes_running_before_base_succeeds(isolated_db):
    """The core Quake II regression test: no matter the creation order,
    the update must never reach RUNNING while the base game has not yet
    reached a successful terminal state -- and once the base does
    succeed, the update proceeds normally on its own."""
    conn, library_dir = isolated_db
    from switchagent import config
    parent, _child, registry = _two_backends()

    base_item = _make_library_item(conn, config.LIBRARY_DIR, _QUAKE2_BASE_NAME, b"base payload", _QUAKE2_BASE_TITLE_ID)
    update_item = _make_library_item(
        conn, config.LIBRARY_DIR, _QUAKE2_UPDATE_NAME, b"update payload", _QUAKE2_UPDATE_TITLE_ID
    )
    # Update confirmed and created BEFORE the base -- proves this is
    # enforced by actual install state, not by creation/confirmation order.
    update_job = _make_confirmed_job(conn, config.LIBRARY_DIR, _QUAKE2_UPDATE_NAME, update_item, "mock-switch-parent")
    _set_created_at(conn, update_job, "2026-01-01T00:00:00+00:00")
    base_job = _make_confirmed_job(conn, config.LIBRARY_DIR, _QUAKE2_BASE_NAME, base_item, "mock-switch-parent")
    _set_created_at(conn, base_job, "2026-01-01T00:00:01+00:00")

    for _ in range(10):
        outcome = queue_worker.run_worker_once(conn, registry)
        if outcome is None:
            break
        if outcome.job_id == update_job:
            base_status = db.get_job(conn, base_job)["status"]
            assert base_status in ("DONE", "DONE_UNVERIFIED"), (
                f"update job ran to completion while base was still '{base_status}'"
            )

    assert db.get_job(conn, base_job)["status"] in ("DONE", "DONE_UNVERIFIED")
    assert db.get_job(conn, update_job)["status"] in ("DONE", "DONE_UNVERIFIED")
    # the base must genuinely have been sent -- not just assumed
    assert parent.storage_tree("SD_INSTALL").list_files() == sorted([_QUAKE2_BASE_NAME, _QUAKE2_UPDATE_NAME])


def test_update_marked_waiting_for_base_on_first_check(isolated_db):
    conn, library_dir = isolated_db
    from switchagent import config
    parent, _child, registry = _two_backends()

    base_item = _make_library_item(conn, config.LIBRARY_DIR, _QUAKE2_BASE_NAME, b"base payload", _QUAKE2_BASE_TITLE_ID)
    update_item = _make_library_item(
        conn, config.LIBRARY_DIR, _QUAKE2_UPDATE_NAME, b"update payload", _QUAKE2_UPDATE_TITLE_ID
    )
    update_job = _make_confirmed_job(conn, config.LIBRARY_DIR, _QUAKE2_UPDATE_NAME, update_item, "mock-switch-parent")
    _set_created_at(conn, update_job, "2026-01-01T00:00:00+00:00")
    base_job = _make_confirmed_job(conn, config.LIBRARY_DIR, _QUAKE2_BASE_NAME, base_item, "mock-switch-parent")
    _set_created_at(conn, base_job, "2026-01-01T00:00:01+00:00")

    outcome = queue_worker.run_worker_once(conn, registry)
    # update was checked first (earlier created_at) -- must be parked, not processed
    assert outcome.job_id == base_job  # the worker skips the waiting update and processes the base instead
    update_row = db.get_job(conn, update_job)
    assert update_row["status"] == "WAITING_FOR_BASE"
    # The message must name the BASE it's waiting on, not the update's own
    # TITLE_ID (it used to show manifest.title_id, i.e. the waiting job's
    # OWN id -- exactly backwards, and confusing in the UI).
    assert _QUAKE2_BASE_TITLE_ID in update_row["error"]
    assert _QUAKE2_UPDATE_TITLE_ID not in update_row["error"]


def test_dependent_jobs_blocked_when_base_fails(isolated_db):
    """If the base game's install definitively fails, the update/DLC/mod
    must never auto-run -- and must be clearly, terminally blocked, not
    left waiting forever."""
    conn, library_dir = isolated_db
    from switchagent import config
    parent, _child, registry = _two_backends()
    parent.arm_failure("error", storage="SD_INSTALL", dest_path=_QUAKE2_BASE_NAME)

    base_item = _make_library_item(conn, config.LIBRARY_DIR, _QUAKE2_BASE_NAME, b"base payload", _QUAKE2_BASE_TITLE_ID)
    update_item = _make_library_item(
        conn, config.LIBRARY_DIR, _QUAKE2_UPDATE_NAME, b"update payload", _QUAKE2_UPDATE_TITLE_ID
    )
    base_job = _make_confirmed_job(conn, config.LIBRARY_DIR, _QUAKE2_BASE_NAME, base_item, "mock-switch-parent")
    _set_created_at(conn, base_job, "2026-01-01T00:00:00+00:00")
    update_job = _make_confirmed_job(conn, config.LIBRARY_DIR, _QUAKE2_UPDATE_NAME, update_item, "mock-switch-parent")
    _set_created_at(conn, update_job, "2026-01-01T00:00:01+00:00")

    outcome1 = queue_worker.run_worker_once(conn, registry)
    assert outcome1.job_id == base_job
    assert outcome1.status == "FAILED"

    # A dependency block (like DEVICE_UNAVAILABLE) is discovered via
    # `continue`, not `return` -- run_worker_once() only returns a non-None
    # outcome when it actually reaches _process_job(). With nothing else
    # left in the queue this call correctly returns None; what matters is
    # the job's own persisted state and history, checked below.
    outcome2 = queue_worker.run_worker_once(conn, registry)
    assert outcome2 is None

    row = db.get_job(conn, update_job)
    assert row["status"] == "BLOCKED_BY_DEPENDENCY"
    assert parent.storage_tree("SD_INSTALL").list_files() == []  # update was never actually sent

    history = db.list_install_history(conn)
    blocked_entries = [h for h in history if h["outcome"] == "BLOCKED_BY_DEPENDENCY"]
    assert len(blocked_entries) == 1
    assert blocked_entries[0]["job_id"] == update_job

    # never auto-retried
    outcome3 = queue_worker.run_worker_once(conn, registry)
    assert outcome3 is None

    with pytest.raises(ValueError):
        db.retry_job(conn, update_job)


def test_dependent_dlc_and_mod_also_blocked_by_failed_base(isolated_db):
    conn, library_dir = isolated_db
    from switchagent import config
    parent, _child, registry = _two_backends()
    parent.arm_failure("error", storage="SD_INSTALL", dest_path="Base [0100000000010000][v0].nsp")

    base_item = _make_library_item(
        conn, config.LIBRARY_DIR, "Base [0100000000010000][v0].nsp", b"base", "0100000000010000",
    )
    dlc_item = _make_library_item(
        conn, config.LIBRARY_DIR, "DLC [0100000000011001][v0].nsp", b"dlc", "0100000000011001",
    )
    base_job = _make_confirmed_job(conn, config.LIBRARY_DIR, "Base [0100000000010000][v0].nsp", base_item, "mock-switch-parent")
    _set_created_at(conn, base_job, "2026-01-01T00:00:00+00:00")
    dlc_job = _make_confirmed_job(conn, config.LIBRARY_DIR, "DLC [0100000000011001][v0].nsp", dlc_item, "mock-switch-parent")
    _set_created_at(conn, dlc_job, "2026-01-01T00:00:01+00:00")

    queue_worker.run_worker_once(conn, registry)  # base -> FAILED
    queue_worker.run_worker_once(conn, registry)  # dlc -> BLOCKED_BY_DEPENDENCY

    assert db.get_job(conn, dlc_job)["status"] == "BLOCKED_BY_DEPENDENCY"


def test_independent_titles_do_not_block_each_other(isolated_db):
    """Two unrelated games, no dependency relationship at all -- both
    process normally, sequentially, per the existing single-worker model;
    neither's presence delays or blocks the other."""
    conn, library_dir = isolated_db
    from switchagent import config
    parent, _child, registry = _two_backends()

    item_a = _make_library_item(conn, config.LIBRARY_DIR, "GameA [0100000000010000][v0].nsp", b"A", "0100000000010000")
    item_b = _make_library_item(conn, config.LIBRARY_DIR, "GameB [0100000000020000][v0].nsp", b"B", "0100000000020000")
    job_a = _make_confirmed_job(conn, config.LIBRARY_DIR, "GameA [0100000000010000][v0].nsp", item_a, "mock-switch-parent")
    job_b = _make_confirmed_job(conn, config.LIBRARY_DIR, "GameB [0100000000020000][v0].nsp", item_b, "mock-switch-parent")

    outcome1 = queue_worker.run_worker_once(conn, registry)
    outcome2 = queue_worker.run_worker_once(conn, registry)

    assert {outcome1.job_id, outcome2.job_id} == {job_a, job_b}
    assert db.get_job(conn, job_a)["status"] == "DONE"
    assert db.get_job(conn, job_b)["status"] == "DONE"


def test_atmosphere_mod_also_waits_for_its_base_game(isolated_db):
    """Mods are tagged with the base game's own TITLE_ID directly (no
    variant arithmetic) -- must be gated the same way as updates/DLC."""
    conn, library_dir = isolated_db
    from switchagent import config
    from switchagent import scanner
    parent, _child, registry = _two_backends()

    base_item = _make_library_item(
        conn, config.LIBRARY_DIR, "Base [0100000000010000][v0].nsp", b"base", "0100000000010000",
    )
    mod_dir = config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "asset.bin").write_bytes(b"asset")
    scanner.scan_library_once(conn)  # indexes the mod folder into library_items

    mod_row = conn.execute(
        "SELECT id FROM library_items WHERE content_type = 'ATMOSPHERE_MOD'"
    ).fetchone()
    assert mod_row is not None
    mod_path = str(config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000")

    from switchagent import preview as preview_mod
    mod_report = preview_mod.preview_path(config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000")
    mod_job = queue_worker.create_job_from_report(
        conn, mod_report, library_item_id=mod_row["id"], action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, mod_job)
    _set_created_at(conn, mod_job, "2026-01-01T00:00:00+00:00")

    base_job = _make_confirmed_job(conn, config.LIBRARY_DIR, "Base [0100000000010000][v0].nsp", base_item, "mock-switch-parent")
    _set_created_at(conn, base_job, "2026-01-01T00:00:01+00:00")

    outcome1 = queue_worker.run_worker_once(conn, registry)
    assert outcome1.job_id == base_job  # mod was parked, base processed
    assert db.get_job(conn, mod_job)["status"] == "WAITING_FOR_BASE"

    outcome2 = queue_worker.run_worker_once(conn, registry)
    assert outcome2.job_id == mod_job
    assert outcome2.status == "DONE"


def test_mod_with_no_competing_base_job_is_not_blocked(isolated_db):
    """The practical case the dependency check must NOT break: a mod for a
    game the user already owns/installed some other way (this hardware
    cannot prove what's actually on the console -- no MTP-visible
    installed-games list, see docs/STAGE5A-MTP-RESEARCH.md). With no Base
    Game job competing for the same title in this device's queue, the mod
    must install normally, not wait forever for a base that was never
    going to be confirmed through this tool."""
    conn, library_dir = isolated_db
    from switchagent import config, scanner, preview as preview_mod
    parent, _child, registry = _two_backends()

    mod_dir = config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "asset.bin").write_bytes(b"asset")
    scanner.scan_library_once(conn)

    mod_row = conn.execute("SELECT id FROM library_items WHERE content_type = 'ATMOSPHERE_MOD'").fetchone()
    mod_report = preview_mod.preview_path(
        config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000"
    )
    mod_job = queue_worker.create_job_from_report(
        conn, mod_report, library_item_id=mod_row["id"], action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, mod_job)

    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome.job_id == mod_job
    assert outcome.status == "DONE"


def test_mod_retry_after_its_own_interruption_is_not_mistaken_for_a_failed_base(isolated_db):
    """Regression: a mod's dependency check uses its OWN TITLE_ID as the
    "base" it looks for (mods have no variant arithmetic, unlike
    updates/DLC) -- without excluding the mod's own SD_CARD history rows,
    a mod's first INTERRUPTED attempt made its own retry look like "the
    base game failed" (found via a real regression in
    test_stage41_recovery.py while building this feature). Mods always
    target SD_CARD, base games always target SD_INSTALL -- that's what
    now keeps the two apart."""
    conn, library_dir = isolated_db
    from switchagent import config, scanner, preview as preview_mod
    parent, _child, registry = _two_backends()
    parent.arm_failure("disconnect", dest_path="atmosphere/contents/0100000000010000/romfs/asset.bin")

    mod_dir = config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "asset.bin").write_bytes(b"asset")
    scanner.scan_library_once(conn)

    mod_row = conn.execute("SELECT id FROM library_items WHERE content_type = 'ATMOSPHERE_MOD'").fetchone()
    mod_report = preview_mod.preview_path(
        config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000"
    )
    mod_job = queue_worker.create_job_from_report(
        conn, mod_report, library_item_id=mod_row["id"], action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, mod_job)

    outcome1 = queue_worker.run_worker_once(conn, registry)
    assert outcome1.status == "INTERRUPTED"

    db.retry_job(conn, mod_job)
    outcome2 = queue_worker.run_worker_once(conn, registry)
    assert outcome2 is not None, "the retry must not be silently swallowed as BLOCKED_BY_DEPENDENCY"
    assert outcome2.job_id == mod_job
    assert outcome2.status == "DONE"
