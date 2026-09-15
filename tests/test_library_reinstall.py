from datetime import datetime, timedelta, timezone
import pytest
from switchagent import db
from .test_web_api import web_ctx, client, _seed_library_item


@pytest.mark.parametrize("status", ["DONE", "DONE_UNVERIFIED"])
def test_completed_transfer_remains_selectable_and_can_be_queued_again(client, web_ctx, status):
    item = _seed_library_item(web_ctx)
    payload = {"library_item_ids": [item], "target_device_id": "mock-switch-parent"}
    first = client.post("/api/jobs", json=payload).json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, first, status, finished_at=db.now_iso())
    entry = client.get(f"/api/library/{item}").json()
    assert entry["can_install"] and entry["recent_transfer_until"]
    assert "recent-transfer" in client.get("/").text
    second = client.post("/api/jobs", json=payload).json()
    assert not second["errors"] and second["created"][0]["job_id"] != first
    assert not client.get(f"/api/library/{item}").json()["can_install"]


def test_highlight_expires_without_losing_reinstall_or_history(client, web_ctx):
    item = _seed_library_item(web_ctx)
    job = client.post("/api/jobs", json={"library_item_ids": [item], "target_device_id": "mock-switch-parent"}).json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job, "DONE_UNVERIFIED", finished_at=(datetime.now(timezone.utc)-timedelta(minutes=21)).isoformat())
    entry = client.get(f"/api/library/{item}").json()
    assert entry["can_install"]
    assert entry["recent_transfer_until"] is None
    assert entry["job"]["status"] == "DONE_UNVERIFIED"
