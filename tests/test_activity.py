"""The Library page's activity panel: GET /api/activity and what feeds it.

Before this, only a Rescan click ever showed a scan's progress: the startup
scan and every watcher-triggered one ran silently, and a console's
"Installed games" read -- which every "On Switch" badge waits for, and
which can take a while -- showed nothing at all. The page gave no way to
tell "this is everything" from "still loading".
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from switchagent import config, covers, db, known_folders, scanner
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

from .conftest import build_7z


@pytest.fixture
def web_ctx(tmp_path, monkeypatch):
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    (tmp_path / "inbox").mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    monkeypatch.setattr(scanner, "is_file_stable", lambda *_a, **_k: True)
    db_path = tmp_path / "test.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path)
    with db.open_db(db_path) as conn:
        ctx.refresh_devices(conn)
    return ctx


def test_a_scan_reports_walking_then_indexing_every_item_then_finishing(isolated_db):
    conn, _ = isolated_db
    (config.LIBRARY_DIR / "A [0100000000010000][v0].nsp").write_bytes(b"a")
    (config.LIBRARY_DIR / "B [0100000000020000][v0].nsp").write_bytes(b"b")
    build_7z(config.LIBRARY_DIR / "switch.7z", {"switch/app/app.nro": b"nro"})
    seen = []
    scanner.scan_library_once(conn, on_progress=lambda phase, done, total: seen.append((phase, done, total)))

    phases = [phase for phase, _done, _total in seen]
    assert phases[0] == "walking" and phases[-1] == "finishing"
    assert phases.index("indexing") < phases.index("finishing")
    indexing = [(done, total) for phase, done, total in seen if phase == "indexing"]
    assert {total for _done, total in indexing} == {3}
    assert [done for done, _total in indexing] == [0, 1, 2, 3]  # every item, in order, none skipped


def test_the_activity_endpoint_says_everything_in_one_read(web_ctx):
    client = TestClient(create_app(web_ctx))
    body = client.get("/api/activity").json()
    assert set(body) == {"scan", "covers", "devices"}
    assert {"running", "phase", "done", "total", "current_filename", "finished_at"} <= set(body["scan"])
    assert {"enabled", "running", "ready", "total", "not_found", "failed", "error"} <= set(body["covers"])
    # The mock context's two consoles are connected; neither is being read.
    assert [d["phase"] for d in body["devices"]] == [None, None]
    assert all(d["label"] for d in body["devices"])


def test_a_running_scan_shows_its_progress(web_ctx):
    client = TestClient(create_app(web_ctx))
    with web_ctx.scan_lock:
        web_ctx.scan_state.running = True
        web_ctx.scan_state.started_at = db.now_iso()
        web_ctx.scan_state.phase, web_ctx.scan_state.done, web_ctx.scan_state.total = "indexing", 7, 20
    scan = client.get("/api/activity").json()["scan"]
    assert (scan["phase"], scan["done"], scan["total"]) == ("indexing", 7, 20)


def test_a_console_being_read_is_shown_until_the_read_is_done(web_ctx):
    client = TestClient(create_app(web_ctx))
    device_id = web_ctx.registry.known_device_ids()[0]
    web_ctx._note_device_activity(device_id, "Reading installed games")
    busy = [d for d in client.get("/api/activity").json()["devices"] if d["phase"]]
    assert [d["phase"] for d in busy] == ["Reading installed games"]
    web_ctx._note_device_activity(device_id, None)
    assert not [d for d in client.get("/api/activity").json()["devices"] if d["phase"]]


def _cover_queue(monkeypatch, tmp_path, *, download):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(covers, "download", download)
    queue = covers.CoverQueue()
    return queue


def _wait(queue):
    for _ in range(200):
        if not queue.running:
            return
        time.sleep(.01)


def test_a_game_titledb_has_no_cover_for_is_a_count_not_an_error(tmp_path, monkeypatch):
    def download(url, limit):
        return json.dumps({"1": {"id": "0100000000011000", "name": "Another Game",
                                 "iconUrl": "https://example.invalid/i.jpg"}}).encode()
    queue = _cover_queue(monkeypatch, tmp_path, download=download)
    queue.submit([("01333DE6FB400000", "Zuma Deluxe (port)")])
    _wait(queue)
    state = queue.snapshot()
    assert (state["not_found"], state["failed"]) == (1, 0)
    assert "No cover in TitleDB" in state["error"]  # the old field keeps its meaning


def test_a_cover_that_failed_to_download_is_an_error(tmp_path, monkeypatch):
    def download(url, limit):
        raise OSError("offline")
    queue = _cover_queue(monkeypatch, tmp_path, download=download)
    queue.submit(["0100000000010000"])
    _wait(queue)
    state = queue.snapshot()
    assert state["failed"] == 1 and state["not_found"] == 0


def test_the_library_page_has_the_panel_and_keeps_the_ids_scripts_bind_to(web_ctx):
    html = TestClient(create_app(web_ctx)).get("/").text
    assert 'id="activity"' in html and 'id="activity-topline"' in html
    for element_id in ("scan-cancel-btn", "cover-retry", "cover-status-text", "scan-status-text"):
        assert f'id="{element_id}"' in html
    assert "activity.js" in html
