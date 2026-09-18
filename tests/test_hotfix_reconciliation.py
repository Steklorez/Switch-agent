"""Field regressions through the same HTTP routes the UI uses; mock only."""
import threading
import time
from types import SimpleNamespace

import pytest

from switchagent import config, db, extractor, manifest, queue_worker
from switchagent.web import preparation, services
from .test_web_api import client, web_ctx, _seed_library_item  # shared isolated HTTP fixtures
from .test_multi_package_archive import (
    _make_archive_library_item, BASE_NAME, UPDATE_NAME, DLC1_NAME,
)


def seed(ctx, name, entries):
    with db.open_db(ctx.db_path) as conn:
        return _make_archive_library_item(conn, config.LIBRARY_DIR, name, entries)


def confirm(client, ids):
    return client.post("/api/jobs", json={"library_item_ids": ids, "target_device_id": "mock-switch-parent"})


def wait_preparation(client):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = client.get("/api/preparations").json()[-1]
        if state["phase"] in ("Ready", "Failed"):
            return state
        time.sleep(.02)
    raise AssertionError("preparation did not finish")


def test_async_install_cleans_first_archive_before_extracting_next(client, web_ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'LOGS_DIR', tmp_path / 'logs')
    a = seed(web_ctx, "a.zip", {BASE_NAME: b"base", UPDATE_NAME: b"update", DLC1_NAME: b"dlc", "readme.txt": b"extra"})
    other = "Other [0100BBBBBBBBB000][v0].nsz"
    b = seed(web_ctx, "b.zip", {other: b"other"})
    reached, release = threading.Event(), threading.Event()
    calls = []
    original = extractor.safe_extract

    def extract(path, *args, **kwargs):
        calls.append(path.name)
        if path.name == "b.zip":
            reached.set()
            assert release.wait(10)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(extractor, "safe_extract", extract)
    try:
        response = client.post("/api/preparations", json={"library_item_ids": [a, b], "target_device_id": "mock-switch-parent"})
        assert response.status_code == 202
        deadline = time.monotonic() + 5
        while not reached.is_set() and time.monotonic() < deadline:
            with db.open_db(web_ctx.db_path) as conn:
                queue_worker.run_worker_once(conn, web_ctx.registry)
            time.sleep(.02)
        assert reached.is_set()
        assert not list(config.WORK_DIR.glob('batch-*'))
        state = client.get("/api/preparations").json()[0]
        assert state["items"][str(a)]["phase"] == "Ready"
        assert state["items"][str(b)]["phase"] == "Extracting"
        assert "preparation-list" in client.get("/queue").text
        # A restore must not replace SQLite underneath this preparation.
        assert client.post("/api/restore", files={"file": ("backup.zip", b"unused")}).status_code == 409
        with db.open_db(web_ctx.db_path) as conn:
            assert queue_worker.run_worker_once(conn, web_ctx.registry) is None
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with db.open_db(web_ctx.db_path) as conn:
            queue_worker.run_worker_once(conn, web_ctx.registry)
        state = client.get('/api/preparations').json()[-1]
        if state['phase'] in ('Ready', 'Failed'):
            break
        time.sleep(.02)
    assert state["phase"] == "Ready"
    assert calls == ["a.zip", "b.zip"]
    jobs = state["result"]["created"]
    assert len(jobs) == 4
    with db.open_db(web_ctx.db_path) as conn:
        assert len(db.list_install_history(conn)) == 4
    assert not list(config.WORK_DIR.glob("batch-*"))
    assert all(p.name.startswith("job-") for p in config.WORK_DIR.iterdir())
    assert len(list(config.LIBRARY_DIR.glob("*.zip"))) == 2


