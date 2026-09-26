"""Tests for the multi-device queue worker (switchagent/queue_worker.py).

Core rule under test throughout: a job's target_device_id is fixed at
creation and NEVER substituted for a different, currently-available device
-- see docs/STAGE4.md and docs/STAGE4.1.md. Every test that has two devices
deliberately checks BOTH that the right device got the file AND that the
wrong device did not.

Jobs are created through queue_worker.create_job_from_report() (stage
4.1), never a bare db.create_job(), for any test that expects the worker to
actually process the job -- a bare db.create_job() row has no manifest and
is refused by db.confirm_job() (see tests below that specifically exercise
that low-level guard).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from switchagent import db, preview, queue_worker
from switchagent import manifest as manifest_mod
from switchagent.mtp import MockMtpBackend

from .conftest import build_zip
from .test_stage41_recovery import _make_mod_item


def _make_item(conn, inbox_dir, name: str, content: bytes, title_id: str):
    (inbox_dir / name).write_bytes(content)
    db.upsert_inbox_item(
        conn, relative_path=name, item_type="FILE", file_type="NSP",
        size=len(content), mtime=0.0, content_hash=f"hash-{name}",
        title_id=title_id, title_id_source="filename", status="ANALYZED",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    return db.get_inbox_item(conn, name)["id"]


def _make_confirmed_job(conn, inbox_dir, name: str, item_id: int, device_id: str) -> int:
    report = preview.preview_path(inbox_dir / name)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="INSTALL_VIA_DBI", target_device_id=device_id,
    )
    db.confirm_job(conn, job_id)
    return job_id


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


# 1/2/3. Two simultaneously "connected" mock Switches; each job only ever
# reaches its own target device -------------------------------------------

def test_job_for_device_a_lands_only_on_device_a(isolated_db):
    conn, inbox_dir = isolated_db
    parent, child, registry = _two_backends()

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"payload A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.job_id == job_id
    assert outcome.status == "DONE"
    assert parent.storage_tree("SD_INSTALL").list_files() == [name]
    assert child.storage_tree("SD_INSTALL").list_files() == []


def test_job_for_device_b_lands_only_on_device_b(isolated_db):
    conn, inbox_dir = isolated_db
    parent, child, registry = _two_backends()

    name = "GameB [0100000000020000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"payload B", "0100000000020000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-child")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.job_id == job_id
    assert outcome.status == "DONE"
    assert child.storage_tree("SD_INSTALL").list_files() == [name]
    assert parent.storage_tree("SD_INSTALL").list_files() == []


# 4. Both devices share storage names ("SD_CARD"/"SD_INSTALL") but content
# never mixes ---------------------------------------------------------------

def test_same_storage_names_on_both_devices_do_not_mix_content(isolated_db):
    conn, inbox_dir = isolated_db
    parent, child, registry = _two_backends()

    name_a = "GameA [0100000000010000][v0].nsp"
    name_b = "GameB [0100000000020000][v0].nsp"
    item_a = _make_item(conn, inbox_dir, name_a, b"content A", "0100000000010000")
    item_b = _make_item(conn, inbox_dir, name_b, b"content B", "0100000000020000")
    job_a = _make_confirmed_job(conn, inbox_dir, name_a, item_a, "mock-switch-parent")
    job_b = _make_confirmed_job(conn, inbox_dir, name_b, item_b, "mock-switch-child")

    assert queue_worker.run_worker_once(conn, registry).job_id == job_a
    assert queue_worker.run_worker_once(conn, registry).job_id == job_b

    assert parent.storage_tree("SD_INSTALL").read_file(name_a) == b"content A"
    assert child.storage_tree("SD_INSTALL").read_file(name_b) == b"content B"
    # each device's SD_INSTALL only ever has its own file, despite sharing the same storage name
    assert parent.storage_tree("SD_INSTALL").list_files() == [name_a]
    assert child.storage_tree("SD_INSTALL").list_files() == [name_b]


# 5/6. Device A goes away -- job A does not fail over to B, and B keeps
# being served normally ------------------------------------------------

def test_device_a_unavailable_does_not_redirect_job_a_to_device_b(isolated_db):
    """job_a has never attempted a transfer -- UI-005: an absent target
    device for a job at this stage is WAITING_FOR_DEVICE (self-resolving),
    not the older, more conservative DEVICE_UNAVAILABLE (see
    test_recovery_targets_device_a_specifically_not_device_b below for the
    already-attempted case, which still gets DEVICE_UNAVAILABLE)."""
    conn, inbox_dir = isolated_db
    parent, child, registry = _two_backends()
    parent.set_device_present(False)  # A is unplugged

    name_a = "GameA [0100000000010000][v0].nsp"
    name_b = "GameB [0100000000020000][v0].nsp"
    item_a = _make_item(conn, inbox_dir, name_a, b"content A", "0100000000010000")
    item_b = _make_item(conn, inbox_dir, name_b, b"content B", "0100000000020000")
    job_a = _make_confirmed_job(conn, inbox_dir, name_a, item_a, "mock-switch-parent")
    job_b = _make_confirmed_job(conn, inbox_dir, name_b, item_b, "mock-switch-child")

    # One call: must skip job_a (unavailable device) and still fully serve job_b.
    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.job_id == job_b
    assert outcome.status == "DONE"
    assert db.get_job(conn, job_a)["status"] == "WAITING_FOR_DEVICE"
    assert db.get_job(conn, job_a)["target_device_id"] == "mock-switch-parent", "target must not change"
    # job_a's payload must never have reached device B
    assert child.storage_tree("SD_INSTALL").list_files() == [name_b]
    assert parent.storage_tree("SD_INSTALL").list_files() == []


def test_device_b_continues_serving_its_own_job_normally(isolated_db):
    conn, inbox_dir = isolated_db
    parent, child, registry = _two_backends()
    parent.set_device_present(False)

    name_b = "GameB [0100000000020000][v0].nsp"
    item_b = _make_item(conn, inbox_dir, name_b, b"content B", "0100000000020000")
    job_b = _make_confirmed_job(conn, inbox_dir, name_b, item_b, "mock-switch-child")

    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome == queue_worker.JobRunOutcome(job_id=job_b, status="DONE")
    assert db.get_job(conn, job_b)["status"] == "DONE"


def test_device_a_disconnect_mid_transfer_waits_for_device_a_not_failed_over(isolated_db):
    conn, inbox_dir = isolated_db
    parent, child, registry = _two_backends()
    parent.arm_failure("disconnect")  # present at connect() time, drops during the actual send

    name_a = "GameA [0100000000010000][v0].nsp"
    item_a = _make_item(conn, inbox_dir, name_a, b"content A", "0100000000010000")
    job_a = _make_confirmed_job(conn, inbox_dir, name_a, item_a, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.job_id == job_a
    assert outcome.status == "WAITING_FOR_DEVICE"
    assert db.get_job(conn, job_a)["target_device_id"] == "mock-switch-parent"
    assert child.storage_tree("SD_INSTALL").list_files() == []


# 7/8. Recovery after a simulated process restart -----------------------

def test_recovery_targets_device_a_specifically_not_device_b(isolated_db):
    conn, inbox_dir = isolated_db
    parent, child, registry = _two_backends()

    name_a = "GameA [0100000000010000][v0].nsp"
    item_a = _make_item(conn, inbox_dir, name_a, b"content A", "0100000000010000")
    job_a = _make_confirmed_job(conn, inbox_dir, name_a, item_a, "mock-switch-parent")

    # Simulate a worker that crashed mid-transfer, without going through
    # run_worker_once (whatever state a real crash could leave behind).
    # increment_attempt=True matches what the real RUNNING transition in
    # _run_job_transfer() always does -- this job genuinely HAD an attempt,
    # which is exactly what keeps it on the older, more conservative
    # DEVICE_UNAVAILABLE path (UI-005) below rather than the gentler,
    # self-resolving WAITING_FOR_DEVICE a never-attempted job would get.
    db.update_job_status(conn, job_a, "RUNNING", started_at=db.now_iso(), increment_attempt=True)

    recovered = db.recover_stale_running_jobs(conn)
    assert recovered == 1
    assert db.get_job(conn, job_a)["status"] == "INTERRUPTED"
    assert db.get_job(conn, job_a)["target_device_id"] == "mock-switch-parent"

    # Device A is still absent post-"restart" -- retrying must look for A
    # specifically and must NOT succeed via device B.
    parent.set_device_present(False)
    db.retry_job(conn, job_a)
    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome is None  # nothing processable: only job is DEVICE_UNAVAILABLE now
    assert db.get_job(conn, job_a)["status"] == "DEVICE_UNAVAILABLE"
    assert db.get_job(conn, job_a)["target_device_id"] == "mock-switch-parent"
    assert child.storage_tree("SD_INSTALL").list_files() == []


def test_two_jobs_keep_correct_target_device_after_reopening_the_database(isolated_db):
    conn, inbox_dir = isolated_db
    name_a = "GameA [0100000000010000][v0].nsp"
    name_b = "GameB [0100000000020000][v0].nsp"
    item_a = _make_item(conn, inbox_dir, name_a, b"content A", "0100000000010000")
    item_b = _make_item(conn, inbox_dir, name_b, b"content B", "0100000000020000")
    job_a = _make_confirmed_job(conn, inbox_dir, name_a, item_a, "mock-switch-parent")
    job_b = _make_confirmed_job(conn, inbox_dir, name_b, item_b, "mock-switch-child")

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    conn.close()

    # Fresh connection to the same file -- simulates the process restarting
    # and re-opening the persisted queue.
    reopened = db.get_connection(db_path)
    db.init_db(reopened)

    assert db.get_job(reopened, job_a)["target_device_id"] == "mock-switch-parent"
    assert db.get_job(reopened, job_b)["target_device_id"] == "mock-switch-child"
    assert db.get_job(reopened, job_a)["status"] == "CONFIRMED"
    assert db.get_job(reopened, job_b)["status"] == "CONFIRMED"
    reopened.close()


# 9. A job can never exist without an unambiguous target device -----------

def test_create_job_without_target_device_id_raises(isolated_db):
    conn, inbox_dir = isolated_db
    item_id = _make_item(conn, inbox_dir, "GameA [0100000000010000][v0].nsp", b"content A", "0100000000010000")

    with pytest.raises(ValueError):
        db.create_job(
            conn, inbox_item_id=item_id, action="INSTALL_VIA_DBI",
            target_storage="SD_INSTALL", target_device_id="",
        )

    assert db.list_jobs(conn) == [], "no row should have been inserted"


def test_create_job_with_none_target_device_id_raises(isolated_db):
    conn, inbox_dir = isolated_db
    item_id = _make_item(conn, inbox_dir, "GameA [0100000000010000][v0].nsp", b"content A", "0100000000010000")

    with pytest.raises(ValueError):
        db.create_job(
            conn, inbox_item_id=item_id, action="INSTALL_VIA_DBI",
            target_storage="SD_INSTALL", target_device_id=None,
        )


def test_worker_never_invents_a_device_for_an_unregistered_target(isolated_db):
    """Even if a job somehow targets a device_id the worker has never heard
    of (not present in the DeviceRegistry at all -- not merely
    disconnected), it must wait, not silently pick any registered backend.
    Never-attempted -> WAITING_FOR_DEVICE (UI-005), same as the merely-
    disconnected case above; this job has had zero chances to prove it
    needs the more conservative DEVICE_UNAVAILABLE."""
    conn, inbox_dir = isolated_db
    _parent, _child, registry = _two_backends()

    name = "GameC [0100000000030000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content C", "0100000000030000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-grandparent")  # never registered

    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome is None
    assert db.get_job(conn, job_id)["status"] == "WAITING_FOR_DEVICE"
    assert db.get_job(conn, job_id)["target_device_id"] == "mock-switch-grandparent"


# -- extra: worker-level regression coverage for confirm/retry state guards --

def test_confirm_job_without_manifest_raises(isolated_db):
    """A bare db.create_job() row (no manifest attached) must never become
    confirmable -- confirmation is the last gate before a job is eligible
    to run, and the worker has nothing safe to send without a manifest."""
    conn, inbox_dir = isolated_db
    item_id = _make_item(conn, inbox_dir, "GameA [0100000000010000][v0].nsp", b"content A", "0100000000010000")
    job_id = db.create_job(
        conn, inbox_item_id=item_id, action="INSTALL_VIA_DBI",
        target_storage="SD_INSTALL", target_device_id="mock-switch-parent",
    )
    with pytest.raises(ValueError, match="no manifest"):
        db.confirm_job(conn, job_id)


def test_confirm_job_wrong_status_raises(isolated_db):
    conn, inbox_dir = isolated_db
    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")
    with pytest.raises(ValueError):
        db.confirm_job(conn, job_id)  # already CONFIRMED, not PENDING_CONFIRM


def test_retry_job_wrong_status_raises(isolated_db):
    conn, inbox_dir = isolated_db
    item_id = _make_item(conn, inbox_dir, "GameA [0100000000010000][v0].nsp", b"content A", "0100000000010000")
    job_id = db.create_job(
        conn, inbox_item_id=item_id, action="INSTALL_VIA_DBI",
        target_storage="SD_INSTALL", target_device_id="mock-switch-parent",
    )
    with pytest.raises(ValueError):
        db.retry_job(conn, job_id)  # still PENDING_CONFIRM, nothing to retry


# -- SD_INSTALL semantics fix: TransferStatus.UNVERIFIED handling ---------
#
# MockMtpBackend cannot reproduce real DBI/MTP quirks, but it CAN simulate
# the outcome a real backend now honestly reports for an install-like
# destination (arm_failure("unverified", ...) -- see switchagent/mtp/mock.py)
# so this queue-level branch is covered without needing real hardware.

def test_unverified_transfer_marks_job_done_unverified_not_done(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.arm_failure("unverified")

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.job_id == job_id
    assert outcome.status == "DONE_UNVERIFIED"
    assert outcome.status != "DONE"
    assert outcome.status != "FAILED"
    row = db.get_job(conn, job_id)
    assert row["status"] == "DONE_UNVERIFIED"
    # the file WAS actually written by the mock (device accepted it) --
    # only the verdict is unverified, not the underlying transfer.
    assert parent.storage_tree("SD_INSTALL").list_files() == [name]


def test_unverified_job_counts_as_spoken_for_content(isolated_db):
    """DONE_UNVERIFIED must behave like DONE for dedup purposes -- a second
    job for the exact same content should be recognized as already
    spoken-for, not left to collide silently."""
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.arm_failure("unverified")

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")
    queue_worker.run_worker_once(conn, registry)

    assert db.get_job(conn, job_id)["status"] == "DONE_UNVERIFIED"
    existing = db.find_existing_job_for_hash(conn, f"hash-{name}")
    assert existing is not None
    assert existing["id"] == job_id


def test_successful_job_persists_bytes_total_and_done_on_the_job_row(isolated_db):
    """Before this fix, jobs.bytes_total/bytes_done were only ever written
    at a TERMINAL status (via install_history's own separate bytes_total,
    or the DESTINATION_CONFLICT/FAILED/INTERRUPTED writes) -- during the
    whole RUNNING phase the jobs row itself kept bytes_total=NULL,
    bytes_done=0 no matter how much had actually transferred, so
    queue.js's own SD_CARD progress-bar fraction (which reads exactly
    these two columns) never had real numbers to show. This checks the
    terminal DONE row directly on `jobs`, not install_history, which
    already had the right number before this fix."""
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    content = b"content A" * 50
    name = "GameB [0100000000020000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, content, "0100000000020000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    queue_worker.run_worker_once(conn, registry)

    row = db.get_job(conn, job_id)
    assert row["status"] in ("DONE", "DONE_UNVERIFIED")
    assert row["bytes_total"] == len(content)
    assert row["bytes_done"] == len(content)


# -- install_history: one row per terminal outcome (Web UI History page) --

def test_done_job_records_install_history(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    queue_worker.run_worker_once(conn, registry)

    history = db.list_install_history(conn)
    assert len(history) == 1
    assert history[0]["job_id"] == job_id
    assert history[0]["outcome"] == "DONE"
    assert history[0]["display_name"] == name
    assert history[0]["target_device_id"] == "mock-switch-parent"
    assert history[0]["title_id"] == "0100000000010000"


def test_interrupted_job_records_install_history(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.arm_failure("crash")
    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    queue_worker.run_worker_once(conn, registry)

    history = db.list_install_history(conn)
    assert len(history) == 1
    assert history[0]["outcome"] == "INTERRUPTED"


def test_destination_conflict_records_install_history(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.connect()
    parent.storage_tree("SD_INSTALL").write_file("GameA [0100000000010000][v0].nsp", b"someone else's file")

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    queue_worker.run_worker_once(conn, registry)

    history = db.list_install_history(conn)
    assert len(history) == 1
    assert history[0]["outcome"] == "DESTINATION_CONFLICT"


def test_destination_conflict_detected_without_a_separate_exists_probe(isolated_db):
    """_run_job_transfer() used to call backend.exists() before every
    send_file() -- a second full navigate + directory-scan of the exact
    same destination send_file() was about to check again internally on
    its own. On real MTP hardware each of those is its own COM round-trip
    to the device, so for a job with many small files this doubled the
    per-file navigation cost for no benefit. It's gone now: the per-file
    loop relies entirely on send_file()'s own existence check, catching
    FileAlreadyExistsError. This confirms both halves of that change
    still hold: the DESTINATION_CONFLICT outcome and its error message are
    byte-for-byte unchanged, and the mock backend's operation log no
    longer contains a standalone EXISTS call for this file."""
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.connect()
    name = "GameA [0100000000010000][v0].nsp"
    parent.storage_tree("SD_INSTALL").write_file(name, b"someone else's file")
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "DESTINATION_CONFLICT"
    job = db.get_job(conn, job_id)
    assert job["error"] == (
        f"'{name}' already exists on 'SD_INSTALL' and was not sent by this job -- skipped automatically "
        "(Settings: existing files on the Switch are skipped)"
    )
    # Auto-resolved the instant it happens (Settings' conflict policy) --
    # no one needs to look at it, so it is abandoned right away.
    assert bool(job["abandoned"]) is True
    exists_ops = [op for op in parent.operation_log if op.operation == "EXISTS"]
    assert exists_ops == []


def test_destination_conflict_override_replaces_existing_file(isolated_db):
    """W3-006 Override: a job created with force_overwrite=1 (only ever set
    by services.override_job(), the user's explicit "Override" click on a
    DESTINATION_CONFLICT card) must skip the existence check entirely and
    replace whatever's already at the destination -- the opposite of
    test_destination_conflict_records_install_history above, which proves
    the DEFAULT (force_overwrite=0) job still refuses."""
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.connect()
    parent.storage_tree("SD_INSTALL").write_file("GameA [0100000000010000][v0].nsp", b"someone else's file")

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")
    conn.execute("UPDATE jobs SET force_overwrite=1 WHERE id=?", (job_id,))
    conn.commit()

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status in ("DONE", "DONE_UNVERIFIED")
    assert parent.storage_tree("SD_INSTALL").read_file(name) == b"content A"

    history = db.list_install_history(conn)
    assert len(history) == 1
    assert history[0]["outcome"] in ("DONE", "DONE_UNVERIFIED")


def test_unverified_job_records_install_history(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.arm_failure("unverified")
    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    queue_worker.run_worker_once(conn, registry)

    history = db.list_install_history(conn)
    assert len(history) == 1
    assert history[0]["outcome"] == "DONE_UNVERIFIED"


# -- mod display name: Queue/History side (real bug report -- see
# tests/test_library_grouping.py for the Library-page side of the same
# fix) --------------------------------------------------------------------

def test_mod_job_history_shows_base_game_name_not_bare_title_id(isolated_db):
    """A mod's install_history.display_name must resolve through
    queue_worker.find_family_base_name_source(), exactly like the Library
    page -- one shared lookup, not two independently-maintained naming
    systems. Regression for a real report: a Bread and Fred mod's
    install_history row showed "0100AF401B6A4000" (its own TITLE_ID)."""
    conn, _inbox_dir = isolated_db
    from switchagent import config
    base_id = db.upsert_library_item(
        conn, absolute_path=str(config.LIBRARY_DIR / "Bread and Fred [0100AF401B6A4000][v0] (0.41 GB).nsz"),
        item_type="FILE", file_type="NSZ", size=100, mtime=0.0, content_hash="base",
        title_id="0100AF401B6A4000", title_id_source="filename", status="AVAILABLE",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL", content_type="GAME_PACKAGE",
        package_format="NSZ",
    )
    mod_id = db.upsert_library_item(
        conn, absolute_path=str(config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100AF401B6A4000"),
        item_type="MOD_FOLDER", file_type="ATMOSPHERE_MOD", size=5, mtime=0.0, content_hash="mod",
        title_id="0100AF401B6A4000", title_id_source="atmosphere_path", title_id_confident=True,
        status="AVAILABLE", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
        content_type="ATMOSPHERE_MOD",
    )
    mod_job_id = db.create_job(
        conn, action="COPY_MERGE", target_storage="SD_CARD", target_device_id="mock-switch-parent",
        library_item_id=mod_id,
    )
    job_row = db.get_job(conn, mod_job_id)

    name = queue_worker.display_name_for_job(conn, job_row)
    # " -- Mod" suffix: a mod's job must be visibly distinguishable from
    # its base game's own job, which otherwise shares this exact borrowed
    # name.
    assert name == "Bread and Fred [0100AF401B6A4000][v0] (0.41 GB).nsz — Mod"
    assert name != "0100AF401B6A4000"

    # TITLE_ID-based lookup itself, directly.
    base_row = queue_worker.find_family_base_name_source(conn, "0100AF401B6A4000")
    assert base_row["id"] == base_id


def test_mod_job_history_falls_back_to_bare_title_id_with_no_family(isolated_db):
    """No base/update/DLC indexed at all for this TITLE_ID -- must not
    invent a name."""
    conn, _inbox_dir = isolated_db
    from switchagent import config
    mod_id = db.upsert_library_item(
        conn, absolute_path=str(config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000099999"),
        item_type="MOD_FOLDER", file_type="ATMOSPHERE_MOD", size=5, mtime=0.0, content_hash="mod2",
        title_id="0100000000099999", title_id_source="atmosphere_path", title_id_confident=True,
        status="AVAILABLE", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
        content_type="ATMOSPHERE_MOD",
    )
    mod_job_id = db.create_job(
        conn, action="COPY_MERGE", target_storage="SD_CARD", target_device_id="mock-switch-parent",
        library_item_id=mod_id,
    )
    job_row = db.get_job(conn, mod_job_id)
    assert queue_worker.display_name_for_job(conn, job_row) == "0100000000099999 — Mod"


def test_job_log_records_lifecycle_events(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")
    queue_worker.run_worker_once(conn, registry)

    messages = [row["message"] for row in db.list_job_log(conn, job_id)]
    assert any("created" in m for m in messages)
    assert any("manifest frozen" in m for m in messages)
    assert any("confirmed" in m for m in messages)
    assert any("running" in m for m in messages)
    assert any("done" in m for m in messages)


# ---------------------------------------------------------------------------
# UI-005: WAITING_FOR_DEVICE -- a job that has NEVER started a transfer
# whose target device is merely absent self-resolves the moment its SAME
# target device reappears, no manual retry needed. Existing
# DEVICE_UNAVAILABLE semantics for an already-attempted job are unchanged
# (see test_recovery_targets_device_a_specifically_not_device_b above,
# which still gets DEVICE_UNAVAILABLE after a real attempt).
# ---------------------------------------------------------------------------

def test_missing_device_before_first_attempt_marks_waiting_for_device(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.set_device_present(False)

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome is None  # nothing else processable -- the only job's device is absent
    row = db.get_job(conn, job_id)
    assert row["status"] == "WAITING_FOR_DEVICE"
    assert row["attempt_count"] == 0


def test_waiting_for_device_stays_waiting_across_multiple_passes_without_log_spam(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.set_device_present(False)

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    for _ in range(3):
        queue_worker.run_worker_once(conn, registry)

    row = db.get_job(conn, job_id)
    assert row["status"] == "WAITING_FOR_DEVICE"
    assert row["attempt_count"] == 0  # still never actually started
    transitions = [
        r["message"] for r in db.list_job_log(conn, job_id) if "WAITING_FOR_DEVICE" in r["message"]
    ]
    assert len(transitions) == 1, f"expected exactly one transition log entry, got {transitions}"


def test_device_reappearing_lets_the_waiting_job_run(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.set_device_present(False)

    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    queue_worker.run_worker_once(conn, registry)
    assert db.get_job(conn, job_id)["status"] == "WAITING_FOR_DEVICE"

    parent.set_device_present(True)  # the SAME device reappears
    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome.job_id == job_id
    assert outcome.status == "DONE"
    assert parent.storage_tree("SD_INSTALL").list_files() == [name]


def test_a_different_device_reappearing_does_not_satisfy_a_waiting_job(isolated_db):
    """job_a targets device A specifically -- device B being available must
    never let job_a run on it (the core "never substitute a different
    device" rule this whole module exists to enforce, see its own module
    docstring, applies to WAITING_FOR_DEVICE exactly like every other
    status)."""
    conn, inbox_dir = isolated_db
    parent, child, registry = _two_backends()
    parent.set_device_present(False)

    name_a = "GameA [0100000000010000][v0].nsp"
    item_a = _make_item(conn, inbox_dir, name_a, b"content A", "0100000000010000")
    job_a = _make_confirmed_job(conn, inbox_dir, name_a, item_a, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome is None  # device B (present, but has no jobs of its own here) can't satisfy job_a
    assert db.get_job(conn, job_a)["status"] == "WAITING_FOR_DEVICE"
    assert child.storage_tree("SD_INSTALL").list_files() == []


# ---------------------------------------------------------------------------
# UI-006: stall detection -- jobs.last_progress_at is touched only at real,
# honest progress points (RUNNING start, each file's start, each file's
# completion), never a fake periodic keepalive. The derived "possibly
# stalled" UI condition itself lives in web/services.py and is tested
# there; these cover only that the timestamp is actually recorded
# correctly by the worker.
# ---------------------------------------------------------------------------

def test_last_progress_at_is_set_when_job_starts_running(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    name = "GameA [0100000000010000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"content A", "0100000000010000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    queue_worker.run_worker_once(conn, registry)

    row = db.get_job(conn, job_id)
    assert row["status"] == "DONE"
    assert row["last_progress_at"] is not None


def test_last_progress_at_advances_across_multiple_files(isolated_db, monkeypatch):
    """A multi-file Atmosphere mod job -- last_progress_at must reflect
    real, ongoing activity across the whole transfer, not just a single
    write at the very start. Counts every db.update_job_status() call that
    actually carries a last_progress_at value -- precise proof the
    touch-points exist at every documented point (RUNNING start, each
    file's start, each file's own live byte reports, each file's
    completion), not just once up front."""
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()

    mod_dir = inbox_dir / "0100000000099999"
    (mod_dir / "romfs").mkdir(parents=True)
    (mod_dir / "romfs" / "a.bin").write_bytes(b"aaa")
    (mod_dir / "romfs" / "b.bin").write_bytes(b"bbbbb")
    mod_id = db.upsert_inbox_item(
        conn, relative_path="0100000000099999", item_type="MOD_FOLDER", file_type="ATMOSPHERE_MOD",
        size=8, mtime=0.0, content_hash="modhash", title_id="0100000000099999",
        title_id_source="atmosphere_path", title_id_confident=True, status="ANALYZED",
        suggested_action="COPY_MERGE", suggested_target="SD_CARD",
    )
    from switchagent import preview
    report = preview.preview_path(mod_dir)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=mod_id, action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    real_update_job_status = db.update_job_status
    progress_touch_count = 0

    def _counting_update_job_status(conn_, job_id_, status_, **kwargs):
        nonlocal progress_touch_count
        if kwargs.get("last_progress_at") is not None:
            progress_touch_count += 1
        return real_update_job_status(conn_, job_id_, status_, **kwargs)

    monkeypatch.setattr(db, "update_job_status", _counting_update_job_status)
    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.job_id == job_id
    assert outcome.status == "DONE"
    assert db.get_job(conn, job_id)["last_progress_at"] is not None
    # RUNNING start (1) + 2 files x (start-of-file + one live progress callback
    # from the backend + completion) (6) == 7. The middle one is the transport
    # reporting bytes as they actually move (see MtpBackend.send_file's
    # `progress` contract): MockMtpBackend reports once per file, a real WPD
    # transfer reports about once a second, and the Shell fallback -- which
    # cannot see inside its own copy -- reports not at all.
    assert progress_touch_count == 7


# -- FAULT-001: manifest/progress corruption is refused, never guessed at
# (mock-only, per the mandate) -----------------------------------------

def test_worker_fails_a_job_with_missing_manifest_but_keeps_processing_the_queue(isolated_db):
    """A job whose manifest.json has gone missing (e.g. work/job-<id>/ was
    deleted by hand) must be failed outright, not silently crash
    run_worker_once() for every job behind it. Previously an unguarded
    load_manifest() call raised straight out of this function's for loop --
    since the SAME broken job stays first in creation order, it would raise
    again on every subsequent pass, blocking the entire queue for every
    device, forever, not just failing this one job."""
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()

    item_a = _make_item(conn, inbox_dir, "GameA [0100000000091000][v0].nsp", b"a bytes", "0100000000091000")
    job_a = _make_confirmed_job(conn, inbox_dir, "GameA [0100000000091000][v0].nsp", item_a, "mock-switch-parent")
    manifest_mod.manifest_path_for(job_a).unlink()

    item_b = _make_item(conn, inbox_dir, "GameB [0100000000092000][v0].nsp", b"b bytes", "0100000000092000")
    job_b = _make_confirmed_job(conn, inbox_dir, "GameB [0100000000092000][v0].nsp", item_b, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)

    # job_a (created first, oldest) is failed inline and the SAME call
    # keeps scanning -- job_b (still perfectly valid) is the one actually
    # processed and returned, proving the queue was not stalled.
    assert outcome.job_id == job_b
    assert outcome.status == "DONE"
    row_a = db.get_job(conn, job_a)
    assert row_a["status"] == "FAILED"
    assert "manifest" in row_a["error"].lower()
    history_a = [h for h in db.list_install_history(conn) if h["job_id"] == job_a]
    assert len(history_a) == 1
    assert history_a[0]["outcome"] == "FAILED"
    assert history_a[0]["title_id"] is None  # honest: never guessed from a missing manifest


def test_worker_fails_a_job_with_malformed_manifest_json(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    item_id = _make_item(conn, inbox_dir, "Game [0100000000093000][v0].nsp", b"bytes", "0100000000093000")
    job_id = _make_confirmed_job(conn, inbox_dir, "Game [0100000000093000][v0].nsp", item_id, "mock-switch-parent")

    manifest_mod.manifest_path_for(job_id).write_text("{ not valid json", encoding="utf-8")

    outcome = queue_worker.run_worker_once(conn, registry)

    # Handled inline (like WAITING_FOR_BASE/BLOCKED_BY_DEPENDENCY) -- with
    # only this one, now-terminal job in the queue, there is nothing left
    # to actually process via _process_job() this call, so the return
    # value is None, not this job's own outcome.
    assert outcome is None
    assert db.get_job(conn, job_id)["status"] == "FAILED"
    history = [h for h in db.list_install_history(conn) if h["job_id"] == job_id]
    assert len(history) == 1 and history[0]["outcome"] == "FAILED"
    assert parent.storage_tree("SD_INSTALL").list_files() == []


def test_has_outstanding_base_job_skips_a_sibling_with_a_corrupted_manifest(isolated_db):
    """_has_outstanding_base_job() (the install-order dependency check's own
    helper) must never crash while scanning OTHER non-terminal jobs on the
    same device just because one of them has a corrupted manifest -- and
    must still find a genuinely valid outstanding base job sitting right
    alongside the corrupted one."""
    conn, inbox_dir = isolated_db
    _parent, _child, _registry = _two_backends()

    corrupted_item = _make_item(
        conn, inbox_dir, "Corrupted [0100000000094000][v0].nsp", b"x", "0100000000094000",
    )
    corrupted_job = _make_confirmed_job(
        conn, inbox_dir, "Corrupted [0100000000094000][v0].nsp", corrupted_item, "mock-switch-parent",
    )
    manifest_mod.manifest_path_for(corrupted_job).write_text("not json at all", encoding="utf-8")

    # No OTHER base job for this title exists yet -- must not raise, and
    # the corrupted job itself must never be mistaken for an outstanding one.
    assert queue_worker._has_outstanding_base_job(conn, "mock-switch-parent", "0100000000094000") is False

    valid_base_item = _make_item(
        conn, inbox_dir, "ValidBase [0100000000094000][v0].nsp", b"y", "0100000000094000",
    )
    _make_confirmed_job(
        conn, inbox_dir, "ValidBase [0100000000094000][v0].nsp", valid_base_item, "mock-switch-parent",
    )

    # A genuinely valid outstanding base job for the same title, sitting
    # alongside the still-corrupted one, must still be found.
    assert queue_worker._has_outstanding_base_job(conn, "mock-switch-parent", "0100000000094000") is True


def test_process_job_still_records_history_if_manifest_reload_fails_afterward(isolated_db, monkeypatch):
    """_process_job()'s OWN, separate manifest reload (used only to fill in
    title_id/bytes_total for the install_history row, after
    _run_job_transfer() has already durably set the job's terminal status)
    must not let history-recording itself be skipped just because the
    manifest became unreadable between the transfer finishing and this
    second reload."""
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    item_id = _make_item(conn, inbox_dir, "Game [0100000000095000][v0].nsp", b"bytes", "0100000000095000")
    job_id = _make_confirmed_job(conn, inbox_dir, "Game [0100000000095000][v0].nsp", item_id, "mock-switch-parent")

    real_load_manifest = manifest_mod.load_manifest
    call_count = {"n": 0}

    def _flaky_load_manifest(job_id_):
        call_count["n"] += 1
        # 1st call: run_worker_once()'s own load. 2nd call: _run_job_
        # transfer()'s load (must succeed, or the transfer itself can't
        # run). 3rd call: _process_job()'s post-transfer reload for
        # history -- this is the one this test targets.
        if call_count["n"] >= 3:
            raise FileNotFoundError("simulated: manifest vanished after the transfer already completed")
        return real_load_manifest(job_id_)

    monkeypatch.setattr(queue_worker.manifest_mod, "load_manifest", _flaky_load_manifest)

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "DONE"  # the transfer itself succeeded fine
    assert db.get_job(conn, job_id)["status"] == "DONE"
    history = [h for h in db.list_install_history(conn) if h["job_id"] == job_id]
    assert len(history) == 1
    assert history[0]["outcome"] == "DONE"
    assert history[0]["title_id"] is None  # honest: could not be reloaded, never guessed
    assert history[0]["bytes_total"] is None


def test_worker_fails_safely_when_a_frozen_staged_file_is_missing(isolated_db):
    """FAULT-001 names this as its own distinct scenario: an archive-sourced
    ('frozen') manifest file's staged copy under work/job-<id>/ can vanish
    (a future work_cleanup bug, manual deletion, disk trouble) between job
    creation and the worker actually running. Already caught by the
    existing Stage-4.1 TOCTOU guard (a 'frozen' source is just another
    manifest file verify_manifest_against_source checks) -- locked in here
    explicitly, separate from a bare inbox/library file going missing
    (already covered by test_stage41_recovery.py's own 'source deleted'
    scenario, which only ever exercises the 'inbox' source_kind)."""
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()

    zip_path = build_zip(inbox_dir / "pack.zip", {"Game [0100000000096000][v0].nsp": b"zip nsp bytes"})
    db.upsert_inbox_item(
        conn, relative_path="pack.zip", item_type="FILE", file_type="ZIP",
        size=zip_path.stat().st_size, mtime=0.0, content_hash="zip-hash",
        title_id="0100000000096000", title_id_source="filename", status="ANALYZED",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    item_id = db.get_inbox_item(conn, "pack.zip")["id"]
    report = preview.preview_path(zip_path, extract=True)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="INSTALL_VIA_DBI", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    frozen_file = manifest_mod.job_work_dir(job_id) / "Game [0100000000096000][v0].nsp"
    assert frozen_file.is_file()
    frozen_file.unlink()

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "FAILED"
    assert "no longer exists" in outcome.error
    assert parent.storage_tree("SD_INSTALL").list_files() == []


# -- FAULT-003: restart matrix (mock-only, per the mandate) --------------
# RUNNING -> INTERRUPTED recovery is already exercised by
# test_recovery_targets_device_a_specifically_not_device_b above (the one
# status a restart actively CHANGES). Everything below fills in the rest
# of the mandate's own named status list -- proving a restart (simulated
# the same way as the existing test_two_jobs_keep_correct_target_device_
# after_reopening_the_database: close the connection, open a fresh one to
# the SAME file) leaves every one of them completely unchanged, since
# db.recover_stale_running_jobs() only ever touches RUNNING rows.

def _reopen(conn):
    """Simulates a process restart: closes `conn` and returns a fresh
    connection to the exact same on-disk database file."""
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    conn.close()
    reopened = db.get_connection(db_path)
    db.init_db(reopened)
    return reopened


@pytest.mark.parametrize("status", [
    "PENDING_CONFIRM", "CONFIRMED", "DEVICE_UNAVAILABLE", "WAITING_FOR_DEVICE",
    "WAITING_FOR_BASE", "FAILED", "INTERRUPTED", "SOURCE_CHANGED",
    "DESTINATION_CONFLICT", "BLOCKED_BY_DEPENDENCY", "DONE", "DONE_UNVERIFIED",
])
def test_restart_leaves_every_non_running_status_completely_unchanged(isolated_db, status):
    conn, inbox_dir = isolated_db
    name = "Game [0100000000099000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"bytes", "0100000000099000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")
    db.update_job_status(conn, job_id, status, error="synthetic, for FAULT-003" if status != "PENDING_CONFIRM" else None)
    before = dict(db.get_job(conn, job_id))

    recovered_count = db.recover_stale_running_jobs(conn)
    reopened = _reopen(conn)
    recovered_after_reopen = db.recover_stale_running_jobs(reopened)

    after = dict(db.get_job(reopened, job_id))
    assert recovered_count == 0
    assert recovered_after_reopen == 0
    assert after["status"] == status, f"a restart must never change a {status} job's status on its own"
    assert after["target_device_id"] == before["target_device_id"]
    assert after["manifest_path"] == before["manifest_path"]
    reopened.close()


def test_startup_abandons_a_job_left_unconfirmable_by_a_restart(isolated_db):
    """A PENDING_CONFIRM job means the process died between staging it and
    confirming it, and nothing can ever confirm it now: the only caller of
    confirm_job() for these is web/preparation.py's sequential run, whose
    state lives in memory and does not survive a restart. Left alone it
    pins its staged payload in work/ forever, keeps forget_device()
    refusing that Switch, and (before services._not_yet_queued) sat in
    Queue permanently as a phantom row with a live Cancel button."""
    conn, inbox_dir = isolated_db
    name = "Game [0100000000099000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"bytes", "0100000000099000")
    staged = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")
    db.update_job_status(conn, staged, "PENDING_CONFIRM")
    confirmed = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    reopened = _reopen(conn)
    assert db.abandon_unconfirmed_jobs(reopened) == 1

    after = db.get_job(reopened, staged)
    assert after["status"] == "FAILED" and after["abandoned"]
    # Nothing else is touched, and a second startup finds nothing left.
    assert db.get_job(reopened, confirmed)["status"] == "CONFIRMED"
    assert db.abandon_unconfirmed_jobs(reopened) == 0
    reopened.close()


def test_restart_recovers_a_partially_delivered_job_and_resume_still_works(isolated_db):
    """Combines two mandate-named scenarios in one coherent flow: a
    multi-file job partially delivered, THEN a real process restart
    (connection closed and reopened -- not just retried within the same
    process, unlike test_interrupted_multi_file_retry_resumes_without_
    already_exists_error above, which never closes the connection at
    all), and only THEN retried -- proving progress.json (which lives on
    disk, not in the DB connection) survives a genuine restart and still
    makes the retry skip already-delivered files correctly."""
    conn, inbox_dir = isolated_db
    title_id = "0100000000075000"
    item_id, mod_root = _make_mod_item(conn, inbox_dir, title_id, {
        "a_first.bin": b"file one content", "b_second.bin": b"file two content",
    })
    parent, _child, registry = _two_backends()
    report = preview.preview_path(mod_root)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    dest_b = f"atmosphere/contents/{title_id}/romfs/b_second.bin"
    parent.arm_failure("crash", dest_path=dest_b)
    outcome1 = queue_worker.run_worker_once(conn, registry)
    assert outcome1.status == "INTERRUPTED"

    # Simulate a real process restart -- recovery must find nothing to do
    # here (the job is already INTERRUPTED, never RUNNING at restart time).
    reopened = _reopen(conn)
    recovered = db.recover_stale_running_jobs(reopened)
    assert recovered == 0
    assert db.get_job(reopened, job_id)["status"] == "INTERRUPTED"

    db.retry_job(reopened, job_id)
    outcome2 = queue_worker.run_worker_once(reopened, registry)

    assert outcome2.status == "DONE"
    files = set(parent.storage_tree("SD_CARD").list_files())
    assert f"atmosphere/contents/{title_id}/romfs/a_first.bin" in files
    assert dest_b in files
    reopened.close()


def test_restart_preserves_a_user_verified_done_unverified_outcome(isolated_db):
    conn, inbox_dir = isolated_db
    parent, _child, registry = _two_backends()
    parent.arm_failure("unverified")
    name = "Game [0100000000076000][v0].nsp"
    item_id = _make_item(conn, inbox_dir, name, b"bytes", "0100000000076000")
    job_id = _make_confirmed_job(conn, inbox_dir, name, item_id, "mock-switch-parent")

    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome.status == "DONE_UNVERIFIED"

    history_row = next(h for h in db.list_install_history(conn) if h["job_id"] == job_id)
    db.set_user_verified_outcome(conn, history_row["id"], "SUCCESS")

    reopened = _reopen(conn)

    after = next(h for h in db.list_install_history(reopened) if h["job_id"] == job_id)
    assert after["outcome"] == "DONE_UNVERIFIED", "the TRANSPORT outcome itself must never be rewritten"
    assert after["user_verified_outcome"] == "SUCCESS", "the user's own confirmation must survive a restart"
    reopened.close()


def test_restart_preserves_a_partially_finished_batch(isolated_db):
    """One batch, two jobs targeting the SAME device: one already DONE, the
    other still CONFIRMED (never started) at the moment of the simulated
    restart. Both must come back exactly as they were, and the
    still-CONFIRMED one must still be pickable up normally afterward."""
    conn, inbox_dir = isolated_db
    name_a = "GameA [0100000000077000][v0].nsp"
    name_b = "GameB [0100000000078000][v0].nsp"
    item_a = _make_item(conn, inbox_dir, name_a, b"content A", "0100000000077000")
    item_b = _make_item(conn, inbox_dir, name_b, b"content B", "0100000000078000")
    parent, _child, registry = _two_backends()

    report_a = preview.preview_path(inbox_dir / name_a)
    job_a = queue_worker.create_job_from_report(
        conn, report_a, inbox_item_id=item_a, action="INSTALL_VIA_DBI",
        target_device_id="mock-switch-parent", batch_id=None,
    )
    db.confirm_job(conn, job_a)
    report_b = preview.preview_path(inbox_dir / name_b)
    job_b = queue_worker.create_job_from_report(
        conn, report_b, inbox_item_id=item_b, action="INSTALL_VIA_DBI",
        target_device_id="mock-switch-parent", batch_id=None,
    )
    db.confirm_job(conn, job_b)

    outcome_a = queue_worker.run_worker_once(conn, registry)
    assert outcome_a.job_id == job_a and outcome_a.status == "DONE"
    assert db.get_job(conn, job_b)["status"] == "CONFIRMED", "job_b must still be untouched -- one unit of work per call"

    reopened = _reopen(conn)
    assert db.get_job(reopened, job_a)["status"] == "DONE"
    assert db.get_job(reopened, job_b)["status"] == "CONFIRMED"

    outcome_b = queue_worker.run_worker_once(reopened, registry)
    assert outcome_b.job_id == job_b and outcome_b.status == "DONE"
    reopened.close()
