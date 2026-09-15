"""Tests for switchagent/cli.py's --version flag (Packaging Stage 6:
version is a single source of truth, read from installed package
metadata -- see switchagent/__init__.py)."""

from __future__ import annotations

import json

import pytest

from switchagent import __version__, cli, config, known_folders


def test_version_flag_prints_the_single_source_of_truth_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_version_flag_is_not_swallowed_by_the_implicit_scan_rewrite(capsys):
    """main() rewrites a bare `-x...`-shaped argv to `scan -x...` for
    backwards compatibility with the old scan_cli.py shape -- --version
    must be excluded from that rewrite, or it would 404 against `scan`'s
    own (version-less) argument parser instead of printing the version."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0


def test_help_still_shows_every_subcommand_not_just_scans(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for subcommand in ("scan", "preview", "mtp-mock", "mtp-test", "web", "doctor"):
        assert subcommand in out


# ---------------------------------------------------------------------------
# UI-001: `switch-agent doctor`
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_cli_config(tmp_path, monkeypatch):
    """Same isolation trick as tests/test_web_api.py's web_ctx fixture --
    doctor must never touch this machine's real config.yaml/DB/Downloads
    folder."""
    monkeypatch.setattr(config, "APP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "switchagent.db")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    return tmp_path


def test_doctor_text_output_lists_every_expected_check(capsys, isolated_cli_config):
    exit_code = cli.main(["doctor", "--mock"])
    out = capsys.readouterr().out
    assert exit_code in (0, 1)
    for expected in ("Database", "Library", "Work directory", "Free disk", "RAR extraction", "MTP/COM", "Devices"):
        assert expected in out


def test_doctor_json_output_is_valid_and_never_probes_com_in_mock_mode(capsys, isolated_cli_config):
    exit_code = cli.main(["doctor", "--json", "--mock"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["version"] == __version__
    com_check = next(c for c in payload["checks"] if c["name"] == "MTP/COM")
    assert "device(s) discoverable" not in com_check["detail"]
    assert exit_code in (0, 1)


def test_doctor_never_contains_a_raw_device_id(capsys, isolated_cli_config):
    """Regression guard: doctor output must only ever show safe
    fingerprints (mtp.windows.device_fingerprint), never a raw device_id
    (which embeds the real USB serial -- see mtp/windows.py's
    mask_device_id docstring)."""
    from switchagent import db

    with db.open_db(config.DB_PATH) as conn:
        db.upsert_device_seen(conn, "usb#vid_057e&pid_3000#SERIALNUMBER1234#{fingerprint-guid}", "Switch")

    exit_code = cli.main(["doctor", "--json", "--mock"])
    out = capsys.readouterr().out
    assert "SERIALNUMBER1234" not in out
    assert exit_code in (0, 1)
