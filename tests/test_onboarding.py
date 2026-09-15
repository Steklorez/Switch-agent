"""Tests for W3-007 onboarding: switchagent/web/onboarding.py's pure
decision logic, plus FastAPI integration tests proving the resulting
banners render on the Library/Devices pages without ever blocking
navigation to Settings/History/Devices/Diagnostics (Web UI spec: "not a
mandatory wizard").
"""

from __future__ import annotations

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders
from switchagent.web import onboarding
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

# ---------------------------------------------------------------------------
# Pure logic -- switchagent/web/onboarding.py never touches config/db/
# WebContext itself, so every case here is testable with plain values, no
# fixtures needed.
# ---------------------------------------------------------------------------


def test_library_not_configured_shows_choose_library_folder_message():
    msg = onboarding.compute_library_message(
        library_dir_configured=False, library_dir_exists=False, library_item_count=0,
    )
    assert msg is not None
    assert msg.id == "library_not_configured"
    assert msg.heading == "Choose Library Folder"
    assert any(a.href == "/settings#library-folder" for a in msg.actions)


def test_library_configured_but_missing_directory_is_also_choose_library_folder():
    """"Not configured OR invalid" (W3-007 spec) -- config.yaml can have
    library.source_dir set but pointing at a folder that no longer exists
    (moved drive, deleted folder); that must show the exact same message
    as never having configured one at all, not a third, separate case."""
    msg = onboarding.compute_library_message(
        library_dir_configured=True, library_dir_exists=False, library_item_count=0,
    )
    assert msg is not None
    assert msg.id == "library_not_configured"


def test_library_configured_and_empty_shows_no_supported_content_found():
    msg = onboarding.compute_library_message(
        library_dir_configured=True, library_dir_exists=True, library_item_count=0,
    )
    assert msg is not None
    assert msg.id == "library_empty"
    assert msg.heading == "No supported content found"
    labels = {a.label for a in msg.actions}
    assert "Rescan" in labels
    assert "Settings" in labels


def test_library_configured_and_populated_shows_no_onboarding_message():
    msg = onboarding.compute_library_message(
        library_dir_configured=True, library_dir_exists=True, library_item_count=3,
    )
    assert msg is None


def test_no_device_ever_seen_shows_step_by_step_guidance():
    msg = onboarding.compute_device_message(known_device_count=0, any_device_connected=False)
    assert msg is not None
    assert msg.id == "no_device_ever_seen"
    assert len(msg.steps) == 4
    joined = " ".join(msg.steps)
    assert "USB" in joined
    assert "DBI" in joined
    assert "MTP Responder" in joined


def test_no_device_ever_seen_includes_the_dbi_does_the_install_semantics():
    """W3-007 required semantics: onboarding copy must never imply
    SwitchAgent itself performs or guarantees the on-console install
    result -- DBI does. Required to appear literally for this case."""
    msg = onboarding.compute_device_message(known_device_count=0, any_device_connected=False)
    assert "SwitchAgent transfers content to DBI" in msg.body
    assert "DBI performs installation on the console" in msg.body


def test_device_known_but_disconnected_is_a_distinct_calmer_message():
    msg = onboarding.compute_device_message(known_device_count=1, any_device_connected=False)
    assert msg is not None
    assert msg.id == "device_known_but_disconnected"
    assert msg.id != "no_device_ever_seen"


def test_device_known_but_disconnected_never_repeats_first_time_setup_steps():
    """Spec: "don't scare a returning user with first-time-setup
    instructions when they just unplugged their console for a minute" --
    no numbered steps, no first-time wording."""
    msg = onboarding.compute_device_message(known_device_count=1, any_device_connected=False)
    assert msg.steps == ()
    assert "hasn't detected a Nintendo Switch yet" not in msg.body
    assert "seen this Switch before" in msg.body


def test_device_currently_connected_shows_no_onboarding_message():
    msg = onboarding.compute_device_message(known_device_count=1, any_device_connected=True)
    assert msg is None


def test_device_message_ids_are_all_distinct():
    ids = {
        onboarding.compute_device_message(known_device_count=0, any_device_connected=False).id,
        onboarding.compute_device_message(known_device_count=1, any_device_connected=False).id,
    }
    assert len(ids) == 2


