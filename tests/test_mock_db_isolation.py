"""Mock mode must never open the real database.

Regression test for a real incident (2026-09-17): a freshly built
`dist\\SwitchAgent\\SwitchAgent.exe` was launched by hand in mock mode to
look at the UI. dist/ carries no `portable.flag`, so paths.runtime_mode()
correctly reported "installed" and config.APP_DATA_ROOT resolved to the
REAL %LOCALAPPDATA%\\SwitchAgent -- the same database the user's actual
installation uses. build_mock_context()'s two fixture devices were
registered there and "Parent's Switch (mock)" / "Child's Switch (mock)"
stayed on the user's Devices page permanently.

What is asserted here is the fix's one load-bearing property: by the time
either entrypoint builds a mock context, config.DB_PATH already points at
config.MOCK_DB_PATH -- and the real DB file is never even created. Both
entrypoints are covered separately on purpose: they are two independent
copies of the same `use_mock = args.mock or MOCK_MTP` decision (cli.py for
`switch-agent web`, desktop.py for the packaged .exe), and the incident
happened through the desktop one.
"""

from __future__ import annotations

import argparse

import pytest

from switchagent import cli, config, desktop, known_folders
from switchagent.web import context as web_context


class _StopBeforeWorkers(Exception):
    """Raised from the stubbed context builder so neither entrypoint gets
    as far as starting worker threads, a library watcher or uvicorn --
    everything this test cares about has already happened by then."""


@pytest.fixture
def isolated_app_data(tmp_path, monkeypatch):
    """Point every config path at tmp_path, exactly like tests/test_cli.py's
    isolated_cli_config -- and keep DB_PATH/MOCK_DB_PATH as two distinct,
    not-yet-existing files so "was the real one opened?" is answerable by
    looking at the filesystem."""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "APP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "switchagent.db")
    monkeypatch.setattr(config, "MOCK_DB_PATH", data_dir / "switchagent-mock.db")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    return tmp_path


def _capture_mock_db_path(monkeypatch, module) -> list:
    """Replace `module`'s build_mock_context with a stub that records the
    db_path it was handed and aborts. cli.py imports it inside _cmd_web (so
    the stub belongs on web.context itself); desktop.py imports it at module
    import time (so the stub belongs on desktop)."""
    seen: list = []

    def _stub(db_path, **kwargs):
        seen.append(db_path)
        raise _StopBeforeWorkers

    monkeypatch.setattr(module, "build_mock_context", _stub)
    return seen


def test_cli_web_mock_builds_its_context_against_the_mock_database(isolated_app_data, monkeypatch):
    seen = _capture_mock_db_path(monkeypatch, web_context)
    args = cli.build_parser().parse_args(["web", "--mock"])

    with pytest.raises(_StopBeforeWorkers):
        cli._cmd_web(args)

    assert seen == [config.MOCK_DB_PATH]
    assert config.DB_PATH == config.MOCK_DB_PATH


def test_cli_web_without_mock_still_uses_the_real_database(isolated_app_data, monkeypatch):
    """The redirect must be exactly as narrow as it claims: no --mock, no
    MOCK_MTP, nothing moves."""
    real_db_path = config.DB_PATH
    seen: list = []

    def _stub(db_path, **kwargs):
        seen.append(db_path)
        raise _StopBeforeWorkers

    monkeypatch.setattr(web_context, "build_real_context", _stub)
    monkeypatch.delenv("MOCK_MTP", raising=False)
    args = cli.build_parser().parse_args(["web"])

    with pytest.raises(_StopBeforeWorkers):
        cli._cmd_web(args)

    assert seen == [real_db_path]
    assert config.DB_PATH == real_db_path


def test_mock_mtp_env_var_redirects_the_database_too(isolated_app_data, monkeypatch):
    """MOCK_MTP=true is documented (docs/WEB-UI.md) as equivalent to
    --mock -- including for this, or the env-var route would quietly keep
    the hole the flag route just closed."""
    seen = _capture_mock_db_path(monkeypatch, web_context)
    monkeypatch.setenv("MOCK_MTP", "true")
    args = cli.build_parser().parse_args(["web"])

    with pytest.raises(_StopBeforeWorkers):
        cli._cmd_web(args)

    assert seen == [config.MOCK_DB_PATH]


def test_desktop_entrypoint_mock_builds_its_context_against_the_mock_database(
    isolated_app_data, monkeypatch,
):
    """The packaged .exe's own entrypoint -- the one the 2026-09-17
    incident actually went through."""
    seen = _capture_mock_db_path(monkeypatch, desktop)
    args = argparse.Namespace(mock=True, no_browser=True, port=18999, host="127.0.0.1")

    with pytest.raises(_StopBeforeWorkers):
        desktop._run_primary_instance(args)

    assert seen == [config.MOCK_DB_PATH]
    assert config.DB_PATH == config.MOCK_DB_PATH


def test_the_real_database_file_is_never_created_by_a_mock_run(isolated_app_data, monkeypatch):
    """The end state a user would actually notice: after a mock run, the
    real switchagent.db does not exist (and so cannot have been polluted)."""
    real_db_path = config.DB_PATH
    _capture_mock_db_path(monkeypatch, desktop)
    args = argparse.Namespace(mock=True, no_browser=True, port=18999, host="127.0.0.1")

    with pytest.raises(_StopBeforeWorkers):
        desktop._run_primary_instance(args)

    assert not real_db_path.exists()


def test_the_two_database_paths_are_distinct_siblings():
    """Same directory, different filename -- so a mock run is obvious in a
    file listing next to the real database rather than hidden somewhere
    else on disk, and neither can ever resolve to the other."""
    assert config.MOCK_DB_PATH != config.DB_PATH
    assert config.MOCK_DB_PATH.parent == config.DB_PATH.parent
    assert "mock" in config.MOCK_DB_PATH.name
