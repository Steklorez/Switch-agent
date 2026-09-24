"""The Queue page's Abort: stop everything that is queued, stop every
preparation run from creating more jobs, and stop the transfer in flight at
its next safe point -- never by force, never by pretending.

Three layers, each tested where it lives:
  - queue_worker: where a running job may stop (before it starts, between
    files, mid-file through the progress callback) and what it records;
  - RealMtpBackend: an abort raised from the progress callback is never
    retried and never handed to the Shell fallback;
  - web: POST /api/queue/abort and GET /api/worker/status, preparation runs.
"""

from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders, preview, queue_worker
from switchagent import manifest as manifest_mod
from switchagent.mtp import MockMtpBackend, wpd
from switchagent.mtp.errors import TransferAborted
from switchagent.web import services
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context
from switchagent.web.preparation import PreparationQueue

from .test_stage41_recovery import _make_mod_item


class _StreamingMock(MockMtpBackend):
    """A MockMtpBackend that reports progress partway through each file,
    before anything is written -- the way the WPD transport does while the
    bytes are still moving. `on_progress` lets a test press Abort at an
    exact moment."""

    def __init__(self, *args, on_progress=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_progress = on_progress

    def send_file(self, storage, dest_path, source_path, *, overwrite=False, progress=None, expected_sha256=None):
        if progress is not None:
            total = source_path.stat().st_size
            if self.on_progress:
                self.on_progress(dest_path)
            progress(max(0, total - 1), total)  # not the last call: bytes are still moving
        return super().send_file(
            storage, dest_path, source_path, overwrite=overwrite, progress=progress, expected_sha256=expected_sha256,
        )


def _registry(backend):
    backend.add_storage("SD_CARD")
    backend.add_storage("SD_INSTALL")
    registry = queue_worker.DeviceRegistry()
    registry.register(backend.device_id, backend)
    return registry


def _confirmed_mod_job(conn, inbox_dir, files, title_id="0100000000010000"):
    item_id, mod_root = _make_mod_item(conn, inbox_dir, title_id, files)
    report = preview.preview_path(mod_root)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)
    return job_id


def _history(conn, job_id):
    return conn.execute("SELECT * FROM install_history WHERE job_id=?", (job_id,)).fetchall()


# ---------------------------------------------------------------------------
# queue_worker
# ---------------------------------------------------------------------------


def test_abort_mid_file_interrupts_the_job_and_writes_nothing_more(isolated_db):
    conn, inbox_dir = isolated_db
    aborted = {"now": False}
    backend = _StreamingMock(device_id="mock-switch-parent")
    registry = _registry(backend)
    job_id = _confirmed_mod_job(conn, inbox_dir, {"a.bin": b"first file", "b.bin": b"second file"})
    title = "0100000000010000"
    second = f"atmosphere/contents/{title}/romfs/b.bin"

    def press_abort_during(dest_path):
        if dest_path == second:
            aborted["now"] = True

    backend.on_progress = press_abort_during

    outcome = queue_worker.run_worker_once(conn, registry, should_abort=lambda: aborted["now"])

    assert outcome.status == "INTERRUPTED"
    assert "stopped by Abort while sending" in outcome.error
    job = db.get_job(conn, job_id)
    assert job["status"] == "INTERRUPTED"
    assert not job["abandoned"]  # stays in Queue with its Retry, it is not a cancel
    # The file in flight is still named -- what lets a Retry replace it.
    assert job["current_file"] == second
    assert manifest_mod.load_progress(job_id) == {f"atmosphere/contents/{title}/romfs/a.bin"}
    assert backend.storage_tree("SD_CARD").list_files() == [f"atmosphere/contents/{title}/romfs/a.bin"]
    assert [row["outcome"] for row in _history(conn, job_id)] == ["INTERRUPTED"]


