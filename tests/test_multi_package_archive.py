"""Regression tests: an archive selected for install that contains
Base + Update + DLC (or several DLC) used to only ever install the
Base -- extractor.classify_entries() silently kept just the first
installable entry it found, so every later entry never even reached
PreviewReport, let alone became a job (see tests/test_extractor.py's
test_classify_entries_collects_every_package_entry_not_just_the_first for
the root-cause-level reproduction, and tests/test_preview.py for the
PreviewReport.package_entries collection itself).

These tests cover the job-count/order matrix at the level users actually
hit this bug: one Web UI "Confirm Install" call
(switchagent.web.services.create_and_confirm_jobs) over a single selected
archive-backed library item. All jobs run via MockMtpBackend, matching
tests/test_install_order.py's own established pattern for the
install-order/dependency machinery this fix also touches.
"""

from __future__ import annotations

from switchagent import db, manifest as manifest_mod, queue_worker
from switchagent.mtp import MockMtpBackend
from switchagent.web import services

from .conftest import build_zip

BASE_TITLE_ID = "0100AAAAAAAAA000"
UPDATE_TITLE_ID = "0100AAAAAAAAA800"
# DLC recovery is base_id = (dlc_id & ~0xFFF) - 0x1000 (see
# title_id.classify_title_variant's own docstring/example) -- these two
# must actually recover BASE_TITLE_ID above, not just look plausible.
DLC1_TITLE_ID = "0100AAAAAAAAB001"
DLC2_TITLE_ID = "0100AAAAAAAAB002"

BASE_NAME = f"Game [{BASE_TITLE_ID}][v0].nsz"
UPDATE_NAME = f"Game Update [{UPDATE_TITLE_ID}][v65536].nsz"
DLC1_NAME = f"Game DLC1 [{DLC1_TITLE_ID}][v0].nsz"
DLC2_NAME = f"Game DLC2 [{DLC2_TITLE_ID}][v0].nsz"


def _backend():
    parent = MockMtpBackend(device_id="mock-switch-parent", device_name="Parent's Switch")
    parent.add_storage("SD_CARD")
    parent.add_storage("SD_INSTALL")
    registry = queue_worker.DeviceRegistry()
    registry.register("mock-switch-parent", parent)
    return parent, registry


def _make_archive_library_item(conn, library_dir, name: str, entries: dict[str, bytes]) -> int:
    path = build_zip(library_dir / name, entries)
    st = path.stat()
    return db.upsert_library_item(
        conn, absolute_path=str(path), item_type="FILE", file_type="ZIP",
        size=st.st_size, mtime=st.st_mtime, content_hash=f"hash-{name}",
        title_id=None, title_id_source=None, status="AVAILABLE",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        content_type="GAME_PACKAGE",
    )


def _confirm(conn, item_id: int, device_id: str = "mock-switch-parent") -> dict:
    return services.create_and_confirm_jobs(conn, [item_id], device_id)


def _title_ids(job_ids) -> list:
    return [manifest_mod.load_manifest(jid).title_id for jid in job_ids]


def _set_created_at(conn, job_id: int, iso: str) -> None:
    """Test-only: forces a deterministic iteration order regardless of how
    fast the test itself ran (db.now_iso() only has second resolution) --
    same technique as tests/test_install_order.py's own helper."""
    conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (iso, job_id))
    conn.commit()


def _run_until_idle(conn, registry, max_passes: int = 10) -> None:
    for _ in range(max_passes):
        if queue_worker.run_worker_once(conn, registry) is None:
            return
    raise AssertionError(f"worker still had processable work after {max_passes} passes")


# ---------------------------------------------------------------------------
# A-D: job counts for increasingly complex archives
# ---------------------------------------------------------------------------

def test_a_base_only_creates_one_job(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "base_only.zip", {BASE_NAME: b"base"})
    result = _confirm(conn, item_id)
    assert result["errors"] == []
    assert len(result["created"]) == 1
    assert _title_ids([c["job_id"] for c in result["created"]]) == [BASE_TITLE_ID]


def test_b_base_and_update_creates_two_jobs_in_order(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "bu.zip", {
        UPDATE_NAME: b"update",  # deliberately listed before the base in the archive itself
        BASE_NAME: b"base",
    })
    result = _confirm(conn, item_id)
    assert result["errors"] == []
    job_ids = [c["job_id"] for c in result["created"]]
    assert len(job_ids) == 2
    assert _title_ids(job_ids) == [BASE_TITLE_ID, UPDATE_TITLE_ID]  # Base always first, regardless of archive order


