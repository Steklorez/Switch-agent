import json
import threading
import time
import pytest
from fastapi.testclient import TestClient
from switchagent import config, covers, preferences
from switchagent.web.app import create_app
from .test_web_api import web_ctx


def test_cover_download_runs_off_request_thread_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    entered, release = threading.Event(), threading.Event()
    tid = "01007EF00011E000"
    calls = []
    def download(url, limit):
        calls.append(url)
        if url == covers.TITLEDB:
            entered.set()
            assert release.wait(5)
            return json.dumps({"1": {"id": tid, "iconUrl": "https://img-eshop.cdn.nintendo.net/icon.jpg"}}).encode()
        return b"\xff\xd8\xfftest"
    monkeypatch.setattr(covers, "download", download)
    queue = covers.CoverQueue()
    queue.submit([tid])
    assert entered.wait(2)
    assert queue.running and not covers.image_path(tid).exists()
    release.set()
    for _ in range(100):
        if not queue.running:
            break
        time.sleep(.01)
    assert covers.image_path(tid).read_bytes() == b"\xff\xd8\xfftest"
    queue.submit([tid])
    assert len(calls) == 2
    assert queue.snapshot()["ready"] == [tid]
    assert queue.snapshot()["total"] == 1


def test_cover_falls_back_to_titledb_name_search_when_title_id_is_wrong(tmp_path, monkeypatch):
    """Reproduces the "Rune Factory: Guardians of Azuma" report: a release
    filename's bracketed TITLE_ID doesn't match anything in TitleDB (wrong/
    stale, see title_id.py's from_filename docstring), but the game's own
    name does -- the cover is still found, without the user having to
    correct the filename."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    wrong_tid = "01003AF0200B0000"
    real_tid = "010022A02008C000"

    def download(url, limit):
        if url == covers.TITLEDB:
            return json.dumps({
                "1": {"id": real_tid, "name": "Rune Factory: Guardians of Azuma",
                      "iconUrl": "https://img-eshop.cdn.nintendo.net/icon.jpg"},
            }).encode()
        return b"\xff\xd8\xfftest"
    monkeypatch.setattr(covers, "download", download)
    queue = covers.CoverQueue()
    queue.submit([(wrong_tid, "Rune Factory Guardians of Azuma [01003AF0200B0000][v0]")])
    for _ in range(100):
        if not queue.running:
            break
        time.sleep(.01)
    assert queue.error is None
    assert covers.image_path(wrong_tid).read_bytes() == b"\xff\xd8\xfftest"


def test_cover_name_fallback_tolerates_close_but_inexact_names(tmp_path, monkeypatch):
    """A near-miss (one dropped letter) still resolves via difflib, but
    only because it clears the strict cutoff -- see _NAME_MATCH_CUTOFF."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    tid = "0100000000099000"

    def download(url, limit):
        if url == covers.TITLEDB:
            return json.dumps({
                "1": {"id": "0100000000011000", "name": "Bayonetta 3",
                      "iconUrl": "https://img-eshop.cdn.nintendo.net/icon.jpg"},
            }).encode()
        return b"\xff\xd8\xfftest"
    monkeypatch.setattr(covers, "download", download)
    queue = covers.CoverQueue()
    queue.submit([(tid, "Bayoneta 3 [0100000000099000][v0]")])
    for _ in range(100):
        if not queue.running:
            break
        time.sleep(.01)
    assert queue.error is None
    assert covers.image_path(tid).read_bytes() == b"\xff\xd8\xfftest"


def test_cover_name_fallback_does_not_guess_between_different_games(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    tid = "0100000000099000"

    def download(url, limit):
        if url == covers.TITLEDB:
            return json.dumps({
                "1": {"id": "0100000000011000", "name": "Completely Different Game",
                      "iconUrl": "https://img-eshop.cdn.nintendo.net/icon.jpg"},
            }).encode()
        return b"\xff\xd8\xfftest"
    monkeypatch.setattr(covers, "download", download)
    queue = covers.CoverQueue()
    queue.submit([(tid, "Some Unrelated Title [0100000000099000][v0]")])
    for _ in range(100):
        if not queue.running:
            break
        time.sleep(.01)
    assert not covers.image_path(tid).exists()
    assert "No cover in TitleDB" in queue.error


def test_cover_index_migrates_past_old_flat_format_cache(tmp_path, monkeypatch):
    """A cache file written before the by_id/by_name split existed (a flat
    TITLE_ID -> iconUrl dict) must be treated as stale and rebuilt, never
    crash _load_index."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    tid = "01007EF00011E000"
    covers_dir = tmp_path / "covers"
    covers_dir.mkdir(parents=True)
    (covers_dir / "index.json").write_text(json.dumps({tid: "https://img-eshop.cdn.nintendo.net/old.jpg"}), encoding="utf-8")

    def download(url, limit):
        if url == covers.TITLEDB:
            return json.dumps({
                "1": {"id": tid, "name": "Old Format Game",
                      "iconUrl": "https://img-eshop.cdn.nintendo.net/new.jpg"},
            }).encode()
        return b"\xff\xd8\xfftest"
    monkeypatch.setattr(covers, "download", download)
    queue = covers.CoverQueue()
    queue.submit([tid])
    for _ in range(100):
        if not queue.running:
            break
        time.sleep(.01)
    assert queue.error is None
    assert covers.image_path(tid).read_bytes() == b"\xff\xd8\xfftest"


def test_offline_cover_failure_is_bounded_and_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    calls = []
    def fail(*args):
        calls.append(True)
        raise OSError("offline")
    monkeypatch.setattr(covers, "download", fail)
    queue = covers.CoverQueue()
    queue.submit(["01007EF00011E000", "0100A6300150C000"])
    for _ in range(100):
        if not queue.running:
            break
        time.sleep(.01)
    assert not queue.running and queue.error == "offline"
    queue.submit(["01007EF00011E000"])
    assert len(calls) == 1


def test_cover_urls_cannot_target_local_network():
    assert not covers.allowed_url("http://127.0.0.1/image")
    assert not covers.allowed_url("https://img-eshop.cdn.nintendo.net.attacker.test/image")
    with pytest.raises(ValueError):
        covers.image_path("../../config")


def test_preferences_api_persists_and_disables_watcher(web_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    client = TestClient(create_app(web_ctx))
    response = client.post("/api/preferences", json={"auto_scan": False, "scan_interval": 30, "covers": False})
    assert response.status_code == 200
    assert preferences.load()["covers"] is False
    assert not web_ctx.library_watcher_running
    assert client.post("/api/preferences", json={"auto_scan": True, "scan_interval": 0, "covers": True}).status_code == 422
    web_ctx.covers.submit(["01007EF00011E000"])
    assert not web_ctx.covers.running
    assert client.get("/settings").status_code == 200


def test_cover_status_route_and_hidden_image_loading(web_ctx, tmp_path, monkeypatch):
    from switchagent import db, scanner
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (config.LIBRARY_DIR / "Wonder Boy [0100A6300150C000][v0].nsp").write_bytes(b"fixture")
    with db.open_db(web_ctx.db_path) as conn:
        scanner.scan_library_once(conn)
    client = TestClient(create_app(web_ctx))
    state = client.get("/api/covers/status")
    assert state.status_code == 200
    assert state.json()["phase"] == "Idle"
    html = client.get("/").text
    assert 'id="cover-status-text"' in html
    assert 'data-cover-id="0100A6300150C000"' in html
    assert 'loading="lazy"' not in html