# ---------------------------------------------------------------------------
# FastAPI integration -- onboarding never blocks navigation; each case
# renders its own distinct banner on the page it belongs to.
# ---------------------------------------------------------------------------


@pytest.fixture
def web_ctx(tmp_path, monkeypatch):
    """A brand new context: library folder never configured, no device
    ever seen (nothing registered, refresh_devices() never called) -- both
    "worst case" onboarding states active at once, on purpose, so the
    "never blocks navigation" test below exercises the strictest case."""
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
        pass  # just run schema/migrations

    # Deliberately never call ctx.refresh_devices() here -- that is the
    # ONLY thing that ever populates either the `devices` DB table or
    # WebContext's own live cache (see services.list_devices()'s own
    # docstring), so this context starts in a genuine "no Switch ever
    # seen" state regardless of which backends happen to be pre-registered
    # in the mock registry.
    ctx = build_mock_context(db_path)
    yield ctx


@pytest.fixture
def client(web_ctx):
    app = create_app(web_ctx)
    return TestClient(app)


def test_onboarding_never_blocks_settings_history_devices_and_they_render_normal_content(client):
    """Web UI spec: onboarding is "not a mandatory wizard" -- never blocks
    or gates access to Settings/History/Devices/Diagnostics. Both onboarding
    cases (library unconfigured, no device ever seen) are active in this
    fixture at once; every one of these pages must still return 200 and
    render its own normal content regardless."""
    res = client.get("/settings")
    assert res.status_code == 200
    assert "Settings" in res.text
    assert "Diagnostics" in res.text  # Diagnostics is a section of /settings, see settings.html

    res = client.get("/history")
    assert res.status_code == 200
    assert "History" in res.text

    res = client.get("/devices")
    assert res.status_code == 200
    assert "Devices" in res.text


def test_onboarding_never_blocks_the_library_page_itself(client):
    res = client.get("/")
    assert res.status_code == 200
    # The page's own normal toolbar/filter controls are still there.
    assert 'id="rescan-btn"' in res.text