def test_second_archive_failure_blocks_prepared_first_and_releases_payload(client, web_ctx, monkeypatch):
    a = seed(web_ctx, "a.zip", {BASE_NAME: b"base"})
    b = seed(web_ctx, "b.zip", {UPDATE_NAME: b"update"})
    original = extractor.safe_extract

    def broken(path, *args, **kwargs):
        if path.name == "b.zip":
            raise extractor.CorruptArchiveError("bad CRC")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(extractor, "safe_extract", broken)
    response = confirm(client, [a, b])
    assert response.status_code == 422 and not response.json()["created"]
    with db.open_db(web_ctx.db_path) as conn:
        assert queue_worker.run_worker_once(conn, web_ctx.registry) is None
        assert not db.list_install_history(conn)
    assert not list(config.WORK_DIR.glob("batch-*"))


def test_preflight_expanded_size_blocks_before_extraction(client, web_ctx, monkeypatch):
    a = seed(web_ctx, "large.zip", {BASE_NAME: b"x" * (20 * 1024 * 1024)})
    monkeypatch.setattr(preparation.shutil, "disk_usage", lambda path: SimpleNamespace(free=preparation.RESERVE_BYTES + 1024))
    def forbidden(*args, **kwargs):
        raise AssertionError("no extraction before disk preflight")
    monkeypatch.setattr(extractor, "safe_extract", forbidden)
    response = confirm(client, [a])
    assert response.status_code == 422
    assert "Insufficient workspace" in response.json()["errors"][0]["error"]


def test_ui_retry_exact_update_after_restart_reuses_payload_and_cleans_history_safe(client, web_ctx, monkeypatch):
    item = seed(web_ctx, "all.zip", {BASE_NAME: b"base", UPDATE_NAME: b"update", DLC1_NAME: b"dlc"})
    result = confirm(client, [item]).json()
    base, update, dlc = [entry["job_id"] for entry in result["created"]]
    backend = web_ctx.registry.get("mock-switch-parent")
    backend.arm_failure("disconnect", storage="SD_INSTALL", dest_path=UPDATE_NAME)
    with db.open_db(web_ctx.db_path) as conn:
        assert queue_worker.run_worker_once(conn, web_ctx.registry).job_id == base
        assert queue_worker.run_worker_once(conn, web_ctx.registry).status == "INTERRUPTED"
        queue_worker.run_worker_once(conn, web_ctx.registry)
        before = dict(db.get_job(conn, update))
    original_manifest = manifest.load_manifest(update)
    source = manifest.resolve_source_path(original_manifest.files[0], update, batch_id=original_manifest.batch_id)
    payload = source.read_bytes()
    source.write_bytes(b"X" * len(payload))
    assert client.post(f"/api/jobs/{update}/retry").status_code == 409
    source.unlink()
    assert client.post(f"/api/jobs/{update}/retry").status_code == 409
    source.write_bytes(payload)
    def forbidden(*args, **kwargs):
        raise AssertionError("UI retry must not extract again")
    monkeypatch.setattr(extractor, "safe_extract", forbidden)
    # New connection and reloaded manifest stand in for process restart.
    response = client.post(f"/api/jobs/{update}/retry")
    assert response.status_code == 200
    new_id = response.json()["new_job_id"]
    assert manifest.load_manifest(new_id) == original_manifest
    with db.open_db(web_ctx.db_path) as conn:
        assert dict(db.get_job(conn, update)) == before
        assert queue_worker.run_worker_once(conn, web_ctx.registry).status == "DONE"
        assert len(db.list_install_history(conn)) == 4
    assert not source.exists()
    assert (config.LIBRARY_DIR / "all.zip").exists()