def test_abort_between_files_stops_before_the_next_one(isolated_db):
    """A backend that only reports progress once a file is complete (the
    Shell fallback reports nothing at all) cannot be stopped mid-file: the
    job stops at the next file boundary instead, with nothing half-written."""
    conn, inbox_dir = isolated_db
    aborted = {"now": False}
    backend = MockMtpBackend(device_id="mock-switch-parent")  # one progress call, after the whole file
    registry = _registry(backend)
    job_id = _confirmed_mod_job(conn, inbox_dir, {"a.bin": b"one", "b.bin": b"two", "c.bin": b"three"})

    original = backend.send_file

    def send_then_abort(*args, **kwargs):
        result = original(*args, **kwargs)
        aborted["now"] = True  # pressed while the first file was being copied
        return result

    backend.send_file = send_then_abort

    outcome = queue_worker.run_worker_once(conn, registry, should_abort=lambda: aborted["now"])

    assert outcome.status == "INTERRUPTED"
    assert "after 1 of 3 file(s)" in outcome.error
    assert "nothing was left half-written" in outcome.error
    assert len(backend.storage_tree("SD_CARD").list_files()) == 1
    assert len(manifest_mod.load_progress(job_id)) == 1


def test_the_last_progress_report_never_aborts_a_fully_sent_file(isolated_db):
    """When every byte is across, only the device's own commit is left --
    aborting there would throw away a file the console already has."""
    conn, inbox_dir = isolated_db
    backend = MockMtpBackend(device_id="mock-switch-parent")
    registry = _registry(backend)
    job_id = _confirmed_mod_job(conn, inbox_dir, {"only.bin": b"single file"})
    aborted = {"now": False}
    original = backend.send_file

    def abort_while_sending(*args, **kwargs):
        # Pressed while the file is on its way; the one progress call this
        # mock makes reports sent == total.
        aborted["now"] = True
        return original(*args, **kwargs)

    backend.send_file = abort_while_sending

    outcome = queue_worker.run_worker_once(conn, registry, should_abort=lambda: aborted["now"])

    assert outcome.status == "DONE"
    assert db.get_job(conn, job_id)["status"] == "DONE"


def test_an_abort_before_the_job_started_starts_nothing(isolated_db):
    conn, inbox_dir = isolated_db
    backend = MockMtpBackend(device_id="mock-switch-parent")
    registry = _registry(backend)
    job_id = _confirmed_mod_job(conn, inbox_dir, {"a.bin": b"x"})

    assert queue_worker.run_worker_once(conn, registry, should_abort=lambda: True) is None
    assert db.get_job(conn, job_id)["status"] == "CONFIRMED"  # the pass simply did not start it
    assert backend.storage_tree("SD_CARD").list_files() == []


def test_an_abort_while_sources_were_being_checked_cancels_without_history(isolated_db, monkeypatch):
    conn, inbox_dir = isolated_db
    backend = MockMtpBackend(device_id="mock-switch-parent")
    registry = _registry(backend)
    job_id = _confirmed_mod_job(conn, inbox_dir, {"a.bin": b"x"})
    aborted = {"now": False}
    real_verify = manifest_mod.verify_manifest_against_source

    def slow_verify(*args, **kwargs):
        aborted["now"] = True  # pressed while the sources were being re-hashed
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(manifest_mod, "verify_manifest_against_source", slow_verify)

    outcome = queue_worker.run_worker_once(conn, registry, should_abort=lambda: aborted["now"])

    assert outcome.status == "FAILED"
    job = db.get_job(conn, job_id)
    assert job["status"] == "FAILED" and job["abandoned"]
    assert job["attempt_count"] == 0  # never went RUNNING
    assert _history(conn, job_id) == []
    assert backend.storage_tree("SD_CARD").list_files() == []


# ---------------------------------------------------------------------------
# RealMtpBackend: an abort is not a transport fault
# ---------------------------------------------------------------------------


