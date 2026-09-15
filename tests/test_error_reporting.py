"""Tests for W3-009 (public error-reporting UX): switchagent/web/
error_reporting.py's pure text/URL builders, plus FastAPI integration
tests proving an unhandled exception renders the generic "Something went
wrong" page (never a raw traceback), the issue summary/Report-issue link
meet every spec requirement, and a raw device_id never leaks anywhere in
the process -- same technique test_diagnostics.py's own raw-device_id-leak
tests already use.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders
from switchagent.mtp.windows import device_fingerprint
from switchagent.web import error_reporting
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def test_build_issue_summary_text_contains_all_required_fields_in_order():
    data = error_reporting.IssueSummaryInput(
        version="0.1.0", windows="Windows-10-10.0.19045-SP0", runtime_mode="dev",
        page_action="GET /queue", job_id="42", device_fingerprint="abc123def456",
        job_status="FAILED", description="it broke",
    )
    text = error_reporting.build_issue_summary_text(data)
    labels = (
        "SwitchAgent version:", "Windows:", "Runtime mode:", "Page/action:",
        "Job ID:", "Device fingerprint:", "Job status:", "Description:",
    )
    for label in labels:
        assert label in text
    # exact required order
    positions = [text.index(label) for label in labels]
    assert positions == sorted(positions)
    assert "0.1.0" in text
    assert "abc123def456" in text
    assert "it broke" in text


def test_build_issue_summary_text_defaults_are_placeholders_not_blank_or_none():
    text = error_reporting.build_issue_summary_text(error_reporting.IssueSummaryInput())
    assert "None" not in text
    assert "Job ID: —" in text


def test_build_report_issue_url_targets_github_new_issue_with_encoded_body():
    text = "SwitchAgent version: 0.1.0\nDescription: hello world & special=chars"
    url = error_reporting.build_report_issue_url(text)
    assert url.startswith(error_reporting.GITHUB_NEW_ISSUE_URL + "?body=")
    # the raw text (with its literal space/&/=/newline) must not appear
    # unescaped in the URL -- it must be percent/plus encoded instead.
    assert "SwitchAgent version: 0.1.0" not in url
    assert "hello+world" in url or "hello%20world" in url


def test_windows_version_string_never_raises_and_returns_something_non_empty():
    version = error_reporting.windows_version_string()
    assert isinstance(version, str)
    assert version


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------


@pytest.fixture
def web_ctx(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    work_dir = tmp_path / "work"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", work_dir)
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


def _app_with_boom_route(ctx, *, message: str = "synthetic failure for W3-009 tests"):
    """Adds a route that deliberately raises, for this test module only --
    production switchagent/web/app.py never has anything like this. Exists
    purely to exercise the global exception handler under test without
    depending on any real route actually having a bug right now."""
    app = create_app(ctx)

    @app.get("/__test_unhandled_error")
    def _boom():
        raise RuntimeError(message)

    return app


@pytest.fixture
def client(web_ctx):
    app = _app_with_boom_route(web_ctx)
    # Starlette's ServerErrorMiddleware always re-raises the original
    # exception after our handler runs (so the ASGI server's own exception
    # logging still fires in production -- see app.py's own comments);
    # raise_server_exceptions=False makes the TestClient return our
    # handler's response instead of re-raising that exception into the
    # test itself.
    return TestClient(app, raise_server_exceptions=False)


def test_unexpected_error_renders_generic_page_not_a_raw_traceback(client):
    res = client.get("/__test_unhandled_error")
    assert res.status_code == 500
    assert "Something went wrong" in res.text
    assert "Traceback (most recent call last)" not in res.text
    assert re.search(r'File "[^"]+", line \d+', res.text) is None
    assert "app.py" not in res.text
    assert ".py" not in res.text


def test_unexpected_error_page_has_required_actions(client):
    res = client.get("/__test_unhandled_error")
    assert 'id="error-export-diagnostics-link"' in res.text
    assert 'href="/api/diagnostics/export?format=txt"' in res.text
    assert 'id="error-copy-summary-btn"' in res.text
    assert 'id="error-issue-summary"' in res.text
    assert 'id="error-report-issue-link"' in res.text


def test_unexpected_error_issue_summary_has_all_required_fields(client):
    res = client.get("/__test_unhandled_error")
    for label in (
        "SwitchAgent version:", "Windows:", "Runtime mode:", "Page/action:",
        "Job ID:", "Device fingerprint:", "Job status:", "Description:",
    ):
        assert label in res.text
    assert "GET /__test_unhandled_error" in res.text  # Page/action populated from the real request


def test_report_issue_link_targets_github_with_encoded_body_and_is_a_plain_link(client):
    res = client.get("/__test_unhandled_error")
    match = re.search(r'id="error-report-issue-link" href="([^"]+)"[^>]*target="_blank"', res.text)
    assert match is not None, res.text
    href = match.group(1).replace("&amp;", "&")
    assert href.startswith(error_reporting.GITHUB_NEW_ISSUE_URL + "?body=")


def test_report_issue_link_is_never_wired_to_a_network_call_in_js():
    """The only "sending" that ever happens is the user's own click through
    to GitHub's own page, and then GitHub's own Submit button -- error.js
    must never attach any handler (window.open, fetch, XHR) to this link
    itself; a plain <a target="_blank"> is the entire mechanism."""
    js_path = Path(__file__).resolve().parents[1] / "switchagent" / "web" / "static" / "error.js"
    text = js_path.read_text(encoding="utf-8")
    assert "error-report-issue-link" not in text
    assert "window.open" not in text
    assert "fetch(" not in text
    assert "XMLHttpRequest" not in text
    # The realistic exfiltration primitives an auto-send would actually
    # reach for, none of which are named-arg substrings of the checks
    # above -- each closes a distinct way this file could quietly phone
    # home without ever calling fetch()/XHR/window.open.
    assert "sendBeacon" not in text
    assert "new Image" not in text
    assert "WebSocket" not in text
    assert ".submit(" not in text


def test_unexpected_error_never_posts_anything_itself(client):
    """Regression guard: the whole page must be reachable and fully
    rendered from a single GET -- nothing about rendering this page (or
    its JS) triggers any POST anywhere. The rendered HTML itself is
    checked here (no form, no meta-refresh, no inline <script> body that
    could run something on load); error.js's own file content -- window.
    open/fetch/XHR/sendBeacon/Image/WebSocket/.submit() -- is separately
    and more thoroughly checked by
    test_report_issue_link_is_never_wired_to_a_network_call_in_js."""
    res = client.get("/__test_unhandled_error")
    assert res.status_code == 500
    # No auto-submitted form / meta-refresh / redirect anywhere on this page.
    assert "<form" not in res.text
    assert "http-equiv" not in res.text.lower()
    # Every <script> on this page is an external src= reference (checked
    # elsewhere in isolation) -- none has an inline body that could run
    # anything the instant the page loads.
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", res.text) is None


def test_show_technical_details_disclosure_is_sanitized_never_raw(tmp_path, monkeypatch):
    """Reuses the same sanitize_free_text() pattern already established for
    diagnostics exports (see test_diagnostics.py's
    test_build_export_dict_sanitizes_a_job_errors_embedded_home_path_and_raw_device_id)."""
    inbox_dir = tmp_path / "inbox"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    home = tmp_path / "Users" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))

    db_path = tmp_path / "test.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path)

    raw_serial = "SERIALNUMBER1234"
    raw_id = f"usb#vid_057e&pid_3000#{raw_serial}#{{fingerprint-guid}}"
    message = f"failed touching {home}\\Downloads and device {raw_id}"
    app = _app_with_boom_route(ctx, message=message)
    client = TestClient(app, raise_server_exceptions=False)

    res = client.get("/__test_unhandled_error")
    assert res.status_code == 500
    assert "Show technical details" in res.text
    assert raw_serial not in res.text
    assert str(home).lower() not in res.text.lower()
    assert "%USERPROFILE%" in res.text
    assert "Traceback (most recent call last)" not in res.text


def test_unexpected_error_issue_summary_never_contains_a_raw_device_id_when_no_device_connected(tmp_path, monkeypatch):
    """Exercises switchagent/web/app.py's db.list_devices() fallback path
    (no currently-connected device, but one previously seen) -- a fresh
    context with nothing registered/connected, and a synthetic
    serial-shaped device_id upserted directly into the DB. Carries a
    positive control (the safe fingerprint IS present in the rendered
    "Device fingerprint:" line) so the negative assertions below can't
    pass merely because _pick_known_device_fingerprint() silently
    returned the empty "-" placeholder instead of actually looking the
    device up -- the one real end-to-end proof this module's fingerprint-
    only guarantee needs (a bare unit test of build_issue_summary_text()
    can't prove it, since that function is never given a raw id by any
    real caller in the first place)."""
    inbox_dir = tmp_path / "inbox"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)

    real_looking_id = r"::{GUID}\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    db_path = tmp_path / "test.db"
    with db.open_db(db_path) as conn:
        db.upsert_device_seen(conn, real_looking_id, "Switch")

    ctx = build_mock_context(db_path)  # refresh_devices() never called -- nothing "connected"
    app = _app_with_boom_route(ctx)
    client = TestClient(app, raise_server_exceptions=False)

    res = client.get("/__test_unhandled_error")
    assert res.status_code == 500
    assert "xtj10229424075" not in res.text
    assert real_looking_id not in res.text
    assert f"Device fingerprint: {device_fingerprint(real_looking_id)}" in res.text


def test_ordinary_http_exceptions_are_unaffected_by_the_global_handler(client):
    """Regression guard: the new Exception handler must never intercept
    FastAPI's own HTTPException-based 4xx responses -- those keep their
    existing JSON shape exactly as before."""
    res = client.get("/api/jobs/999999")
    assert res.status_code == 404
    assert res.json()["detail"] == "job not found"


def test_unexpected_error_summary_picks_up_job_id_and_status_from_the_request_path(web_ctx):
    """switchagent/web/app.py's _build_error_page_context() opportunistically
    extracts a job id from a `/jobs/<id>` -shaped path and looks up its
    current status -- proves that wiring actually works, using a real job
    row rather than a placeholder. A bare db.create_job() row (no preview/
    manifest attached) is enough here -- this only needs a real id/status
    pair to look up, never runs the job through the worker."""
    with db.open_db(web_ctx.db_path) as conn:
        db.upsert_inbox_item(
            conn, relative_path="Game [0100000000010000][v0].nsp", item_type="FILE", file_type="NSP",
            size=7, mtime=0.0, content_hash="hash-1", title_id="0100000000010000",
            title_id_source="filename", status="ANALYZED",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        )
        item_id = db.get_inbox_item(conn, "Game [0100000000010000][v0].nsp")["id"]
        job_id = db.create_job(
            conn, inbox_item_id=item_id, action="INSTALL_VIA_DBI",
            target_storage="SD_INSTALL", target_device_id="mock-switch-parent",
        )
        expected_status = db.get_job(conn, job_id)["status"]

    app = create_app(web_ctx)

    @app.get("/jobs/{job_id}/__boom")
    def _boom(job_id: int):
        raise RuntimeError("synthetic failure for job-context test")

    client = TestClient(app, raise_server_exceptions=False)
    res = client.get(f"/jobs/{job_id}/__boom")
    assert res.status_code == 500
    assert f"Job ID: {job_id}" in res.text
    assert f"Job status: {expected_status}" in res.text
