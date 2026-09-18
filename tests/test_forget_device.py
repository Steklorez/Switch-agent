"""Forgetting a device: POST /api/devices/by-fingerprint/{fp}/forget.

Why this action exists at all -- "has ever been seen by this server" is
not the same as "is one of this user's Switches". Two real ways a row
nobody wants ends up on the Devices page:

  * a mock-mode run against the real database seeded "Parent's Switch
    (mock)" / "Child's Switch (mock)" into it (2026-09-17 -- see
    tests/test_mock_db_isolation.py for the fix that makes that specific
    accident impossible now, and config.MOCK_DB_PATH's comment);
  * one console can enumerate under two identities -- the stock firmware's
    own MTP, with a blanked USB serial, before DBI is started, then DBI's
    own -- leaving a permanent second row for a Switch that is already
    listed.

Until this existed, neither could be removed without editing SQLite by
hand. The contracts under test are what keeps "forget" honest:

  * it erases SwitchAgent's OWN memory of the device (row, friendly name,
    storage mappings, cached installed-title list) and nothing else --
    Queue/History records that mention it survive untouched;
  * it refuses in the two states where it would be wrong (device connected
    right now, unfinished jobs still targeting it) with a 409 whose
    message is written to be shown to the user;
  * it is not a blocklist -- the same device seen again is recorded again,
    as a new device.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders
from switchagent.mtp.windows import device_fingerprint
from switchagent.web import services
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

PARENT = "mock-switch-parent"  # registered in the mock registry -> connected
GHOST = "ghost-switch"  # only ever a devices row -> disconnected
GHOST_FP = device_fingerprint(GHOST)
PARENT_FP = device_fingerprint(PARENT)

TITLE_ID = "0100000000010000"


@pytest.fixture
def web_ctx(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)

    db_path = tmp_path / "test.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path)
    with db.open_db(db_path) as conn:
        ctx.refresh_devices(conn)
        # A device SwitchAgent has seen but that is not in the registry --
        # i.e. exactly the shape of the stale rows this action is for.
        db.upsert_device_seen(conn, GHOST, "Nintendo Switch")
    yield ctx


@pytest.fixture
def client(web_ctx):
    return TestClient(create_app(web_ctx))


def _seed_job(web_ctx, *, device_id, status=None, name="ZzzQuest"):
    """One library item + one job targeting `device_id`. Left at its default
    PENDING_CONFIRM status unless `status` says otherwise."""
    path = config.LIBRARY_DIR / f"{name} [{TITLE_ID}][v0].nsp"
    path.write_bytes(b"payload")
    with db.open_db(web_ctx.db_path) as conn:
        item_id = db.upsert_library_item(
            conn, absolute_path=str(path), item_type="FILE", file_type="NSP",
            size=path.stat().st_size, mtime=path.stat().st_mtime, content_hash=name,
            title_id=TITLE_ID, title_id_source="filename", status="AVAILABLE",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE", package_format="NSP",
        )
        batch_id = db.create_installation_batch(conn, target_device_id=device_id)
        job_id = db.create_job(
            conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
            target_device_id=device_id, library_item_id=item_id, batch_id=batch_id,
        )
        if status is not None:
            db.update_job_status(conn, job_id, status)
    return job_id


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_forgetting_a_disconnected_device_removes_it_and_everything_derived(client, web_ctx):
    with db.open_db(web_ctx.db_path) as conn:
        db.set_device_friendly_name(conn, GHOST, "Old Switch")
        db.set_device_storage_mapping(conn, GHOST, "SD Card", "SD_CARD")
        db.set_device_installed_base_title_ids(conn, GHOST, {TITLE_ID})

    res = client.post(f"/api/devices/by-fingerprint/{GHOST_FP}/forget")
    assert res.status_code == 200, res.text

    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_device(conn, GHOST) is None
        assert db.list_device_storage_mappings(conn, GHOST) == []
        remaining = conn.execute(
            "SELECT COUNT(*) FROM device_installed_titles WHERE device_id = ?", (GHOST,),
        ).fetchone()[0]
        assert remaining == 0
    # and it is gone from the page the user was looking at
    assert GHOST_FP not in client.get("/devices").text


def test_forgetting_leaves_queue_and_history_records_alone(client, web_ctx):
    """A device row is SwitchAgent's memory of an identity; install_history
    is its record of what it actually did. Forgetting the first must never
    quietly rewrite the second."""
    job_id = _seed_job(web_ctx, device_id=GHOST, status="DONE")
    with db.open_db(web_ctx.db_path) as conn:
        db.record_install_history(
            conn, job_id=job_id, title_id=TITLE_ID, display_name="ZzzQuest",
            target_device_id=GHOST, target_storage="SD_INSTALL", outcome="DONE",
            error=None, bytes_total=1000,
        )

    assert client.post(f"/api/devices/by-fingerprint/{GHOST_FP}/forget").status_code == 200

    with db.open_db(web_ctx.db_path) as conn:
        history = conn.execute(
            "SELECT * FROM install_history WHERE target_device_id = ?", (GHOST,),
        ).fetchall()
        assert len(history) == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == 1
        # History stays readable: device_label() already falls back to the
        # safe fingerprint for a device row that no longer exists, so the
        # page shows a stand-in, never a raw serial and never a crash.
        assert services.device_label(conn, GHOST) == f"Switch ({GHOST_FP})"

    assert client.get("/history").status_code == 200


def test_a_forgotten_device_is_recorded_again_if_it_is_ever_seen_again(client, web_ctx):
    """"Forget" is not "blocklist" -- and what comes back is a NEW device,
    with no friendly name carried over from the old row."""
    with db.open_db(web_ctx.db_path) as conn:
        db.set_device_friendly_name(conn, GHOST, "Old Switch")
    assert client.post(f"/api/devices/by-fingerprint/{GHOST_FP}/forget").status_code == 200

    with db.open_db(web_ctx.db_path) as conn:
        db.upsert_device_seen(conn, GHOST, "Nintendo Switch")
        row = db.get_device(conn, GHOST)

    assert row is not None
    assert row["friendly_name"] is None


# ---------------------------------------------------------------------------
# the two refusals
# ---------------------------------------------------------------------------

def test_refuses_to_forget_a_device_that_is_connected_right_now(client, web_ctx):
    """It would be re-recorded by the next refresh_devices() within seconds
    -- the row would visibly come back and read as a bug."""
    res = client.post(f"/api/devices/by-fingerprint/{PARENT_FP}/forget")

    assert res.status_code == 409
    assert "connected right now" in res.json()["detail"]
    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_device(conn, PARENT) is not None


def test_refuses_while_unfinished_jobs_still_target_the_device(client, web_ctx):
    _seed_job(web_ctx, device_id=GHOST)  # PENDING_CONFIRM -- still in the Queue view

    res = client.post(f"/api/devices/by-fingerprint/{GHOST_FP}/forget")

    assert res.status_code == 409
    assert "Queue" in res.json()["detail"]
    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_device(conn, GHOST) is not None


def test_a_settled_job_does_not_block_forgetting(client, web_ctx):
    """Only work still in the Queue view blocks -- the same _not_settled()
    predicate the Queue itself uses. A finished job is history, not work."""
    _seed_job(web_ctx, device_id=GHOST, status="DONE")

    assert client.post(f"/api/devices/by-fingerprint/{GHOST_FP}/forget").status_code == 200


def test_an_unknown_fingerprint_is_404_not_409(client):
    """404 "no such device" and 409 "exists, but not now" are a real
    distinction for the caller, not decoration."""
    res = client.post("/api/devices/by-fingerprint/deadbeefdeadbeef/forget")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# the button
# ---------------------------------------------------------------------------

def test_the_forget_button_is_offered_only_on_disconnected_rows(client):
    html = client.get("/devices").text

    assert f'data-fingerprint="{GHOST_FP}"' in html
    assert f'data-fingerprint="{PARENT_FP}"' not in html


def test_the_button_carries_the_fingerprint_never_the_raw_device_id(client):
    """W3-004: the raw, serial-bearing device_id must not reach the request
    path (it lands verbatim in uvicorn's access log). The rename form still
    carries it as a form value, which is the pre-existing, deliberate
    exception -- this new action must not add a second one."""
    html = client.get("/devices").text
    forget_button = html[html.index("device-forget"):]
    forget_button = forget_button[:forget_button.index("</button>")]

    assert GHOST_FP in forget_button
    assert GHOST not in forget_button