class _AbortingSession:
    """A WPD session whose write is stopped by the caller's own progress
    callback partway through."""

    def __init__(self):
        self.sends = 0
        self.closed = False

    def navigate(self, _root, _path, *, create_missing):
        return "parent"

    def child_id(self, _parent, _name):
        return None

    def send_file(self, _parent, _filename, source_path, *, progress=None, remember=False):
        self.sends += 1
        size = source_path.stat().st_size
        progress(size // 2, size)
        raise AssertionError("the transfer went on after the progress callback aborted it")


@pytest.mark.parametrize("storage", ["SD_CARD", "SD_INSTALL"])
def test_a_wpd_abort_is_neither_retried_nor_sent_through_the_shell(tmp_path, monkeypatch, storage):
    from switchagent.mtp.windows import RealMtpBackend

    backend = RealMtpBackend("::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\dev")
    session = _AbortingSession()
    sessions = [session]
    monkeypatch.setattr(backend, "_require_connected", lambda: None)
    monkeypatch.setattr(backend, "_wpd_session", lambda: sessions[0] if sessions else None)
    monkeypatch.setattr(backend, "_wpd_storage_id", lambda _s, _storage: "s1")
    closed = []
    monkeypatch.setattr(backend, "_close_wpd", lambda: (closed.append(True), sessions.clear()))

    def _no_shell(*_args, **_kwargs):
        raise AssertionError("an aborted file was handed to the Shell copy engine")

    monkeypatch.setattr(backend, "_send_via_shell", _no_shell)
    source = tmp_path / "game.nsp"
    source.write_bytes(b"x" * 4096)

    def progress(sent, total):
        raise TransferAborted("user pressed Abort")

    with pytest.raises(TransferAborted):
        backend.send_file(storage, "game.nsp", source, progress=progress)

    assert session.sends == 1
    assert closed == [True]  # the uncommitted object's session is not reused
    assert backend._wpd_unavailable is False  # the next install still goes over WPD


def test_a_wpd_session_never_commits_an_aborted_object(tmp_path, monkeypatch):
    committed, released = [], []
    monkeypatch.setattr(wpd, "create_file_object", lambda *_a: ("stream", 4096))
    monkeypatch.setattr(wpd, "stream_write", lambda _s, _b, length: length)
    monkeypatch.setattr(wpd, "stream_commit", lambda s: committed.append(s))
    monkeypatch.setattr(wpd, "release", lambda p: released.append(p))
    session = wpd.WpdSession("pnp")
    source = tmp_path / "game.nsp"
    source.write_bytes(b"y" * 10000)

    def progress(sent, total):
        raise TransferAborted("user pressed Abort")

    with pytest.raises(TransferAborted):
        session.send_file("parent", "game.nsp", source, progress=progress, progress_interval_seconds=0)

    assert committed == []
    assert released == ["stream"]


# ---------------------------------------------------------------------------
# web: POST /api/queue/abort, GET /api/worker/status
# ---------------------------------------------------------------------------


@pytest.fixture
def web_ctx(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    db_path = tmp_path / "test.db"
    with db.open_db(db_path) as conn:
        pass
    ctx = build_mock_context(db_path)
    with db.open_db(db_path) as conn:
        ctx.refresh_devices(conn)
    yield ctx


@pytest.fixture
def client(web_ctx):
    return TestClient(create_app(web_ctx))


def _seed_game(web_ctx, name, title_id):
    path = config.LIBRARY_DIR / name
    path.write_bytes(b"payload " + name.encode())
    with db.open_db(web_ctx.db_path) as conn:
        return db.upsert_library_item(
            conn, absolute_path=str(path), item_type="FILE", file_type="NSP",
            size=path.stat().st_size, mtime=path.stat().st_mtime, content_hash=f"h-{name}",
            title_id=title_id, title_id_source="filename", status="AVAILABLE",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        )


def _queue_game(client, web_ctx, name, title_id):
    item_id = _seed_game(web_ctx, name, title_id)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    return res.json()["created"][0]["job_id"]


def test_abort_is_offered_only_when_there_is_something_to_abort(client, web_ctx):
    status = client.get("/api/worker/status").json()
    assert status["abortable"] is False and status["stopping"] is False

    _queue_game(client, web_ctx, "Game [0100000000010000][v0].nsp", "0100000000010000")
    status = client.get("/api/worker/status").json()
    assert status["abortable"] is True and status["queued"] == 1


def test_abort_cancels_every_queued_job_and_leaves_stopped_ones_alone(client, web_ctx):
    first = _queue_game(client, web_ctx, "A [0100000000010000][v0].nsp", "0100000000010000")
    second = _queue_game(client, web_ctx, "B [0100000000020000][v0].nsp", "0100000000020000")
    failed = _queue_game(client, web_ctx, "C [0100000000030000][v0].nsp", "0100000000030000")
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, second, "WAITING_FOR_DEVICE")
        db.update_job_status(conn, failed, "FAILED", error="something earlier")

    res = client.post("/api/queue/abort")

    assert res.status_code == 200
    body = res.json()
    assert sorted(body["cancelled_job_ids"]) == sorted([first, second])
    assert body["stopping_job_ids"] == []
    for job_id in (first, second):
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "FAILED" and job["abandoned"]
    kept = client.get(f"/api/jobs/{failed}").json()
    assert kept["error"] == "something earlier" and not kept["abandoned"]  # its Retry still stands
    assert client.get("/api/worker/status").json()["abortable"] is False


def test_abort_never_pauses_the_worker_and_new_installs_run(client, web_ctx):
    _queue_game(client, web_ctx, "A [0100000000010000][v0].nsp", "0100000000010000")
    client.post("/api/queue/abort")
    assert client.get("/api/worker/status").json()["worker_paused"] is False

    fresh = _queue_game(client, web_ctx, "B [0100000000020000][v0].nsp", "0100000000020000")
    # The worker's own next pass reads the Abort counter afresh.
    with db.open_db(web_ctx.db_path) as conn:
        generation = web_ctx._abort_generation
        outcome = queue_worker.run_worker_once(
            conn, web_ctx.registry, should_abort=web_ctx._abort_checker(generation),
        )
    assert outcome.job_id == fresh and outcome.status == "DONE"


def test_abort_leaves_pause_exactly_as_it_was(client, web_ctx):
    client.post("/api/worker/pause")
    client.post("/api/queue/abort")
    assert client.get("/api/worker/status").json()["worker_paused"] is True


def test_the_running_transfer_reads_as_stopping_until_its_pass_returns(client, web_ctx):
    """The worker pass under way when Abort is pressed is the one that
    stops; the page must say "stopping" for exactly as long as it has not."""
    job_id = _queue_game(client, web_ctx, "A [0100000000010000][v0].nsp", "0100000000010000")
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "RUNNING")
    # What _worker_loop does around run_worker_once:
    generation = web_ctx._abort_generation
    web_ctx._worker_pass_generation = generation
    checker = web_ctx._abort_checker(generation)
    assert checker() is False

    body = client.post("/api/queue/abort").json()

    assert body["stopping_job_ids"] == [job_id]
    assert checker() is True
    status = client.get("/api/worker/status").json()
    assert status["stopping"] is True and status["stopping_seconds"] is not None
    assert status["abortable"] is True  # still something running

    web_ctx._worker_pass_generation = None  # the pass returned
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "INTERRUPTED", error="stopped by Abort")
    status = client.get("/api/worker/status").json()
    assert status["stopping"] is False and status["abortable"] is False
    # A later pass is not affected by the earlier Abort.
    assert web_ctx._abort_checker(web_ctx._abort_generation)() is False


