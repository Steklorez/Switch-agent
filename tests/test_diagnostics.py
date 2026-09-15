"""Tests for switchagent/diagnostics.py -- the single reusable engine
behind UI-001 (Settings Diagnostics section, `switch-agent doctor`) and
UI-008 (export). Covers: report assembly is honest about each input,
sanitization never leaks the real home directory or a raw device_id, and
the text/JSON renderers stay consistent with each other.
"""

from __future__ import annotations

import os

from switchagent import diagnostics


def _base_kwargs(tmp_path, **overrides):
    kwargs = dict(
        version="0.1.0-rc1",
        runtime_mode="dev",
        resource_root=tmp_path / "resource",
        app_data_root=tmp_path / "appdata",
        config_path=tmp_path / "appdata" / "config.yaml",
        db_path=tmp_path / "appdata" / "data" / "switchagent.db",
        library_dir=tmp_path / "library",
        library_dir_configured=True,
        work_dir=tmp_path / "appdata" / "work",
        rar_backend_available=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_build_report_flags_missing_db_as_warning_not_error(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    db_check = next(c for c in report.checks if c.name == "Database")
    assert db_check.status == "WARNING"
    assert "not created yet" in db_check.detail


def test_build_report_ok_for_a_real_schema_current_db(tmp_path):
    from switchagent import db as db_mod

    db_path = tmp_path / "appdata" / "data" / "switchagent.db"
    with db_mod.open_db(db_path):
        pass  # just run schema/migrations

    report = diagnostics.build_report(**_base_kwargs(tmp_path, db_path=db_path))
    db_check = next(c for c in report.checks if c.name == "Database")
    assert db_check.status == "OK"


def test_build_report_flags_missing_library_dir(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    lib_check = next(c for c in report.checks if c.name == "Library")
    assert lib_check.status == "WARNING"


def test_build_report_ok_for_existing_library_dir(tmp_path):
    library_dir = tmp_path / "library"
    library_dir.mkdir(parents=True)
    report = diagnostics.build_report(**_base_kwargs(tmp_path, library_dir=library_dir))
    lib_check = next(c for c in report.checks if c.name == "Library")
    assert lib_check.status == "OK"


def test_work_dir_size_is_reported_when_present(tmp_path):
    work_dir = tmp_path / "appdata" / "work"
    work_dir.mkdir(parents=True)
    (work_dir / "job-1").mkdir()
    (work_dir / "job-1" / "payload.bin").write_bytes(b"x" * 2048)

    report = diagnostics.build_report(**_base_kwargs(tmp_path, work_dir=work_dir))
    work_check = next(c for c in report.checks if c.name == "Work directory")
    assert work_check.status == "OK"
    assert "KB used" in work_check.detail or "B used" in work_check.detail


def test_rar_backend_unavailable_is_warning_not_error(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path, rar_backend_available=False))
    rar_check = next(c for c in report.checks if c.name == "RAR extraction")
    assert rar_check.status == "WARNING"


def test_com_check_is_not_probed_by_default(tmp_path):
    """The HTTP/Settings path must never trigger a live device-discovery
    probe -- probe_com defaults to False."""
    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    com_check = next(c for c in report.checks if c.name == "MTP/COM")
    assert "discoverable" not in com_check.detail


def test_watcher_and_worker_are_na_when_not_supplied(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    watcher_check = next(c for c in report.checks if c.name == "Watcher")
    worker_check = next(c for c in report.checks if c.name == "Worker")
    assert watcher_check.status == "N/A"
    assert worker_check.status == "N/A"


def test_worker_paused_is_reported_as_a_warning_not_an_error(tmp_path):
    report = diagnostics.build_report(
        **_base_kwargs(tmp_path, worker_running=True, worker_paused=True, worker_last_heartbeat_at="2026-01-01T00:00:00+00:00"),
    )
    worker_check = next(c for c in report.checks if c.name == "Worker")
    assert worker_check.status == "WARNING"
    assert "paused" in worker_check.detail


def test_worker_running_and_unpaused_is_ok_and_shows_heartbeat(tmp_path):
    report = diagnostics.build_report(
        **_base_kwargs(tmp_path, worker_running=True, worker_paused=False, worker_last_heartbeat_at="2026-01-01T00:00:00+00:00"),
    )
    worker_check = next(c for c in report.checks if c.name == "Worker")
    assert worker_check.status == "OK"
    assert "2026-01-01T00:00:00+00:00" in worker_check.detail


def test_overall_status_is_worst_of_all_checks(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path, rar_backend_available=False))
    # No DB yet (WARNING) + no library dir (WARNING) + RAR unavailable
    # (WARNING) -- nothing here should escalate to ERROR on a completely
    # fresh, not-yet-configured installation.
    assert report.overall_status == "WARNING"


def test_sanitize_path_replaces_home_directory_with_userprofile_placeholder(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))

    report = diagnostics.build_report(**_base_kwargs(tmp_path, app_data_root=home / "AppData" / "SwitchAgent"))
    assert "%USERPROFILE%" in report.app_data_root
    assert str(home).lower() not in report.app_data_root.lower()


def test_device_fingerprints_are_present_but_no_raw_device_id_anywhere(tmp_path):
    raw_id = "usb#vid_057e&pid_3000#SERIALNUMBER1234#{fingerprint-guid}"
    report = diagnostics.build_report(
        **_base_kwargs(tmp_path, known_device_count=1, connected_device_count=1, device_fingerprints=["abc123"]),
    )
    text = diagnostics.format_report_text(report)
    payload = diagnostics.report_to_dict(report)
    assert raw_id not in text
    assert "abc123" in text
    assert raw_id not in str(payload)
    assert "abc123" in payload["device_fingerprints"]


def test_report_to_dict_and_format_text_agree_on_overall_status(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    payload = diagnostics.report_to_dict(report)
    assert payload["overall_status"] == report.overall_status
    text = diagnostics.format_report_text(report)
    for check in report.checks:
        assert check.name in text


# ---------------------------------------------------------------------------
# UI-008: export
# ---------------------------------------------------------------------------

def test_build_export_dict_includes_report_fields_plus_job_errors_and_log_tail(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    job_errors = [{"id": 1, "display_name": "Some Game", "status": "FAILED", "error": "disk full", "finished_at": "t"}]
    payload = diagnostics.build_export_dict(report, recent_job_errors=job_errors, log_tail="hello world")
    assert payload["version"] == report.version
    assert payload["recent_job_errors"] == job_errors
    assert payload["log_tail"] == "hello world"


def test_build_export_dict_sanitizes_a_job_errors_embedded_home_path_and_raw_device_id(tmp_path, monkeypatch):
    """HW-005 finding: a job's error text is free text from whatever code
    path failed it (e.g. manifest.ManifestError can legitimately embed a
    full local filesystem path) -- confirmed live against real hardware
    that this was copied into the export verbatim, unlike every other
    field this module emits (all already sanitized). Job errors can also,
    in principle, embed a raw device_id if some future error message ever
    does (mask_device_id already guards every other place this project
    logs one) -- covered here too, for the same reason.

    Also reproduces a SECOND real-hardware finding from the same session:
    the bare username can appear a second time embedded in an unrelated
    string shape (there: a hyphenated scratchpad directory name, not a
    `C:\\Users\\<name>\\...` path prefix at all) -- the home-directory-
    PREFIX substitution alone does not and cannot catch that second
    occurrence, so this asserts the broader bare-username sweep does."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("USERNAME", "EvgeniiTestUser")
    raw_id = r"::{GUID}\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"

    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    job_errors = [{
        "id": 9, "display_name": "Some Game", "status": "FAILED",
        "error": (
            f"could not build manifest: source is not under a known scanned root: {home}\\Downloads\\game.nsp "
            f"(device {raw_id}) -- see C--Users-EvgeniiTestUser-scratch\\game.nsp"
        ),
        "finished_at": "t",
    }]

    payload = diagnostics.build_export_dict(report, recent_job_errors=job_errors)

    sanitized_error = payload["recent_job_errors"][0]["error"]
    assert str(home).lower() not in sanitized_error.lower()
    assert "EvgeniiTestUser" not in sanitized_error  # neither occurrence survives
    assert "xtj10229424075" not in sanitized_error
    assert "%USERPROFILE%" in sanitized_error
    assert "%USERNAME%" in sanitized_error
    assert "REDACTED" in sanitized_error
    # Never lose the actually-useful part of the message.
    assert "game.nsp" in sanitized_error
    # Both export formats must agree -- export_dict_to_text renders the
    # same already-sanitized payload, never a second, separate pass.
    text = diagnostics.export_dict_to_text(payload)
    assert str(home).lower() not in text.lower()
    assert "xtj10229424075" not in text


def test_export_dict_to_text_renders_job_errors_and_log_tail(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    job_errors = [{"id": 7, "display_name": "Some Game", "status": "FAILED", "error": "disk full", "finished_at": "t"}]
    payload = diagnostics.build_export_dict(report, recent_job_errors=job_errors, log_tail="a sanitized line")
    text = diagnostics.export_dict_to_text(payload)
    assert "#7 Some Game [FAILED]: disk full" in text
    assert "a sanitized line" in text


def test_export_dict_to_text_omits_sections_that_are_empty(tmp_path):
    report = diagnostics.build_report(**_base_kwargs(tmp_path))
    payload = diagnostics.build_export_dict(report)
    text = diagnostics.export_dict_to_text(payload)
    assert "Recent job errors" not in text
    assert "Log tail" not in text


def test_sanitize_free_text_redacts_raw_device_id_and_home_dir(monkeypatch, tmp_path):
    home = tmp_path / "Users" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))

    raw_id = r"usb#vid_057e&pid_3000#SERIALNUMBER1234#{fingerprint-guid}"
    text = f"connected to {raw_id}\nwrote to {home}\\SwitchAgent\\work\\job-1"
    sanitized = diagnostics.sanitize_free_text(text)
    assert "SERIALNUMBER1234" not in sanitized
    assert str(home).lower() not in sanitized.lower()
    assert "%USERPROFILE%" in sanitized


def test_read_sanitized_log_tail_returns_none_for_missing_file(tmp_path):
    assert diagnostics.read_sanitized_log_tail(tmp_path / "does-not-exist.log") is None


def test_read_sanitized_log_tail_reads_and_sanitizes_a_real_file(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))

    log_path = tmp_path / "switchagent.log"
    raw_id = r"usb#vid_057e&pid_3000#SERIALNUMBER1234#{fingerprint-guid}"
    log_path.write_text(f"2026-01-01 INFO connected to {raw_id}\n2026-01-01 INFO app_data_root={home}\n", encoding="utf-8")

    tail = diagnostics.read_sanitized_log_tail(log_path)
    assert tail is not None
    assert "SERIALNUMBER1234" not in tail
    assert "%USERPROFILE%" in tail


def test_read_sanitized_log_tail_truncates_to_max_bytes_and_drops_partial_first_line(tmp_path):
    log_path = tmp_path / "switchagent.log"
    log_path.write_text("".join(f"line-{i:04d}\n" for i in range(1000)), encoding="utf-8")

    tail = diagnostics.read_sanitized_log_tail(log_path, max_bytes=200)
    assert tail is not None
    assert len(tail.encode("utf-8")) <= 210  # a little slack for the dropped partial line
    assert tail.startswith("line-")  # never starts mid-word
