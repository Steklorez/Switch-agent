"""Windows-safe single-instance guard for the desktop launcher
(switchagent/desktop.py). Combines two independent signals:

  - An OS-level named mutex (acquire_mutex() below) -- the strong,
    Windows-guaranteed signal: the kernel releases a named mutex the
    instant its owning process ends, crash or clean exit alike, so
    `ERROR_ALREADY_EXISTS` reliably means some process is still alive
    holding it, never a stale leftover.
  - A small runtime-state file (pid/port/started_at) that a second launch
    reads to find the *first* instance's actual port, verified live with
    a real HTTP GET to /api/health before being trusted for anything --
    per the task's explicit requirement, the file's mere existence or
    content is never trusted blindly.

Neither signal alone is enough: the mutex proves "something is running"
but not which port to open in the browser; the file proves a port was
once written but not that anything is still listening there. Together:
mutex says whether to even ask, and a live health probe says whether the
answer can be trusted.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("switchagent.desktop")

MUTEX_NAME = "SwitchAgent-SingleInstance-Mutex"


# ---------------------------------------------------------------------------
# OS-level named mutex (real Win32 calls -- not meaningfully unit-testable
# without a real Windows session, but this module IS always running on
# Windows, so exercised directly rather than mocked).
# ---------------------------------------------------------------------------

def acquire_mutex():
    """Returns (handle, already_running). The caller MUST keep `handle`
    alive for the process's entire lifetime -- closing/garbage-collecting
    it releases the mutex early -- and must call release_mutex(handle) on
    clean shutdown (see desktop.py's main())."""
    import win32event
    import win32api
    import winerror

    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    return handle, already_running


def release_mutex(handle) -> None:
    if handle is None:
        return
    import win32api

    win32api.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Runtime-state file: pid/port/started_at. Advisory only -- see module
# docstring; probe_health() is what actually decides trust.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeInfo:
    pid: int
    port: int
    started_at: str


def write_runtime_info(path: Path, *, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"pid": os.getpid(), "port": port, "started_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(data), encoding="utf-8")


def read_runtime_info(path: Path) -> Optional[RuntimeInfo]:
    """Never raises -- a corrupt/partial/missing file just means "no
    usable hint", handled the same as if it never existed."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RuntimeInfo(pid=int(data["pid"]), port=int(data["port"]), started_at=str(data["started_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_runtime_info(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Liveness probe -- the one thing that actually earns trust.
# ---------------------------------------------------------------------------

def _real_http_get(url: str, timeout: float) -> tuple[int, bytes]:
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 -- fixed http://127.0.0.1 URL only
        return resp.status, resp.read()


def probe_health(
    port: int, *, timeout_seconds: float = 1.0, http_get: Callable[[str, float], tuple[int, bytes]] = _real_http_get,
) -> bool:
    """True only if a real SwitchAgent instance answers /api/health on
    `port` right now. Any exception (connection refused, timeout, a
    completely different service on that port) means False -- never
    raises, never assumed True from a mutex or file alone."""
    try:
        status, body = http_get(f"http://127.0.0.1:{port}/api/health", timeout_seconds)
    except Exception:  # noqa: BLE001 -- any failure to connect/read means "not alive", not an error
        return False
    if status != 200:
        return False
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False
    return data.get("app") == "SwitchAgent" and data.get("status") == "ok"


def wait_for_existing_instance(
    port: int,
    *,
    attempts: int = 5,
    interval_seconds: float = 1.0,
    probe: Callable[[int], bool] = probe_health,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Retries probe() briefly -- handles the narrow race where the first
    instance's mutex already exists but its HTTP server hasn't finished
    starting yet (see desktop.py's readiness wait on the primary-instance
    side of this same race)."""
    for attempt in range(attempts):
        if probe(port):
            return True
        if attempt < attempts - 1:
            sleep_fn(interval_seconds)
    return False