def test_a_running_install_aborted_mid_file_can_be_retried_to_completion(client, web_ctx):
    job_id = _queue_game(client, web_ctx, "A [0100000000010000][v0].nsp", "0100000000010000")
    backend = web_ctx.registry.get("mock-switch-parent")
    real_send = backend.send_file
    generation = web_ctx._abort_generation
    checker = web_ctx._abort_checker(generation)

    def send_with_abort(storage, dest_path, source_path, *, progress=None, **kwargs):
        with db.open_db(web_ctx.db_path) as conn:
            services.abort_all(conn, web_ctx)  # the user presses Abort mid-file
        progress(1, source_path.stat().st_size)
        return real_send(storage, dest_path, source_path, progress=progress, **kwargs)

    backend.send_file = send_with_abort
    with db.open_db(web_ctx.db_path) as conn:
        outcome = queue_worker.run_worker_once(conn, web_ctx.registry, should_abort=checker)
    assert outcome.status == "INTERRUPTED"
    assert "DBI received only part of it" in outcome.error
    assert backend.storage_tree("SD_INSTALL").list_files() == []

    backend.send_file = real_send
    retry = client.post(f"/api/jobs/{job_id}/retry").json()
    with db.open_db(web_ctx.db_path) as conn:
        outcome = queue_worker.run_worker_once(
            conn, web_ctx.registry, should_abort=web_ctx._abort_checker(web_ctx._abort_generation),
        )
    assert outcome.job_id == retry["new_job_id"] and outcome.status == "DONE"


