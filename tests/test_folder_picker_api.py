from fastapi.testclient import TestClient
from switchagent import folder_picker
from switchagent.web.app import create_app
from .test_web_api import web_ctx


def test_picker_local_cancel_and_remote_rejection(web_ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(folder_picker, "pick_folder", lambda: calls.append(True))
    app = create_app(web_ctx)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert client.post("/api/settings/library-dir/pick").json() == {"path": None}
    with TestClient(app, client=("192.168.1.42", 12345)) as client:
        assert client.post("/api/settings/library-dir/pick").status_code == 403
    assert calls == [True]


def test_picker_unexpected_failure_returns_json(web_ctx, monkeypatch):
    def fail():
        raise AttributeError("missing Windows flag")
    monkeypatch.setattr(folder_picker, "pick_folder", fail)
    with TestClient(create_app(web_ctx), client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/settings/library-dir/pick")
    assert response.status_code == 503
    assert response.json()["detail"] == "missing Windows flag"


def test_native_picker_flags_and_unicode(monkeypatch):
    import os
    import pytest
    if os.name != "nt":
        pytest.skip("Windows shell API")
    from win32com.shell import shell
    calls = []
    def browse(*args):
        calls.append(args)
        return ([b"pidl"], "Games", 0)
    monkeypatch.setattr(shell, "SHBrowseForFolder", browse)
    monkeypatch.setattr(shell, "SHGetPathFromIDListW", lambda pidl: "D:\\Игры\\Русификаторы")
    assert folder_picker.pick_folder() == "D:\\Игры\\Русификаторы"
    assert calls[0][3] & 0x0040


def test_settings_accepts_multiple_folders_and_rejects_invalid_set(web_ctx, tmp_path):
    from switchagent import config
    second = tmp_path / "Игры"
    second.mkdir()
    paths = [str(config.LIBRARY_DIR), str(second)]
    client = TestClient(create_app(web_ctx))
    try:
        result = client.post("/api/settings/library-dir", json={"path": paths[0], "paths": paths})
        assert result.status_code == 200
        assert result.json()["library_dirs"] == paths
        invalid = client.post("/api/settings/library-dir", json={"path": paths[0], "paths": [str(tmp_path / "missing")]})
        assert invalid.status_code == 400
        assert client.get("/api/settings").json()["library_dirs"] == paths
    finally:
        web_ctx.stop_library_watcher()
