"""A job whose connection to the console drops -- the console stops
answering, or goes away -- waits and continues on its own from where it
stopped, instead of failing or hanging."""

from __future__ import annotations

from switchagent import config, db, preview, queue_worker, scanner
from switchagent.mtp import MockMtpBackend
from switchagent.mtp import windows, wpd
from switchagent.mtp.errors import DeviceDisconnectedError

DEVICE = "mock-switch"


def _setup(isolated_db, monkeypatch, files=("a.bin", "b.bin")):
    conn, _ = isolated_db
    app = config.LIBRARY_DIR / "Release" / "switch" / "App"
    app.mkdir(parents=True)
    for name in files:
        (app / name).write_bytes(name.encode() * 10)
    monkeypatch.setattr(scanner, "is_file_stable", lambda _p: True)
    scanner.scan_library_once(conn)
    folder = app.parent
    row = db.get_library_item(conn, str(folder))
    job_id = queue_worker.create_job_from_report(
        conn, preview.preview_path(folder), library_item_id=row["id"], action="COPY_MERGE",
        target_device_id=DEVICE,
    )
    db.confirm_job(conn, job_id)
    backend = MockMtpBackend(device_id=DEVICE)
    backend.add_storage("SD_CARD")
    backend.add_storage("SD_INSTALL")
    registry = queue_worker.DeviceRegistry()
    registry.register(DEVICE, backend)
    return conn, backend, registry, job_id


def _sends(backend, dest):
    return [e for e in backend.operation_log if e.operation == "SEND_FILE" and e.details.get("path") == dest]


def test_disconnect_mid_job_waits_instead_of_failing(isolated_db, monkeypatch):
    conn, backend, registry, job_id = _setup(isolated_db, monkeypatch)
    backend.arm_failure("disconnect", dest_path="switch/App/b.bin")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "WAITING_FOR_DEVICE"
    job = db.get_job(conn, job_id)
    assert job["status"] == "WAITING_FOR_DEVICE"
    assert job["auto_resume"] == 1
    assert "Reconnect the Switch" in job["error"]
    assert conn.execute("SELECT COUNT(*) FROM install_history WHERE job_id=?", (job_id,)).fetchone()[0] == 0


def test_reconnecting_resumes_from_where_it_stopped(isolated_db, monkeypatch):
    conn, backend, registry, job_id = _setup(isolated_db, monkeypatch)
    backend.arm_failure("disconnect", dest_path="switch/App/b.bin")
    queue_worker.run_worker_once(conn, registry)

    # Still connected: too early to knock again.
    assert queue_worker.run_worker_once(conn, registry) is None

    backend.set_device_present(False)
    assert queue_worker.run_worker_once(conn, registry) is None
    assert db.get_job(conn, job_id)["status"] == "WAITING_FOR_DEVICE"

    backend.set_device_present(True)
    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "DONE"
    assert sorted(backend.storage_tree("SD_CARD").list_files()) == ["switch/App/a.bin", "switch/App/b.bin"]
    assert len(_sends(backend, "switch/App/a.bin")) == 1  # delivered before the drop, not sent again


def test_without_a_reconnect_it_tries_again_after_the_wait(isolated_db, monkeypatch):
    conn, backend, registry, job_id = _setup(isolated_db, monkeypatch)
    backend.arm_failure("disconnect", dest_path="switch/App/b.bin")
    queue_worker.run_worker_once(conn, registry)
    assert queue_worker.run_worker_once(conn, registry) is None

    conn.execute("UPDATE jobs SET resume_after='2000-01-01T00:00:00+00:00' WHERE id=?", (job_id,))
    conn.commit()

    assert queue_worker.run_worker_once(conn, registry).status == "DONE"


def test_a_silent_console_waits_for_reconnect(isolated_db, monkeypatch):
    conn, backend, registry, job_id = _setup(isolated_db, monkeypatch)
    real = backend.ensure_directory
    calls = {"n": 0}

    def silent_once(storage, path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeviceDisconnectedError(windows.CONSOLE_SILENT_ERROR)
        return real(storage, path)

    monkeypatch.setattr(backend, "ensure_directory", silent_once)

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "WAITING_FOR_DEVICE"
    assert db.get_job(conn, job_id)["error"].startswith("The Switch stopped responding")


def test_the_half_written_file_is_replaced_on_resume_not_a_conflict(isolated_db, monkeypatch):
    conn, backend, registry, job_id = _setup(isolated_db, monkeypatch)
    backend.arm_failure("disconnect", dest_path="switch/App/b.bin")
    queue_worker.run_worker_once(conn, registry)
    # What the dropped write left behind on the card.
    backend.storage_tree("SD_CARD").write_file("switch/App/b.bin", b"half")

    backend.set_device_present(False)
    queue_worker.run_worker_once(conn, registry)
    backend.set_device_present(True)

    assert queue_worker.run_worker_once(conn, registry).status == "DONE"
    assert backend.storage_tree("SD_CARD").read_file("switch/App/b.bin") == b"b.bin" * 10


def test_a_job_that_never_dropped_keeps_the_old_manual_rule(isolated_db, monkeypatch):
    conn, backend, registry, job_id = _setup(isolated_db, monkeypatch)
    conn.execute("UPDATE jobs SET attempt_count=1 WHERE id=?", (job_id,))
    conn.commit()
    backend.set_device_present(False)

    queue_worker.run_worker_once(conn, registry)

    assert db.get_job(conn, job_id)["status"] == "DEVICE_UNAVAILABLE"


# -- recognising a console that has stopped answering -------------------------

def test_a_timed_out_wpd_call_means_the_console_is_silent():
    timeout = wpd.ComError(wpd.ERROR_SEM_TIMEOUT_HRESULT, "EnumObjects")
    assert windows._console_silent(timeout)
    assert windows._console_silent(windows._WpdWriteFailed(timeout))
    assert not windows._console_silent(wpd.ComError(0x80004005, "EnumObjects"))
    assert not windows._console_silent(RuntimeError("other"))