def test_c_base_update_dlc_creates_three_jobs(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "bud.zip", {
        BASE_NAME: b"base", UPDATE_NAME: b"update", DLC1_NAME: b"dlc1",
    })
    result = _confirm(conn, item_id)
    assert result["errors"] == []
    job_ids = [c["job_id"] for c in result["created"]]
    assert len(job_ids) == 3
    assert _title_ids(job_ids) == [BASE_TITLE_ID, UPDATE_TITLE_ID, DLC1_TITLE_ID]


def test_d_base_update_two_dlc_creates_four_jobs(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "budd.zip", {
        DLC2_NAME: b"dlc2", UPDATE_NAME: b"update", DLC1_NAME: b"dlc1", BASE_NAME: b"base",
    })
    result = _confirm(conn, item_id)
    assert result["errors"] == []
    job_ids = [c["job_id"] for c in result["created"]]
    assert len(job_ids) == 4
    # Base -> Update -> DLC, multiple DLCs ordered deterministically by name
    assert _title_ids(job_ids) == [BASE_TITLE_ID, UPDATE_TITLE_ID, DLC1_TITLE_ID, DLC2_TITLE_ID]


# ---------------------------------------------------------------------------
# E-G: batch/manifest identity, no silent drop
# ---------------------------------------------------------------------------

def test_e_all_package_jobs_share_one_batch_id(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "budd.zip", {
        BASE_NAME: b"base", UPDATE_NAME: b"update", DLC1_NAME: b"dlc1", DLC2_NAME: b"dlc2",
    })
    result = _confirm(conn, item_id)
    job_ids = [c["job_id"] for c in result["created"]]
    batch_id = result["batch_id"]
    assert batch_id is not None
    batch_jobs = db.list_jobs_by_batch(conn, batch_id)
    assert {j["id"] for j in batch_jobs} == set(job_ids)
    for jid in job_ids:
        assert db.get_job(conn, jid)["target_storage"] == "SD_INSTALL"
        assert db.get_job(conn, jid)["target_device_id"] == "mock-switch-parent"


def test_f_every_package_job_has_its_own_manifest_and_work_dir(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "bud.zip", {
        BASE_NAME: b"base payload", UPDATE_NAME: b"update payload", DLC1_NAME: b"dlc payload",
    })
    result = _confirm(conn, item_id)
    job_ids = [c["job_id"] for c in result["created"]]

    manifests = [manifest_mod.load_manifest(jid) for jid in job_ids]
    dest_names = [m.files[0].dest_relative_path for m in manifests]
    assert dest_names == [BASE_NAME, UPDATE_NAME, DLC1_NAME]
    assert len(set(dest_names)) == 3  # each job's payload is genuinely distinct

    # The batch's 3 jobs share ONE staging dir (their payloads are still 3
    # distinct files there, never duplicated) -- each job's OWN work dir
    # holds only its manifest, no payload copy.
    batch_dir = manifest_mod.batch_work_dir(result["batch_id"])
    for jid, expected_bytes in zip(job_ids, (b"base payload", b"update payload", b"dlc payload")):
        manifest = manifest_mod.load_manifest(jid)
        assert sorted(p.name for p in manifest_mod.job_work_dir(jid).iterdir()) == ["manifest.json"]
        staged = batch_dir / manifest.files[0].dest_relative_path
        assert staged.read_bytes() == expected_bytes


def test_g_no_installable_entry_is_silently_dropped(isolated_db):
    """Same archive as test_d, phrased as the mandate's own requirement:
    every recognized installable entry becomes a job -- literally none of
    Base/Update/DLC1/DLC2 is missing from the created list."""
    conn, _inbox = isolated_db
    from switchagent import config
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "budd.zip", {
        BASE_NAME: b"base", UPDATE_NAME: b"update", DLC1_NAME: b"dlc1", DLC2_NAME: b"dlc2",
    })
    result = _confirm(conn, item_id)
    assert result["errors"] == []
    job_ids = [c["job_id"] for c in result["created"]]
    assert set(_title_ids(job_ids)) == {BASE_TITLE_ID, UPDATE_TITLE_ID, DLC1_TITLE_ID, DLC2_TITLE_ID}


# ---------------------------------------------------------------------------
# H: failure ordering
# ---------------------------------------------------------------------------

