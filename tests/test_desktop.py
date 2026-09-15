"""Tests for the pure/injectable parts of switchagent/desktop.py.

Full end-to-end coverage (a real uvicorn server + tray icon + browser)
belongs to a packaged smoke test (Stage 7), not a unit test -- see the
packaging report for what that covers. Here: the readiness-wait logic
(injectable clock/sleep, no real time spent) and CLI argument defaults.
"""

from __future__ import annotations

import pytest

from switchagent import __version__, desktop


class _FakeServer:
    def __init__(self, ready_after_n_polls: int | None):
        self._ready_after_n_polls = ready_after_n_polls
        self._polls = 0
        self.started = False

    def poll(self):
        self._polls += 1
        if self._ready_after_n_polls is not None and self._polls >= self._ready_after_n_polls:
            self.started = True


def test_wait_until_ready_returns_true_as_soon_as_server_started_flips():
    server = _FakeServer(ready_after_n_polls=3)
    clock = {"t": 0.0}

    def sleep_fn(_seconds):
        clock["t"] += 0.01
        server.poll()

    result = desktop._wait_until_ready(
        server, timeout_seconds=5.0, poll_interval_seconds=0.01,
        sleep_fn=sleep_fn, clock_fn=lambda: clock["t"],
    )

    assert result is True
    assert server.started is True


def test_wait_until_ready_times_out_if_server_never_starts():
    server = _FakeServer(ready_after_n_polls=None)
    clock = {"t": 0.0}

    def sleep_fn(_seconds):
        clock["t"] += 1.0  # advance the fake clock faster than the timeout

    result = desktop._wait_until_ready(
        server, timeout_seconds=3.0, poll_interval_seconds=0.5,
        sleep_fn=sleep_fn, clock_fn=lambda: clock["t"],
    )

    assert result is False
    assert server.started is False


def test_wait_until_ready_returns_true_immediately_if_already_started():
    server = _FakeServer(ready_after_n_polls=None)
    server.started = True
    sleeps = []

    result = desktop._wait_until_ready(
        server, timeout_seconds=5.0, sleep_fn=sleeps.append, clock_fn=lambda: 0.0,
    )

    assert result is True
    assert sleeps == []


def test_build_parser_defaults():
    args = desktop.build_parser().parse_args([])
    assert args.mock is False
    assert args.no_browser is False
    assert args.port == desktop.DEFAULT_PORT
    assert args.host == "0.0.0.0"


def test_local_only_override_and_browser_url(monkeypatch):
    assert desktop.build_parser().parse_args(["--host", "127.0.0.1"]).host == "127.0.0.1"
    urls = []
    monkeypatch.setattr(desktop.webbrowser, "open", urls.append)
    desktop._open_browser(9001)
    assert urls == ["http://127.0.0.1:9001/"]


def test_build_parser_accepts_mock_and_no_browser_and_port():
    args = desktop.build_parser().parse_args(["--mock", "--no-browser", "--port", "9001"])
    assert args.mock is True
    assert args.no_browser is True
    assert args.port == 9001


def test_version_flag_prints_the_single_source_of_truth_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        desktop.build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_uvicorn_with_log_config_none_survives_stdio_being_none(monkeypatch):
    """Regression test for a real bug found via a packaged-EXE smoke test:
    a windowed (console=False) PyInstaller build launched without an
    inherited console (double-click, Start Menu shortcut -- anything that
    doesn't explicitly redirect stdio) runs with sys.stdout/sys.stderr set
    to None. uvicorn's DEFAULT logging setup crashes in that state
    (DefaultFormatter's __init__ touches stream.isatty()) -- desktop.py
    works around this by passing log_config=None to uvicorn.Config().
    This confirms the workaround actually prevents the crash, under the
    exact None-stdio condition that caused it."""
    import sys

    from fastapi import FastAPI

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    import uvicorn

    uvicorn.Config(FastAPI(), host="127.0.0.1", port=0, log_config=None)  # must not raise


def test_uvicorn_without_the_fix_actually_reproduces_the_original_crash(monkeypatch):
    """Documents the bug this workaround exists for: omitting
    log_config=None (uvicorn's own default) under None stdio is what
    originally crashed -- if this test ever stops raising, uvicorn's
    internals changed and desktop.py's workaround comment should be
    re-checked, not silently trusted. logging.config.dictConfig wraps the
    real AttributeError (stream.isatty() on None) in a ValueError -- both
    are asserted so this test fails loudly, not silently, if the
    underlying cause ever changes shape."""
    import sys

    from fastapi import FastAPI

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    import uvicorn

    with pytest.raises(ValueError) as exc_info:
        uvicorn.Config(FastAPI(), host="127.0.0.1", port=0)
    assert isinstance(exc_info.value.__cause__, AttributeError)


def test_runtime_info_path_is_under_app_data_root(monkeypatch, tmp_path):
    from switchagent import config

    monkeypatch.setattr(config, "APP_DATA_ROOT", tmp_path)
    assert desktop._runtime_info_path() == tmp_path / "runtime.json"
