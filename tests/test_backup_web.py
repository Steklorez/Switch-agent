"""Backup API seams: HTTP queues work, the existing device worker owns MTP."""

import threading
import time
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient

from switchagent import db
from switchagent.web import app as app_module
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context


def _ready_job(client, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = client.get(f"/api/backups/jobs/{job_id}").json()
        if row["state"] in ("ready", "failed", "cancelled"):
            return row
        time.sleep(.02)
    raise AssertionError("backup job did not finish")


def test_inventory_request_only_uses_existing_device_worker(tmp_path):
    db_path = tmp_path / "app.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path, device_ids=["mock-switch-parent"])
    backend = ctx.registry.get("mock-switch-parent")
    calls = []
    original = backend.list_directory

    def observed(*args, **kwargs):
        calls.append(threading.current_thread().name)
        return original(*args, **kwargs)

    backend.list_directory = observed
    client = TestClient(create_app(ctx))
    ctx.start_worker()
    try:
        response = client.post("/api/backups/inventory", json={"device_id": "mock-switch-parent", "kind": "saves"})
        assert response.status_code == 202, response.text
        row = _ready_job(client, response.json()["job_id"])
        assert row["state"] == "ready"
        assert calls and set(calls) == {"switchagent-worker"}
    finally:
        ctx.stop_worker()


def test_selected_save_downloads_as_valid_archive(tmp_path):
    db_path = tmp_path / "app.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path, device_ids=["mock-switch-parent"])
    client = TestClient(create_app(ctx))
    ctx.start_worker()
    try:
        created = client.post("/api/backups/snapshots", json={
            "device_id": "mock-switch-parent", "paths": ["Installed games/Demo Adventure/Player"]})
        assert created.status_code == 202, created.text
        snapshot_job = _ready_job(client, created.json()["job_id"])
        assert snapshot_job["state"] == "ready", snapshot_job["error"]
        snapshot_id = snapshot_job["result"][0]["id"]
        archived = client.post("/api/backups/archives", json={"snapshot_ids": [snapshot_id]})
        assert archived.status_code == 202, archived.text
        archive_job = _ready_job(client, archived.json()["job_id"])
        assert archive_job["state"] == "ready", archive_job["error"]
        downloaded = client.get(archive_job["result"]["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.content[:2] == b"PK"
        with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
            assert "manifest.json" in archive.namelist()
        assert client.get("/api/backups/download/" + "0" * 32).status_code == 404
    finally:
        ctx.stop_worker()


def test_upload_import_and_restore_use_worker_and_keep_target_prebackup(tmp_path):
    db_path = tmp_path / "app.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path, device_ids=["mock-switch-parent"])
    backend = ctx.registry.get("mock-switch-parent")
    backend.connect()
    storage = backend._storages["SAVES"]
    save_path = "Installed games/Demo Adventure/Player"
    file_path = save_path + "/progress.dat"
    client = TestClient(create_app(ctx))
    ctx.start_worker()
    try:
        snap = client.post("/api/backups/snapshots", json={"device_id": "mock-switch-parent", "paths": [save_path]})
        snap_job = _ready_job(client, snap.json()["job_id"])
        assert snap_job["state"] == "ready", snap_job["error"]
        archive = client.post("/api/backups/archives", json={"snapshot_ids": [snap_job["result"][0]["id"]]})
        archive_job = _ready_job(client, archive.json()["job_id"])
        payload = client.get(archive_job["result"]["download_url"]).content
        storage.write_file(file_path, b"new progress\n")

        upload = client.post("/api/backups/upload", content=payload, headers={"Content-Type": "application/zip"})
        assert upload.status_code == 202, upload.text
        imported = _ready_job(client, upload.json()["job_id"])
        assert imported["state"] == "ready", imported["error"]
        assert "params" not in imported
        imported_id = imported["result"][0]["id"]
        assert not list((ctx.backup_root / ".partial" / "import").glob("*.zip"))

        prepared = client.post("/api/backups/restores/prepare", json={
            "device_id": "mock-switch-parent", "snapshot_id": imported_id,
            "target_path": save_path})
        assert prepared.status_code == 202, prepared.text
        plan_job = _ready_job(client, prepared.json()["job_id"])
        assert plan_job["state"] == "ready", plan_job["error"]
        plan = plan_job["result"]
        assert plan["prebackup_id"] in {row["id"] for row in ctx.backup_state()["catalog"]["snapshots"]}
        assert plan["requires_profile_confirmation"] is False
        assert ctx.backup_state()["reservations"]["mock-switch-parent"]["plan_id"] == plan["id"]
        confirm = client.post("/api/backups/restores/confirm", json={
            "device_id": "mock-switch-parent", "plan_id": plan["id"]})
        assert confirm.status_code == 202, confirm.text
        confirm_job = _ready_job(client, confirm.json()["job_id"])
        assert confirm_job["state"] == "ready", confirm_job["error"]
        assert confirm_job["result"]["verification"] == "readback-hash"
        assert storage.read_file(file_path) == b"SwitchAgent mock save progress\n"
        assert not ctx.backup_state()["reservations"]
    finally:
        ctx.stop_worker()


def test_cross_profile_requires_exact_name_and_preserves_plan_on_wrong_name(tmp_path):
    db_path = tmp_path / "app.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path, device_ids=["mock-switch-parent"])
    backend = ctx.registry.get("mock-switch-parent")
    backend.connect()
    storage = backend._storages["SAVES"]
    source = "Installed games/Demo Adventure/Player"
    target = "Installed games/Demo Adventure/Guest"
    storage.ensure_directory(target)
    storage.write_file(target + "/progress.dat", b"guest progress\n")
    backend.set_save_identity("SAVES", target, title_id="0100000000010000",
                              user_id="mock-user-2", environment_id="mock-switch-parent-nand-1")
    client = TestClient(create_app(ctx))
    ctx.start_worker()
    try:
        snap = client.post("/api/backups/snapshots", json={"device_id": "mock-switch-parent", "paths": [source]})
        snapshot_id = _ready_job(client, snap.json()["job_id"])["result"][0]["id"]
        prepare = client.post("/api/backups/restores/prepare", json={
            "device_id": "mock-switch-parent", "snapshot_id": snapshot_id, "target_path": target})
        plan_job = _ready_job(client, prepare.json()["job_id"])
        assert plan_job["state"] == "ready", plan_job["error"]
        plan = plan_job["result"]
        assert plan["requires_profile_confirmation"] is True
        assert plan["target_profile"] == "Guest"

        wrong = client.post("/api/backups/restores/confirm", json={
            "device_id": "mock-switch-parent", "plan_id": plan["id"], "confirm_profile": "guest"})
        assert wrong.status_code == 409
        assert ctx.backup_state()["reservations"]["mock-switch-parent"]["state"] == "prepared"
        assert storage.read_file(target + "/progress.dat") == b"guest progress\n"

        exact = client.post("/api/backups/restores/confirm", json={
            "device_id": "mock-switch-parent", "plan_id": plan["id"], "confirm_profile": "Guest"})
        assert exact.status_code == 202, exact.text
        outcome = _ready_job(client, exact.json()["job_id"])
        assert outcome["state"] == "ready", outcome["error"]
        assert storage.read_file(target + "/progress.dat") == b"SwitchAgent mock save progress\n"
        assert not ctx.backup_state()["reservations"]
    finally:
        ctx.stop_worker()


def test_game_download_and_upload_validation(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path, device_ids=["mock-switch-parent"])
    client = TestClient(create_app(ctx))
    ctx.start_worker()
    try:
        game = client.post("/api/backups/games", json={"device_id": "mock-switch-parent",
             "paths": ["Demo Adventure [0100000000010000][v0].nsp"]})
        assert game.status_code == 202, game.text
        game_job = _ready_job(client, game.json()["job_id"])
        assert game_job["state"] == "ready", game_job["error"]
        export_id = game_job["result"][0]["id"]
        assert client.get("/api/backups/download/" + export_id).content == b"mock game package\n"
        assert client.post("/api/backups/games/../../download").status_code == 404
        assert client.post("/api/backups/snapshots", json={"device_id": "mock-switch-parent", "paths": []}).status_code == 422
        assert client.post("/api/backups/snapshots", json={"device_id": "mock-switch-parent", "paths": ["../outside"]}).status_code == 422
        assert client.post("/api/backups/archives", json={"snapshot_ids": ["../../outside"]}).status_code == 422
        assert client.post("/api/backups/restores/prepare", json={"device_id": "mock-switch-parent",
             "snapshot_id": "0" * 32, "target_path": "C:\\outside"}).status_code == 422
        assert client.post("/api/backups/upload", content=b"anything", headers={"Content-Type": "text/plain"}).status_code == 415
        assert client.post("/api/backups/upload", content=b"", headers={"Content-Type": "application/zip"}).status_code == 422
        invalid = client.post("/api/backups/upload", content=b"invalid zip", headers={"Content-Type": "application/zip"})
        assert invalid.status_code == 202
        invalid_job = _ready_job(client, invalid.json()["job_id"])
        assert invalid_job["state"] == "failed"
        monkeypatch.setattr(app_module, "_MAX_BACKUP_UPLOAD_BYTES", 8)
        assert client.post("/api/backups/upload", content=b"0123456789", headers={"Content-Type": "application/zip"}).status_code == 413
        assert not list((ctx.backup_root / ".partial" / "import").glob("*.zip"))
    finally:
        ctx.stop_worker()


def test_cancel_queued_prepare_releases_reservation_without_device_write(tmp_path):
    db_path = tmp_path / "app.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path, device_ids=["mock-switch-parent"])
    backend = ctx.registry.get("mock-switch-parent")
    ctx.worker_paused.set()
    client = TestClient(create_app(ctx))
    ctx.start_worker()
    try:
        prepared = client.post("/api/backups/restores/prepare", json={
            "device_id": "mock-switch-parent", "snapshot_id": "0" * 32})
        assert prepared.status_code == 202, prepared.text
        job_id = prepared.json()["job_id"]
        assert ctx.backup_state()["reservations"]["mock-switch-parent"]["state"] == "preparing"
        assert client.post(f"/api/backups/jobs/{job_id}/cancel").json() == {"cancel_requested": True}
        assert client.get(f"/api/backups/jobs/{job_id}").json()["state"] == "cancelled"
        assert not ctx.backup_state()["reservations"]
        assert not any(row.operation in ("WRITE_SAVE_FILE", "DELETE_SAVE_OBJECT", "CREATE_SAVE_DIRECTORY")
                       for row in backend.operation_log)
    finally:
        ctx.stop_worker()


def test_running_restore_confirm_refuses_job_cancellation(tmp_path):
    db_path = tmp_path / "app.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path, device_ids=["mock-switch-parent"])
    backend = ctx.registry.get("mock-switch-parent")
    backend.connect()
    save_path = "Installed games/Demo Adventure/Player"
    client = TestClient(create_app(ctx))
    ctx.start_worker()
    try:
        snap = client.post("/api/backups/snapshots", json={"device_id": "mock-switch-parent", "paths": [save_path]})
        snapshot_id = _ready_job(client, snap.json()["job_id"])["result"][0]["id"]
        prepared = client.post("/api/backups/restores/prepare", json={
            "device_id": "mock-switch-parent", "snapshot_id": snapshot_id})
        plan = _ready_job(client, prepared.json()["job_id"])["result"]
        entered = threading.Event()
        release = threading.Event()
        original = ctx.backups.confirm_restore

        def held_confirm(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        ctx.backups.confirm_restore = held_confirm
        confirm = client.post("/api/backups/restores/confirm", json={
            "device_id": "mock-switch-parent", "plan_id": plan["id"]})
        job_id = confirm.json()["job_id"]
        assert entered.wait(5)
        assert client.post(f"/api/backups/jobs/{job_id}/cancel").status_code == 409
        assert ctx.backup_state()["reservations"]["mock-switch-parent"]["state"] == "confirming"
        release.set()
        assert _ready_job(client, job_id)["state"] == "ready"
    finally:
        release.set()
        ctx.stop_worker()