def test_h_base_failure_blocks_same_batch_update_and_dlc(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    parent, registry = _backend()
    parent.arm_failure("error", storage="SD_INSTALL", dest_path=BASE_NAME)

    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "bud.zip", {
        BASE_NAME: b"base", UPDATE_NAME: b"update", DLC1_NAME: b"dlc1",
    })
    result = _confirm(conn, item_id)
    base_job, update_job, dlc_job = [c["job_id"] for c in result["created"]]

    outcome1 = queue_worker.run_worker_once(conn, registry)
    assert outcome1.job_id == base_job
    assert outcome1.status == "FAILED"

    _run_until_idle(conn, registry)

    assert db.get_job(conn, update_job)["status"] == "BLOCKED_BY_DEPENDENCY"
    assert db.get_job(conn, dlc_job)["status"] == "BLOCKED_BY_DEPENDENCY"
    assert parent.storage_tree("SD_INSTALL").list_files() == []  # neither was ever actually sent


# ---------------------------------------------------------------------------
# I/J: a currently-outstanding NEW Base outranks stale install_history, and
# DONE_UNVERIFIED is still a sufficient transport outcome once it finishes.
# This is the actual dependency-ordering bug fix (queue_worker._dependency_status
# reordered to check _has_outstanding_base_job before install_history).
# ---------------------------------------------------------------------------

def test_i_j_outstanding_new_base_outranks_old_history_then_unblocks_on_completion(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    parent, registry = _backend()

    # -- "old" install: Base only, finishes DONE_UNVERIFIED -- becomes History.
    old_item = _make_archive_library_item(conn, config.LIBRARY_DIR, "old_base.zip", {BASE_NAME: b"old base"})
    parent.arm_failure("unverified", storage="SD_INSTALL", dest_path=BASE_NAME)
    old_job = _confirm(conn, old_item)["created"][0]["job_id"]
    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome.job_id == old_job
    assert outcome.status == "DONE_UNVERIFIED"
    assert db.has_successful_base_install(conn, "mock-switch-parent", BASE_TITLE_ID)

    # user deletes the game from the Switch (mock: remove it from the
    # device's own storage) and re-selects Base+Update+DLC again.
    parent.storage_tree("SD_INSTALL").delete(BASE_NAME)
    parent.arm_failure("unverified", storage="SD_INSTALL", dest_path=BASE_NAME)  # the NEW base also lands DONE_UNVERIFIED

    new_item = _make_archive_library_item(conn, config.LIBRARY_DIR, "new_bud.zip", {
        BASE_NAME: b"new base", UPDATE_NAME: b"new update", DLC1_NAME: b"new dlc",
    })
    new_result = _confirm(conn, new_item)
    new_base_job, new_update_job, new_dlc_job = [c["job_id"] for c in new_result["created"]]
    # Force the new Update/DLC to be iterated BEFORE the still-CONFIRMED new
    # Base within the same worker pass -- proves this is enforced by actual
    # install state (an outstanding, non-terminal Base job), not merely by
    # creation/iteration order (mirrors test_install_order.py's own
    # established technique for the identical single-package regression).
    _set_created_at(conn, new_update_job, "2026-01-01T00:00:00+00:00")
    _set_created_at(conn, new_dlc_job, "2026-01-01T00:00:01+00:00")
    _set_created_at(conn, new_base_job, "2026-01-01T00:00:02+00:00")

    # Without the fix, has_successful_base_install (the OLD history row)
    # would let the new Update/DLC run immediately, ahead of the new Base.
    outcome2 = queue_worker.run_worker_once(conn, registry)
    assert outcome2.job_id == new_base_job, (
        "the new Base must run before its own batch's Update/DLC, even "
        "though install_history already shows an older successful Base install"
    )
    assert outcome2.status == "DONE_UNVERIFIED"
    assert db.get_job(conn, new_update_job)["status"] == "WAITING_FOR_BASE"
    assert db.get_job(conn, new_dlc_job)["status"] == "WAITING_FOR_BASE"

    # J: DONE_UNVERIFIED is still a sufficient transport outcome -- the new
    # Update/DLC proceed normally now that the new Base has finished.
    _run_until_idle(conn, registry)
    assert db.get_job(conn, new_update_job)["status"] in ("DONE", "DONE_UNVERIFIED")
    assert db.get_job(conn, new_dlc_job)["status"] in ("DONE", "DONE_UNVERIFIED")


# ---------------------------------------------------------------------------
# K: two unrelated game families in one archive must not block each other
# ---------------------------------------------------------------------------

def test_k_two_different_game_families_do_not_get_a_false_mutual_dependency(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    parent, registry = _backend()
    other_base_title_id = "0100BBBBBBBBB000"
    other_base_name = f"Other Game [{other_base_title_id}][v0].nsz"

    # Family A's Update, with no Base of its own anywhere in this archive/
    # queue/history, alongside family B's unrelated Base.
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "two_families.zip", {
        UPDATE_NAME: b"update for family A", other_base_name: b"base for family B",
    })
    result = _confirm(conn, item_id)
    assert result["errors"] == []
    job_ids = [c["job_id"] for c in result["created"]]
    assert len(job_ids) == 2

    _run_until_idle(conn, registry)

    for jid in job_ids:
        assert db.get_job(conn, jid)["status"] == "DONE"
    history = db.list_install_history(conn)
    assert not any(h["outcome"] == "BLOCKED_BY_DEPENDENCY" for h in history)


# ---------------------------------------------------------------------------
# Prepare-all-before-install, no duplicate staged payload, cleanup after a
# successful batch, staging survives a retryable failure.
# ---------------------------------------------------------------------------

def test_every_selected_archive_is_fully_prepared_before_any_job_is_confirmed(isolated_db, monkeypatch):
    """Two archives selected in one Confirm Install call: EVERY job must be
    created (extracted + manifest staged) before ANY of them is confirmed
    -- otherwise archive A's job could already be CONFIRMED (visible to the
    worker) while archive B is still being extracted in this same call."""
    conn, _inbox = isolated_db
    from switchagent import config
    other_base_name = "Other [0100CCCCCCCCC000][v0].nsz"
    item_a = _make_archive_library_item(conn, config.LIBRARY_DIR, "a.zip", {BASE_NAME: b"a"})
    item_b = _make_archive_library_item(conn, config.LIBRARY_DIR, "b.zip", {other_base_name: b"b"})

    calls = []
    real_create = queue_worker.create_job_from_report
    real_confirm = db.confirm_job

    def spy_create(*a, **k):
        calls.append("create")
        return real_create(*a, **k)

    def spy_confirm(conn_, job_id):
        calls.append("confirm")
        return real_confirm(conn_, job_id)

    monkeypatch.setattr(queue_worker, "create_job_from_report", spy_create)
    monkeypatch.setattr(services.db, "confirm_job", spy_confirm)

    result = services.create_and_confirm_jobs(conn, [item_a, item_b], "mock-switch-parent")
    assert result["errors"] == []
    assert calls == ["create", "create", "confirm", "confirm"]


def test_frozen_payload_lives_once_under_the_batch_dir_not_the_job_dir(isolated_db):
    """No second physical copy: the extracted package must live exactly
    once, under the batch's own shared staging dir -- job_work_dir(job_id)
    holds only manifest.json (no gigabyte-sized payload)."""
    conn, _inbox = isolated_db
    from switchagent import config
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "single.zip", {BASE_NAME: b"payload bytes"})
    result = _confirm(conn, item_id)
    job_id = result["created"][0]["job_id"]
    batch_id = result["batch_id"]

    job_dir = manifest_mod.job_work_dir(job_id)
    assert sorted(p.name for p in job_dir.iterdir()) == ["manifest.json"]

    batch_dir = manifest_mod.batch_work_dir(batch_id)
    assert (batch_dir / BASE_NAME).read_bytes() == b"payload bytes"