def test_queue_page_renders_the_abort_control(client, web_ctx):
    html = client.get("/queue").text
    assert 'id="abort-btn"' in html
    assert 'data-worker-action="pause"' in html


# ---------------------------------------------------------------------------
# preparation runs
# ---------------------------------------------------------------------------


def test_abort_all_stops_only_runs_still_in_progress(tmp_path):
    queue = PreparationQueue(tmp_path / "test.db")
    queue.states = {
        "running": {"phase": "Preparing", "items": {}, "target": "mock"},
        "waiting": {"phase": "Waiting", "items": {}, "target": "mock"},
        "done": {"phase": "Ready", "items": {}, "target": "mock"},
        "failed": {"phase": "Failed", "items": {}, "target": "mock"},
    }
    assert queue.has_running() is True
    assert queue.abort_all() == 2
    assert set(queue.states) == {"done", "failed"}
    assert queue.has_running() is False


def test_an_item_prepared_while_abort_was_pressed_is_never_confirmed(tmp_path, monkeypatch):
    """Preparing an item (extracting an archive) can take minutes. If Abort
    lands in the middle of it, the jobs it produced must be released, not
    handed to the worker once extraction finishes."""
    from switchagent import work_cleanup

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    queue = PreparationQueue(tmp_path / "test.db")
    confirmed, installed, cleaned = [], [], []

    def prepare(conn, ids, target, progress=None, confirm=True):
        queue.abort_all()  # pressed while this item was being prepared
        return {"created": [{"job_id": 41}], "errors": [], "batch_id": 7}

    monkeypatch.setattr(services, "create_and_confirm_jobs", prepare)
    monkeypatch.setattr(db, "confirm_job", lambda conn, job_id: confirmed.append(job_id))
    monkeypatch.setattr(db, "list_jobs_by_batch", lambda conn, batch: installed.append(batch) or [])
    monkeypatch.setattr(db, "get_library_item_by_id", lambda conn, item_id: None)
    monkeypatch.setattr(work_cleanup, "cleanup_batch_if_all_done", lambda conn, batch: cleaned.append(batch))
    conn = mock.MagicMock()
    queue.states["task"] = {"phase": "Preparing", "target": "mock", "items": {}}

    with pytest.raises(RuntimeError, match="Aborted by user"):
        queue._install_sequentially(conn, [1, 2], "mock", lambda **kw: None, "task", {"chain": [1, 2]})

    assert confirmed == [] and installed == []
    sql, params = conn.execute.call_args[0]
    assert "abandoned=1" in sql and params[1] == 7
    assert cleaned == [7]
