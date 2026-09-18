"""By explicit request: a device's connection-state change, in EITHER
direction (disconnect OR a fresh/re- connect), invalidates whatever was
queued against its previous session -- no partial/case-by-case survival,
nothing does except the permanent History record. See
services.abandon_all_jobs_for_device() and
PreparationQueue.clear_for_device()'s own docstrings for the reasoning.
"""
import time
import uuid

from switchagent import db
from switchagent.mtp.errors import DeviceNotFoundError
from .test_web_api import web_ctx


def _make_job(conn, *, device_id, status, abandoned=False):
    item_id = db.upsert_library_item(
        conn, absolute_path=f"/lib/{uuid.uuid4().hex}.nsp", item_type="FILE", file_type="NSP",
        size=1, mtime=0.0, content_hash=None, title_id=None, title_id_source=None,
        status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
        target_device_id=device_id, library_item_id=item_id,
    )
    db.update_job_status(conn, job_id, status)
    if abandoned:
        conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (job_id,))
        conn.commit()
    return job_id


def _reset_and_connect(ctx, conn):
    ctx._device_cache = []
    ctx._last_storage_refresh_monotonic = time.monotonic()
    ctx.refresh_devices(conn)


def test_a_leftover_conflict_is_abandoned_the_moment_its_device_reconnects(web_ctx):
    """The real report this whole feature exists for: a DESTINATION_
    CONFLICT job from hours-old testing was still sitting in the Queue,
    live Override/Skip buttons and all, the next time the app (and its
    persisted DB) was reopened with the Switch connected. A fresh
    connect -- including the very first refresh_devices() tick after
    startup, which looks identical to one -- must not let that survive."""
    ctx = web_ctx
    device_id = ctx.registry.known_device_ids()[0]
    with db.open_db(ctx.db_path) as conn:
        job_id = _make_job(conn, device_id=device_id, status="DESTINATION_CONFLICT")
        _reset_and_connect(ctx, conn)
        row = db.get_job(conn, job_id)
    assert row["status"] == "FAILED"
    assert row["abandoned"] == 1
    assert row["finished_at"] is not None


def test_disconnect_abandons_even_a_running_job_and_records_it_to_history(web_ctx, monkeypatch):
    """Unlike cancel_job() (which explicitly refuses to touch RUNNING --
    a live transfer can't be atomically stopped), this must cover RUNNING
    too: the device is already gone by the time this runs, so there's
    nothing left to stop. RUNNING never went through _process_job()
    (queue_worker.py's only other install_history writer), so this must
    record it directly or the outcome is lost entirely."""
    ctx = web_ctx
    device_id = ctx.registry.known_device_ids()[0]
    backend = ctx.registry.get(device_id)
    real_connect = backend.connect
    with db.open_db(ctx.db_path) as conn:
        _reset_and_connect(ctx, conn)  # device is live first
        job_id = _make_job(conn, device_id=device_id, status="RUNNING")
        before = db.list_install_history(conn)

        def absent():
            raise DeviceNotFoundError("unplugged")
        monkeypatch.setattr(backend, "connect", absent)
        ctx.refresh_devices(conn)

        row = db.get_job(conn, job_id)
        after = db.list_install_history(conn)
    assert row["status"] == "FAILED"
    assert row["abandoned"] == 1
    assert len(after) == len(before) + 1
    assert after[0]["job_id"] == job_id
    assert after[0]["outcome"] == "FAILED"
    monkeypatch.setattr(backend, "connect", real_connect)


def test_an_already_recorded_conflict_does_not_get_a_duplicate_history_row(web_ctx):
    """DESTINATION_CONFLICT (and the same set _process_job() already
    records unconditionally the moment it first happens) must NOT be
    recorded a second time here -- only jobs that never reached a
    recorded outcome at all (RUNNING, CONFIRMED, ...) get one written by
    this function."""
    ctx = web_ctx
    device_id = ctx.registry.known_device_ids()[0]
    with db.open_db(ctx.db_path) as conn:
        job_id = _make_job(conn, device_id=device_id, status="DESTINATION_CONFLICT")
        db.record_install_history(
            conn, job_id=job_id, title_id=None, display_name="already recorded",
            target_device_id=device_id, target_storage="SD_INSTALL",
            outcome="DESTINATION_CONFLICT", error="pre-existing entry",
        )
        before = len(db.list_install_history(conn))
        _reset_and_connect(ctx, conn)
        after = len(db.list_install_history(conn))
    assert after == before


def test_a_different_devices_job_is_never_touched(web_ctx, monkeypatch):
    """Only a device whose OWN connection state actually changes on a
    given refresh_devices() tick is affected -- a sibling device that
    was already live before and stays live now must never have its jobs
    swept up just because it happened to share a batch/tick."""
    ctx = web_ctx
    device_ids = ctx.registry.known_device_ids()
    assert len(device_ids) >= 2
    target, other = device_ids[0], device_ids[1]
    target_backend = ctx.registry.get(target)
    real_connect = target_backend.connect
    with db.open_db(ctx.db_path) as conn:
        _reset_and_connect(ctx, conn)  # both devices become "previously live"
        other_job_id = _make_job(conn, device_id=other, status="DESTINATION_CONFLICT")

        def absent():
            raise DeviceNotFoundError("unplugged")
        monkeypatch.setattr(target_backend, "connect", absent)
        ctx.refresh_devices(conn)  # only `target` transitions this tick

        row = db.get_job(conn, other_job_id)
    assert row["status"] == "DESTINATION_CONFLICT"
    assert row["abandoned"] == 0
    monkeypatch.setattr(target_backend, "connect", real_connect)


def test_a_done_job_survives_a_connection_change_untouched(web_ctx):
    ctx = web_ctx
    device_id = ctx.registry.known_device_ids()[0]
    with db.open_db(ctx.db_path) as conn:
        job_id = _make_job(conn, device_id=device_id, status="DONE")
        _reset_and_connect(ctx, conn)
        row = db.get_job(conn, job_id)
    assert row["status"] == "DONE"
    assert row["abandoned"] == 0


def test_disconnect_clears_the_preparation_panel_for_that_device_only(web_ctx, monkeypatch):
    ctx = web_ctx
    device_ids = ctx.registry.known_device_ids()
    target, other = device_ids[0], device_ids[1]
    backend = ctx.registry.get(target)
    real_connect = backend.connect
    with db.open_db(ctx.db_path) as conn:
        _reset_and_connect(ctx, conn)
        ctx.preparations.states["batch-target"] = {
            "id": "batch-target", "phase": "Failed", "started": 0, "target": target, "items": {},
        }
        ctx.preparations.states["batch-other"] = {
            "id": "batch-other", "phase": "Failed", "started": 0, "target": other, "items": {},
        }

        def absent():
            raise DeviceNotFoundError("unplugged")
        monkeypatch.setattr(backend, "connect", absent)
        ctx.refresh_devices(conn)

    assert "batch-target" not in ctx.preparations.states
    assert "batch-other" in ctx.preparations.states
    monkeypatch.setattr(backend, "connect", real_connect)
