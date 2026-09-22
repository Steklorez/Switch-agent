"""Backup API seams: HTTP queues work, the existing device worker owns MTP."""

import threading
import time

from fastapi.testclient import TestClient

from switchagent import db
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
            "device_id": "mock-switch-parent", "paths": ["Demo Adventure/Player/Default"]})
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
    finally:
        ctx.stop_worker()
