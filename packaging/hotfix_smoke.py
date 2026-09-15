"""Run a built portable app with synthetic archives and mock MTP only.

Usage: python packaging/hotfix_smoke.py path/to/SwitchAgent-Portable
Copies the build into an isolated temporary root and closes only its own PID.
"""
import json
import tomllib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path


def main():
    import win32con
    import win32gui
    import win32process

    with tempfile.TemporaryDirectory(prefix="switchagent-hotfix-smoke-") as tmp:
        root = Path(tmp)
        app = root / "app"
        shutil.copytree(Path(sys.argv[1]).resolve(), app)
        assert (app / "portable.flag").exists()
        library = root / "library"
        library.mkdir()
        (app / "config.yaml").write_text("library:\n  source_dir: " + json.dumps(str(library)) + "\n", encoding="utf-8")
        names = ["Base [0100AAAAAAAAA000][v0].nsz", "Update [0100AAAAAAAAA800][v1].nsz",
                 "DLC [0100AAAAAAAAB001][v0].nsz", "Other [0100BBBBBBBBB000][v0].nsz"]
        for archive, entries in (("a.zip", names[:3]), ("b.zip", names[3:])):
            with zipfile.ZipFile(library / archive, "w") as z:
                for name in entries:
                    z.writestr(name, (name.encode() * 100))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def request(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read())

        def until(fn, timeout=40):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if proc.poll() is not None:
                    raise RuntimeError(f"smoke process exited: {proc.returncode}")
                try:
                    result = fn()
                    if result:
                        return result
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            raise TimeoutError("packaged smoke timed out")

        proc = subprocess.Popen([str(app / "SwitchAgent.exe"), "--mock", "--no-browser", "--port", str(port)])
        try:
            health = until(lambda: request("/api/health"))
            expected_version = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
            assert health["version"] == expected_version, health
            for page in ("/", "/queue", "/history", "/settings"):
                with urllib.request.urlopen(base + page) as response:
                    assert response.status == 200
            request("/api/scan", {})
            entries = until(lambda: (rows if len(rows := request("/api/library")) == 2 else None))
            response = request("/api/preparations", {"library_item_ids": [e["id"] for e in entries],
                                                    "target_device_id": "mock-switch-parent"})
            assert response["preparation_id"]
            state = until(lambda: next((s for s in request("/api/preparations") if s["phase"] in ("Ready", "Failed")), None))
            assert state["phase"] == "Ready", state
            assert len(state["result"]["created"]) == 4
            history = until(lambda: (rows if len(rows := request("/api/history")) == 4 else None))
            assert all(h["outcome"] in ("DONE", "DONE_UNVERIFIED") for h in history), history
            until(lambda: not list((app / "work").glob("batch-*")))
            assert len(list(library.glob("*.zip"))) == 2
            assert not list((app / "work").rglob("*.nsz"))
            handles = []
            def collect(hwnd, unused):
                if win32process.GetWindowThreadProcessId(hwnd)[1] == proc.pid:
                    handles.append(hwnd)
            win32gui.EnumWindows(collect, None)
            assert handles, "no owned tray window for graceful shutdown"
            for hwnd in handles:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            assert proc.wait(timeout=25) == 0
            print(f"PASS: portable {expected_version}; 2 archives / 4 jobs; history retained; staging clean; sources preserved; graceful shutdown")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


if __name__ == "__main__":
    main()
