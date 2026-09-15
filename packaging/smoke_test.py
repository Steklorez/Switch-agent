"""Packaged smoke test -- runs the ACTUAL built SwitchAgent.exe (not the
switchagent Python source package) against MockMtpBackend only, verifies
real HTTP readiness/health, exercises one more API endpoint, then
requests a graceful shutdown the exact same way a user's tray-icon Exit
click would (a real WM_CLOSE posted to the app's hidden window -- see
switchagent/tray.py's _TrayWindow.close()), and confirms the process
actually exits cleanly (code 0) with no fatal error in its log.

Used both for a quick local check after `pyinstaller ...` and by
.github/workflows/release-windows.yml. --mock is mandatory here, not
optional -- CI has no physical Switch, and this must never touch real
MTP hardware.

Usage:
    python packaging/smoke_test.py dist/SwitchAgent/SwitchAgent.exe
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def _wait_for_health(proc: subprocess.Popen, port: int, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            raise RuntimeError(
                f"SwitchAgent.exe exited early (code {exit_code}) before answering /api/health -- "
                "possibly another instance's single-instance mutex was already held on this machine; "
                "make sure no stray SwitchAgent.exe is running before starting this smoke test."
            )
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2.0) as resp:
                if resp.status == 200:
                    return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 -- keep polling until timeout
            last_error = exc
        time.sleep(0.5)
    raise TimeoutError(f"SwitchAgent.exe never answered /api/health within {timeout_seconds}s: {last_error}")


def _wait_for_http_ok(url: str, timeout_seconds: float) -> int:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                return resp.status
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.5)
    raise TimeoutError(f"{url} never responded within {timeout_seconds}s: {last_error}")


def _request_graceful_shutdown(timeout_seconds: float = 5.0, *, process_id: int) -> bool:
    """Posts the Exit menu command to this process's hidden tray window.
    Exercises WM_COMMAND -> close -> WM_DESTROY, not just WM_CLOSE.
    Returns False (caller
    should fall back to a hard kill) if the window can't be found within
    `timeout_seconds`, e.g. no interactive window station on some CI
    configurations.

    Retries finding the window rather than checking once: tray creation
    (switchagent/desktop.py calls tray.run_tray_icon() only after HTTP
    readiness and opening the browser) can still be a few hundred
    milliseconds away at the exact moment /api/health first answers --
    found via this exact smoke test flaking during a release-readiness
    audit (one run in a handful found no window and fell back to a hard
    kill, which doesn't exercise graceful shutdown at all and returns a
    non-zero exit code that would spuriously fail CI)."""
    import win32con
    import win32gui
    import win32process

    deadline = time.monotonic() + timeout_seconds
    hwnd = 0
    while time.monotonic() < deadline:
        handles = []
        def collect(candidate, unused):
            if (win32process.GetWindowThreadProcessId(candidate)[1] == process_id
                    and win32gui.GetClassName(candidate) == "SwitchAgentTrayWindow"):
                handles.append(candidate)
        win32gui.EnumWindows(collect, None)
        hwnd = handles[0] if handles else 0
        if hwnd:
            break
        time.sleep(0.2)

    if not hwnd:
        return False
    win32gui.PostMessage(hwnd, win32con.WM_COMMAND, 1024, 0)  # tray.MENU_EXIT_ID
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe_path", type=Path, help="path to the built SwitchAgent.exe")
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--shutdown-timeout", type=float, default=20.0)
    args = parser.parse_args()

    if not args.exe_path.is_file():
        print(f"error: {args.exe_path} does not exist", file=sys.stderr)
        return 1

    # Isolated app-data root for this smoke test run -- installed mode
    # would otherwise write to the real %LOCALAPPDATA%\SwitchAgent on
    # whatever machine runs this (a CI runner or a developer's own PC).
    with tempfile.TemporaryDirectory(prefix="switchagent-smoke-") as tmp_appdata:
        env = dict(os.environ)
        env["LOCALAPPDATA"] = tmp_appdata

        print(f"launching {args.exe_path} --mock --no-browser --port {args.port}")
        proc = subprocess.Popen(
            [str(args.exe_path), "--mock", "--no-browser", "--port", str(args.port)],
            env=env,
        )

        try:
            health = _wait_for_health(proc, args.port, timeout_seconds=args.startup_timeout)
            print(f"health: {health}")
            if health.get("app") != "SwitchAgent" or health.get("status") != "ok":
                print(f"error: unexpected /api/health payload: {health}", file=sys.stderr)
                return 1

            status = _wait_for_http_ok(f"http://127.0.0.1:{args.port}/api/queue", timeout_seconds=10.0)
            print(f"/api/queue -> {status}")
            if status != 200:
                print(f"error: /api/queue returned {status}, expected 200", file=sys.stderr)
                return 1

            print("requesting graceful shutdown through tray Exit command...")
            if not _request_graceful_shutdown(process_id=proc.pid):
                print("warning: could not find the app's window -- falling back to terminate()", file=sys.stderr)
                proc.terminate()

            try:
                exit_code = proc.wait(timeout=args.shutdown_timeout)
            except subprocess.TimeoutExpired:
                print("error: process did not exit after graceful shutdown request -- killing it", file=sys.stderr)
                proc.kill()
                proc.wait(timeout=10)
                return 1

            print(f"process exited with code {exit_code}")
            if exit_code != 0:
                return 1

            log_path = Path(tmp_appdata) / "SwitchAgent" / "logs" / "switchagent.log"
            if log_path.is_file():
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                if "fatal" in log_text.lower() or "Traceback" in log_text:
                    print(f"error: fatal error found in {log_path}:\n{log_text}", file=sys.stderr)
                    return 1
                print(f"log OK ({log_path}, {len(log_text)} bytes, no fatal/traceback)")
        finally:
            if proc.poll() is None:
                proc.kill()

    print("PACKAGED SMOKE TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