def test_library_page_shows_choose_library_folder_banner_when_unconfigured(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Choose Library Folder" in res.text
    assert 'id="onboarding-library_not_configured"' in res.text


def test_library_page_shows_no_supported_content_found_when_configured_but_empty(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    config_yaml_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", config_yaml_path)
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    config.set_library_source_dir(library_dir, config_yaml_path)  # now configured AND exists, but empty

    db_path = tmp_path / "test.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path)  # refresh_devices() never called -- device state irrelevant here
    client = TestClient(create_app(ctx))

    res = client.get("/")
    assert res.status_code == 200
    assert "No supported content found" in res.text
    assert "Choose Library Folder" not in res.text  # the other library case must not also show


def _wait_for_scan_idle(ctx, timeout_seconds: float = 5.0) -> None:
    """Every route-level test above that reaches the library banner does so
    with zero library_items, so none of them ever has to worry about
    scanner.scan_library_once()'s own missing-item reconciliation
    (db.delete_library_items_missing_from(), keyed off exactly what ITS
    pass over disk saw). The two tests below insert a library_items row
    directly (not through the scanner) right after configuring the
    library dir through the real POST /api/settings/library-dir route --
    and that route itself triggers its own automatic background rescan
    (services.set_library_dir -> ctx.run_scan_in_background()) of the
    directory. Waiting for that scan to finish first (it has nothing to
    do on the empty/about-to-be-populated directory, so this is fast)
    avoids a real race where the background pass could reconcile away the
    row this test is about to add out from under it."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with ctx.scan_lock:
            if not ctx.scan_state.running:
                return
        time.sleep(0.1)


def test_library_page_shows_populated_content_not_onboarding_banner_when_items_exist(tmp_path, monkeypatch):
    """Coverage gap #1 from the test-quality audit: every library-onboarding
    route test in this file reaches the banner with ZERO library_items
    rows, so none of them exercises the real wiring in app.py's
    _library_onboarding_message() --
        item_count = len(db.list_library_items(conn))
    -- against an actually populated library. A regression that hardcoded
    library_item_count=0 into that call, or that counted
    db.list_inbox_items() instead of db.list_library_items(), would still
    pass every existing test in this file; only a real row, inserted via
    db.upsert_library_item() and read back through the real GET / route,
    can catch it. The library dir itself is configured through the real
    production POST /api/settings/library-dir endpoint (same as
    test_set_library_dir_persists_and_updates_settings in
    tests/test_web_api.py), not by hand-constructing WebContext state."""
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
    client = TestClient(create_app(ctx))

    res = client.post("/api/settings/library-dir", json={"path": str(library_dir)})
    assert res.status_code == 200
    _wait_for_scan_idle(ctx)

    item_path = library_dir / "ZzzQuest [0100000000010000][v0].nsp"
    item_path.write_bytes(b"payload")
    with db.open_db(db_path) as conn:
        db.upsert_library_item(
            conn, absolute_path=str(item_path), item_type="FILE", file_type="NSP",
            size=item_path.stat().st_size, mtime=item_path.stat().st_mtime,
            content_hash="hash-zzzquest", title_id="0100000000010000",
            title_id_source="filename", status="AVAILABLE",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE", package_format="NSP",
        )

    res = client.get("/")
    assert res.status_code == 200
    assert "Choose Library Folder" not in res.text
    assert "No supported content found" not in res.text
    assert 'id="rescan-btn"' in res.text  # the page's own normal content is still there


def test_library_page_shows_choose_library_folder_when_configured_dir_no_longer_exists(tmp_path, monkeypatch):
    """Coverage gap #2 from the test-quality audit: the only route-level
    test in this file that reaches the "library_not_configured" onboarding
    state does so via library_dir_configured=False (never configured at
    all) -- none of them prove app.py's _library_onboarding_message() also
    honors library_info.exists for a directory that WAS configured (via
    the real POST /api/settings/library-dir route, exactly like the test
    above) and then vanished, e.g. an external/USB drive unplugged. A
    regression that dropped the `library_dir_exists=library_info.exists`
    argument (always treating a configured-but-missing dir as fine) would
    still pass every existing test in this file."""
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
    client = TestClient(create_app(ctx))

    res = client.post("/api/settings/library-dir", json={"path": str(library_dir)})
    assert res.status_code == 200
    _wait_for_scan_idle(ctx)

    shutil.rmtree(library_dir)  # e.g. an external drive was unplugged

    res = client.get("/")
    assert res.status_code == 200
    assert "Choose Library Folder" in res.text


def test_devices_page_shows_no_device_ever_seen_banner_with_dbi_semantics(client):
    res = client.get("/devices")
    assert res.status_code == 200
    assert "Connect your Switch" in res.text
    assert "Open DBI" in res.text
    assert "SwitchAgent transfers content to DBI. DBI performs installation on the console." in res.text


def test_devices_page_shows_reconnect_copy_not_first_time_setup_when_known_but_disconnected(tmp_path, monkeypatch):
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
    with db.open_db(db_path) as conn:
        db.upsert_device_seen(conn, "some-known-device-id", "Parent's Switch")

    # ctx.refresh_devices() is deliberately never called -- this device was
    # "seen before" (it's in the DB) but is not currently connected (never
    # observed live by this fresh context), exactly like a real Switch that
    # was unplugged before the server was last restarted.
    ctx = build_mock_context(db_path)
    client = TestClient(create_app(ctx))

    res = client.get("/devices")
    assert res.status_code == 200
    assert "Switch not connected" in res.text
    assert "hasn't detected a Nintendo Switch yet" not in res.text
    assert "Open DBI" not in res.text  # no numbered first-time steps repeated
    assert "seen this Switch before" in res.text


def test_devices_page_shows_no_onboarding_banner_when_a_device_is_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)

    db_path = tmp_path / "test.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path)  # default device_ids -- two pre-registered mock backends
    with db.open_db(db_path) as conn:
        ctx.refresh_devices(conn)  # both mock backends now connected
    client = TestClient(create_app(ctx))

    res = client.get("/devices")
    assert res.status_code == 200
    assert "onboarding-banner" not in res.text