def test_successful_batch_cleans_up_staging_but_keeps_library_source_and_history(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    parent, registry = _backend()
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "single.zip", {BASE_NAME: b"payload"})
    result = _confirm(conn, item_id)
    job_id = result["created"][0]["job_id"]
    batch_dir = manifest_mod.batch_work_dir(result["batch_id"])
    assert batch_dir.exists()

    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome.status == "DONE"

    assert not batch_dir.exists()  # staging cleaned up
    assert (config.LIBRARY_DIR / "single.zip").exists()  # original archive untouched
    history = db.list_install_history(conn)
    assert len(history) == 1 and history[0]["outcome"] == "DONE" and history[0]["job_id"] == job_id


def test_interrupted_job_keeps_staging_until_retry_succeeds_then_cleans_up(isolated_db):
    conn, _inbox = isolated_db
    from switchagent import config
    parent, registry = _backend()
    parent.arm_failure("crash", storage="SD_INSTALL", dest_path=BASE_NAME)
    item_id = _make_archive_library_item(conn, config.LIBRARY_DIR, "single.zip", {BASE_NAME: b"payload"})
    result = _confirm(conn, item_id)
    job_id = result["created"][0]["job_id"]
    batch_dir = manifest_mod.batch_work_dir(result["batch_id"])

    outcome = queue_worker.run_worker_once(conn, registry)
    assert outcome.status == "INTERRUPTED"
    assert batch_dir.exists()  # preserved -- retry may still need it

    db.retry_job(conn, job_id)  # low-level in-place resume, reuses the staged payload
    outcome2 = queue_worker.run_worker_once(conn, registry)
    assert outcome2.status == "DONE"
    assert not batch_dir.exists()  # cleaned up now that the batch is fully done
