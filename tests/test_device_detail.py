"""W3-004: the Device Details page (`GET /devices/{fingerprint}`), plus the
W3-006 Destination-Conflict-UX link fix that page enabled.

Same MockMtpBackend web fixture style as tests/test_web_api.py. The hard
contracts under test:

  * the page is addressed by, and only ever renders, the SAFE fingerprint
    stand-in -- never the raw, serial-bearing device_id;
  * its activity section is SwitchAgent's own record of what it attempted,
    never a claim about what the console holds ("Installed games" is a
    phrase that must not appear);
  * the actions on it (rename, storage mapping, device-scoped diagnostics
    export) go through fingerprint-addressed routes and really persist --
    the raw device_id is resolved server-side, never in the request URL
    (see api_rename_device_by_fingerprint's own docstring: an earlier
    version of this page called the raw-id routes directly, which leaked
    the raw id into uvicorn's own access log);
  * W3-006's conflict card now links at the specific device, not the
    generic /devices list it fell back to before this page existed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders, queue_worker
from switchagent.mtp.windows import device_fingerprint
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

PARENT = "mock-switch-parent"
CHILD = "mock-switch-child"
PARENT_FP = device_fingerprint(PARENT)
CHILD_FP = device_fingerprint(CHILD)

BASE_TITLE_ID = "0100000000010000"


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
    yield ctx


@pytest.fixture
def client(web_ctx):
    return TestClient(create_app(web_ctx))


def _seed_library_item(web_ctx, name=f"ZzzQuest [{BASE_TITLE_ID}][v0].nsp", title_id=BASE_TITLE_ID) -> int:
    path = config.LIBRARY_DIR / name
    path.write_bytes(b"payload")
    with db.open_db(web_ctx.db_path) as conn:
        return db.upsert_library_item(
            conn, absolute_path=str(path), item_type="FILE", file_type="NSP",
            size=path.stat().st_size, mtime=path.stat().st_mtime, content_hash=name,
            title_id=title_id, title_id_source="filename", status="AVAILABLE",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE", package_format="NSP",
        )


def _record_history(web_ctx, *, device_id, outcome, title_id=BASE_TITLE_ID,
                    display_name="ZzzQuest", user_verified=None, storage="SD_INSTALL", error=None):
    item_id = _seed_library_item(
        web_ctx, name=f"{display_name} [{title_id}][v0].nsp", title_id=title_id,
    )
    with db.open_db(web_ctx.db_path) as conn:
        batch_id = db.create_installation_batch(conn, target_device_id=device_id)
        job_id = db.create_job(
            conn, action="INSTALL_VIA_DBI", target_storage=storage, target_device_id=device_id,
            library_item_id=item_id, batch_id=batch_id,
        )
        db.update_job_status(conn, job_id, outcome, error=error)
        history_id = db.record_install_history(
            conn, job_id=job_id, title_id=title_id, display_name=display_name,
            target_device_id=device_id, target_storage=storage, outcome=outcome,
            error=error, bytes_total=1000,
        )
        if user_verified is not None:
            db.set_user_verified_outcome(conn, history_id, user_verified)
    return history_id


# ---------------------------------------------------------------------------
# rendering: connected and disconnected
# ---------------------------------------------------------------------------

def test_connected_device_renders_every_field(client, web_ctx):
    client.post(f"/api/devices/{PARENT}/rename", json={"friendly_name": "Моя Switch"})

    res = client.get(f"/devices/{PARENT_FP}")
    assert res.status_code == 200
    html = res.text

    assert "Моя Switch" in html
    assert PARENT_FP in html
    assert "Connected" in html
    # first/last seen come straight from the devices table
    with db.open_db(web_ctx.db_path) as conn:
        row = db.get_device(conn, PARENT)
    assert row["first_seen_at"] in html
    assert row["last_seen_at"] in html

    # storage snapshot: DBI's own raw name (StorageInfo.raw_name -- safe,
    # non-serial-bearing text), the logical name it resolves to, the
    # AUTO/MANUAL mapping source, and a free/total space column.
    # MockMtpBackend reports no total_bytes, so the space cell honestly
    # renders "—" here rather than an invented number -- the column itself
    # is what this asserts.
    assert 'id="device-storages"' in html
    assert "<th>Storage (as reported)</th>" in html
    assert "<th>Space</th>" in html
    assert "SD_CARD" in html and "SD_INSTALL" in html
    assert "AUTO" in html

    # diagnostics summary for this device
    assert 'id="device-diagnostics"' in html
    assert "Export diagnostics for this device" in html

    # rename + storage-mapping actions
    assert 'id="device-rename-form"' in html
    assert "storage-mapping-save" in html
    # explicitly out of scope -- must not be offered
    assert "remote delete" not in html.lower()
    assert "forget this device" not in html.lower()


def test_disconnected_device_renders_and_says_so(client, web_ctx):
    """A device seen before but not currently reachable must still have a
    page -- marked disconnected, never 404, never silently dropped."""
    real_looking_id = r"::{GUID}\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    with db.open_db(web_ctx.db_path) as conn:
        db.upsert_device_seen(conn, real_looking_id, "Switch")
    fingerprint = device_fingerprint(real_looking_id)

    res = client.get(f"/devices/{fingerprint}")
    assert res.status_code == 200
    html = res.text
    assert "Disconnected" in html
    assert fingerprint in html
    # never seen connected -> no storage snapshot, said honestly
    assert "has not been seen connected" in html


def test_unknown_fingerprint_is_a_404(client):
    assert client.get("/devices/deadbeefdeadbeef").status_code == 404
    assert client.get("/api/devices/by-fingerprint/deadbeefdeadbeef").status_code == 404


def test_device_detail_page_never_renders_raw_device_id(client, web_ctx):
    """Mirrors test_web_api.py's
    test_queue_and_history_pages_never_render_raw_device_id: device_id
    embeds the console's real USB serial on real hardware (see
    mtp/windows.py's mask_device_id), so it must appear NOWHERE in this
    page's HTML -- not as text, not as an attribute value, not in a URL.
    Only the fingerprint and the friendly/display name may."""
    real_looking_id = r"::{GUID}\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    with db.open_db(web_ctx.db_path) as conn:
        db.upsert_device_seen(conn, real_looking_id, "Switch")
        db.set_device_friendly_name(conn, real_looking_id, "Моя Switch")
    fingerprint = device_fingerprint(real_looking_id)
    _record_history(web_ctx, device_id=real_looking_id, outcome="DONE_UNVERIFIED")

    html = client.get(f"/devices/{fingerprint}").text
    assert "xtj10229424075" not in html
    assert real_looking_id not in html
    assert fingerprint in html
    assert "Моя Switch" in html

    # ...and the same for a mock device whose id is its own plain string.
    _record_history(web_ctx, device_id=PARENT, outcome="DONE")
    parent_html = client.get(f"/devices/{PARENT_FP}").text
    assert PARENT not in parent_html
    assert PARENT_FP in parent_html


# ---------------------------------------------------------------------------
# activity section
# ---------------------------------------------------------------------------

def test_activity_section_is_never_called_installed_games(client, web_ctx):
    """SwitchAgent can only prove what it attempted and transferred, never
    what the console currently holds -- so neither the section heading nor
    any entry in it may be labelled as a list of installed games."""
    _record_history(web_ctx, device_id=PARENT, outcome="DONE_UNVERIFIED")
    html = client.get(f"/devices/{PARENT_FP}").text

    assert "<h3>SwitchAgent activity</h3>" in html
    assert "Installed games" not in html
    assert "installed games" not in html.lower()


def test_activity_section_shows_every_outcome_and_both_axes(client, web_ctx):
    _record_history(web_ctx, device_id=PARENT, outcome="DONE",
                    title_id=BASE_TITLE_ID, display_name="VerifiedGame", storage="SD_CARD")
    _record_history(web_ctx, device_id=PARENT, outcome="DONE_UNVERIFIED",
                    title_id="0100000000020000", display_name="UnconfirmedGame")
    _record_history(web_ctx, device_id=PARENT, outcome="DONE_UNVERIFIED", user_verified="SUCCESS",
                    title_id="0100000000030000", display_name="ConfirmedOkGame")
    _record_history(web_ctx, device_id=PARENT, outcome="DONE_UNVERIFIED", user_verified="FAILED",
                    title_id="0100000000040000", display_name="ConfirmedBadGame")
    _record_history(web_ctx, device_id=PARENT, outcome="FAILED",
                    title_id="0100000000050000", display_name="FailedGame", error="MTP write refused")
    _record_history(web_ctx, device_id=PARENT, outcome="INTERRUPTED",
                    title_id="0100000000060000", display_name="InterruptedGame")
    _record_history(web_ctx, device_id=PARENT, outcome="DESTINATION_CONFLICT",
                    title_id="0100000000070000", display_name="ConflictedGame")
    # ...and one on the OTHER device, which must not appear here.
    _record_history(web_ctx, device_id=CHILD, outcome="DONE",
                    title_id="0100000000080000", display_name="OtherDeviceGame")

    html = client.get(f"/devices/{PARENT_FP}").text

    assert "Attempts: <strong>7</strong>" in html
    assert "Transfer verified: <strong>1</strong>" in html
    assert "Transfer unconfirmed: <strong>1</strong>" in html
    assert "Confirmed by you — worked: <strong>1</strong>" in html
    assert "Confirmed by you — failed: <strong>1</strong>" in html
    assert "Failed: <strong>1</strong>" in html
    assert "Interrupted: <strong>1</strong>" in html
    assert "Destination conflicts: <strong>1</strong>" in html

    for name in ("VerifiedGame", "UnconfirmedGame", "ConfirmedOkGame", "ConfirmedBadGame",
                 "FailedGame", "InterruptedGame", "ConflictedGame"):
        assert name in html, name
    assert "OtherDeviceGame" not in html  # strictly this device's own record

    # batches and game families are both represented
    assert 'id="device-activity-batches"' in html
    assert 'id="device-activity-families"' in html
    assert f'href="/games/{BASE_TITLE_ID}"' in html

    # both axes stated per row, never merged
    assert "Transport outcome: <strong>DONE</strong>" in html
    assert "Transport outcome: <strong>DONE UNVERIFIED</strong>" in html
    assert "confirmed successful by you" in html
    assert "confirmed failed by you" in html
    assert "not confirmed" in html
    assert "MTP write refused" in html


def test_device_with_no_activity_says_so(client, web_ctx):
    html = client.get(f"/devices/{PARENT_FP}").text
    assert "SwitchAgent has never attempted a transfer to this Switch." in html
    assert "installed games" not in html.lower()


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def test_rename_from_this_page_uses_the_existing_endpoint_and_persists(client, web_ctx):
    """device_detail.js calls the fingerprint-addressed rename route
    directly (the raw device_id is resolved server-side, inside the
    request handler -- it never crosses into the browser/JS runtime or a
    logged URL at all; see api_rename_device_by_fingerprint's own
    docstring for the privacy finding that motivated this)."""
    res = client.post(f"/api/devices/by-fingerprint/{PARENT_FP}/rename", json={"friendly_name": "Гостиная"})
    assert res.status_code == 200

    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_device(conn, PARENT)["friendly_name"] == "Гостиная"
    assert "Гостиная" in client.get(f"/devices/{PARENT_FP}").text


def test_rename_by_fingerprint_404s_for_an_unknown_fingerprint(client):
    res = client.post("/api/devices/by-fingerprint/deadbeefdeadbeef/rename", json={"friendly_name": "x"})
    assert res.status_code == 404


def test_storage_mapping_change_from_this_page_persists(client, web_ctx):
    """Same DB-level assertion style as test_web_api.py's existing
    storage-mapping round-trip: the mapping row really lands in
    device_storage_mappings, and the page then reports it as MANUAL."""
    web_ctx.storage_refresh_interval_seconds = 0
    parent = web_ctx.registry.get(PARENT)
    parent.add_storage("SD_INSTALL_2", raw_name="9: Weird Vendor Name")
    with db.open_db(web_ctx.db_path) as conn:
        web_ctx.refresh_devices(conn)

    res = client.post(
        f"/api/devices/by-fingerprint/{PARENT_FP}/storages/mapping",
        json={"raw_storage_name": "9: Weird Vendor Name", "logical_name": "SD_INSTALL"},
    )
    assert res.status_code == 200

    with db.open_db(web_ctx.db_path) as conn:
        row = db.get_device_storage_mapping(conn, PARENT, "9: Weird Vendor Name")
        assert row is not None
        assert row["logical_name"] == "SD_INSTALL"
        web_ctx.refresh_devices(conn)

    html = client.get(f"/devices/{PARENT_FP}").text
    assert "9: Weird Vendor Name" in html
    assert "MANUAL" in html

    clear = client.post(
        f"/api/devices/by-fingerprint/{PARENT_FP}/storages/mapping/clear",
        json={"raw_storage_name": "9: Weird Vendor Name"},
    )
    assert clear.status_code == 200
    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_device_storage_mapping(conn, PARENT, "9: Weird Vendor Name") is None


def test_storage_mapping_by_fingerprint_404s_for_an_unknown_fingerprint(client):
    res = client.post(
        "/api/devices/by-fingerprint/deadbeefdeadbeef/storages/mapping",
        json={"raw_storage_name": "SD Card", "logical_name": "SD_CARD"},
    )
    assert res.status_code == 404


def test_device_detail_js_never_issues_a_raw_device_id_shaped_request(tmp_path):
    """The actual privacy property (independent verification, 2026-09-12):
    device_detail.js's rename/mapping actions must build their request URL
    from the fingerprint alone, never from a resolved raw device_id --
    that resolution now happens server-side, inside the fingerprint-
    addressed route handlers, specifically so the raw id never crosses
    into the browser, gets held in a JS variable, or ends up in a request
    URL that uvicorn's own access log would then record. Static-checked
    against the actual file, same technique as
    test_report_issue_link_is_never_wired_to_a_network_call_in_js."""
    js_path = Path(__file__).resolve().parents[1] / "switchagent" / "web" / "static" / "device_detail.js"
    text = js_path.read_text(encoding="utf-8")
    assert "resolveDeviceId" not in text
    assert ".device_id" not in text  # no code path ever reads a raw id out of a JSON response
    assert "${deviceId}" not in text  # no code path ever builds a URL from a resolved raw id
    assert "/api/devices/by-fingerprint/${encodeURIComponent(fingerprint)}/rename" in text
    assert "/api/devices/by-fingerprint/${encodeURIComponent(fingerprint)}/storages/mapping" in text


def test_device_scoped_diagnostics_export_is_scoped_and_leaks_no_raw_device_id(client, web_ctx):
    """Reuses the existing diagnostics engine, narrowed to one device: only
    this device's fingerprint is listed, only its own job errors are
    included, and the raw device_id appears nowhere in the payload (the
    exact FIX-001 class of leak, re-proven for this new export)."""
    _record_history(web_ctx, device_id=PARENT, outcome="FAILED",
                    display_name="ParentFail", error="parent device error")
    _record_history(web_ctx, device_id=CHILD, outcome="FAILED",
                    title_id="0100000000020000", display_name="ChildFail", error="child device error")

    res = client.get(f"/api/devices/by-fingerprint/{PARENT_FP}/diagnostics/export")
    assert res.status_code == 200
    payload = res.json()

    assert payload["scoped_to_device_fingerprint"] == PARENT_FP
    assert payload["device_fingerprints"] == [PARENT_FP]
    assert payload["device"]["device_fingerprint"] == PARENT_FP
    errors = " ".join(e["error"] or "" for e in payload["recent_job_errors"])
    assert "parent device error" in errors
    assert "child device error" not in errors

    body = res.text
    assert PARENT not in body
    assert CHILD not in body

    text = client.get(f"/api/devices/by-fingerprint/{PARENT_FP}/diagnostics/export?format=txt")
    assert text.status_code == 200
    assert PARENT_FP in text.text
    assert PARENT not in text.text
    assert "SwitchAgent activity" in text.text


# ---------------------------------------------------------------------------
# W3-006 integration: the Destination Conflict card's "Device Details" link
# ---------------------------------------------------------------------------

def _seed_real_destination_conflict(web_ctx, client) -> int:
    """Drives a REAL DESTINATION_CONFLICT through the worker (same approach
    as test_web_api.py's own conflict fixture): the file is already present
    at the destination, put there by something that is not this job."""
    item_id = _seed_library_item(web_ctx)
    backend = web_ctx.registry.get(PARENT)
    backend.connect()
    backend.storage_tree("SD_INSTALL").write_file(
        f"ZzzQuest [{BASE_TITLE_ID}][v0].nsp", b"someone else's file",
    )

    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": PARENT})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)
        assert db.get_job(conn, job_id)["status"] == "DESTINATION_CONFLICT"
    return job_id


def test_conflict_card_links_to_the_specific_device_detail_page(client, web_ctx):
    """W3-006 shipped with the conflict card's "Device Details" affordance
    pointing at the generic /devices list, explicitly as a temporary
    fallback until a per-device page existed. Now that W3-004 added one, it
    must point at THIS job's target device -- by fingerprint, never the raw
    device_id."""
    job_id = _seed_real_destination_conflict(web_ctx, client)

    html = client.get("/queue").text
    conflict_card = html.split("conflict-actions", 1)[1].split("</div>", 1)[0]
    assert f'href="/devices/{PARENT_FP}"' in conflict_card
    assert 'href="/devices"' not in conflict_card  # the old generic fallback is gone
    assert PARENT not in html

    # the link really resolves to a rendered page for that device
    assert client.get(f"/devices/{PARENT_FP}").status_code == 200

    # ...and the same fingerprint reaches queue.js through the JSON API, so
    # the live-refreshed card builds the identical link.
    job = next(j for j in client.get("/api/queue").json() if j["id"] == job_id)
    assert job["target_device_fingerprint"] == PARENT_FP

    queue_js = (config.PROJECT_ROOT / "switchagent" / "web" / "static" / "queue.js").read_text(encoding="utf-8")
    assert "/devices/${j.target_device_fingerprint}" in queue_js


def test_device_page_surfaces_the_open_destination_conflict(client, web_ctx):
    """A job currently sitting in DESTINATION_CONFLICT is a live, unresolved
    condition -- shown on this page distinctly from past attempts, and
    (W3-006's own hard rule) never with an overwrite/force affordance. The
    worker's own error text legitimately contains the word "overwrite"
    ("...refusing to overwrite"), so this asserts the absence of an ACTION,
    not of the word."""
    _seed_real_destination_conflict(web_ctx, client)
    html = client.get(f"/devices/{PARENT_FP}").text
    assert 'id="device-open-conflicts"' in html
    assert f"ZzzQuest [{BASE_TITLE_ID}][v0].nsp" in html
    assert "refusing to overwrite" in html  # the honest reason, shown as-is
    for forbidden in ("overwrite anyway", "force overwrite", "assume same file",
                      "remote delete", "delete on device"):
        assert forbidden not in html.lower(), forbidden