def test_colliding_names_are_single_copies_and_cancel_releases_only_after_last_reference(client, web_ctx):
    a = seed(web_ctx, "a.zip", {BASE_NAME: b"a" * (20 * 1024 * 1024)})
    b = seed(web_ctx, "b.zip", {BASE_NAME: b"b"})
    result = confirm(client, [a, b]).json()
    jobs = [c["job_id"] for c in result["created"]]
    paths = []
    for job in jobs:
        m = manifest.load_manifest(job)
        paths.append(manifest.resolve_source_path(m.files[0], job, batch_id=m.batch_id))
        assert [p.name for p in manifest.job_work_dir(job).iterdir()] == ["manifest.json"]
    assert paths[0] != paths[1]
    assert paths[0].read_bytes() == b"a" * (20 * 1024 * 1024)
    assert paths[1].read_bytes() == b"b"
    assert sum(p.stat().st_size for p in config.WORK_DIR.rglob("*.nsz")) == 20 * 1024 * 1024 + 1
    assert client.post(f"/api/jobs/{jobs[0]}/cancel").status_code == 200
    assert paths[1].exists()
    assert client.post(f"/api/jobs/{jobs[1]}/cancel").status_code == 200
    assert not paths[0].exists() and not paths[1].exists()
    assert len(list(config.LIBRARY_DIR.glob("*.zip"))) == 2


def test_preparation_panel_shows_the_override_retry_not_the_stale_conflict(client, web_ctx):
    """Real user report, 2026-09-18 (screenshot on the Queue page): a
    batch of 3 items finished with item 3 hitting a DESTINATION_CONFLICT.
    Clicking Override created a new job (correctly), but the preparation
    panel kept showing item 3's OLD conflict card -- full detail box,
    live Override/Skip buttons -- indefinitely, while the actual new job
    ran as a seemingly unrelated row elsewhere on the page. Worse: since
    the old job's status never changes after a retry, that stale card's
    Override button stayed genuinely clickable and (before this fix)
    would have created ANOTHER new job every time -- see
    test_override_refuses_a_second_click_on_the_same_stale_conflict_card
    in test_web_api.py for that half. This test is the panel-display
    half: once overridden, the item's own entry in /api/preparations must
    reflect the NEW job, not the superseded original."""
    item_id = _seed_library_item(web_ctx)
    backend = web_ctx.registry.get("mock-switch-parent")
    backend.connect()
    backend.storage_tree("SD_INSTALL").write_file(
        "Game [0100000000010000][v0].nsp", b"someone else's file",
    )

    response = client.post(
        "/api/preparations", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"},
    )
    assert response.status_code == 202
    deadline = time.monotonic() + 5
    state = None
    while time.monotonic() < deadline:
        with db.open_db(web_ctx.db_path) as conn:
            queue_worker.run_worker_once(conn, web_ctx.registry)
        state = client.get("/api/preparations").json()[-1]
        if state["phase"] == "Failed":
            break
        time.sleep(.02)
    assert state["phase"] == "Failed"

    item = state["items"][str(item_id)]
    assert len(item["jobs"]) == 1
    stale_job_id = item["jobs"][0]["id"]
    assert item["jobs"][0]["status"] == "DESTINATION_CONFLICT"

    override_res = client.post(f"/api/jobs/{stale_job_id}/override")
    assert override_res.status_code == 200
    new_job_id = override_res.json()["new_job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)

    state = client.get("/api/preparations").json()[-1]
    item = state["items"][str(item_id)]
    assert len(item["jobs"]) == 1
    assert item["jobs"][0]["id"] == new_job_id
    assert item["jobs"][0]["status"] in ("DONE", "DONE_UNVERIFIED")
    assert item["jobs"][0]["status"] != "DESTINATION_CONFLICT"


def test_archived_mods_have_distinct_resolvable_frozen_paths(client, web_ctx):
    title = "0100AAAAAAAAA000"
    name = f"atmosphere/contents/{title}/romfs/text.bin"
    a = seed(web_ctx, "mod-a.zip", {name: b"first"})
    b = seed(web_ctx, "mod-b.zip", {name: b"second"})
    result = confirm(client, [a, b]).json()
    assert len(result["created"]) == 2
    paths = []
    for entry, expected in zip(result["created"], (b"first", b"second")):
        job = entry["job_id"]
        m = manifest.load_manifest(job)
        assert manifest.verify_manifest_against_source(m, job) is None
        path = manifest.resolve_source_path(m.files[0], job, batch_id=m.batch_id)
        assert path.read_bytes() == expected
        paths.append(path)
    assert paths[0] != paths[1]
