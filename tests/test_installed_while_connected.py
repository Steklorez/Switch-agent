""""Installed" is only said while the console it went to is connected, and
for SD files a live look at the card wins over our own record."""

from __future__ import annotations

from switchagent import config, db, preview, queue_worker, scanner
from switchagent.mtp import MockMtpBackend
from switchagent.web import services
from switchagent.web.context import check_sd_files

from .test_loose_nro import make_nro

DEVICE = "mock-switch"


def _installed_nro(isolated_db, monkeypatch):
    conn, _ = isolated_db
    monkeypatch.setattr(scanner, "is_file_stable", lambda _p: True)
    path = config.LIBRARY_DIR / "PodRacer.nro"
    path.write_bytes(make_nro("Pod Racer"))
    scanner.scan_library_once(conn)
    row = db.get_library_item(conn, str(path))
    job_id = queue_worker.create_job_from_report(
        conn, preview.preview_path(path), library_item_id=row["id"], action="COPY_MERGE", target_device_id=DEVICE,
    )
    db.confirm_job(conn, job_id)
    backend = MockMtpBackend(device_id=DEVICE)
    backend.add_storage("SD_CARD")
    backend.add_storage("SD_INSTALL")
    registry = queue_worker.DeviceRegistry()
    registry.register(DEVICE, backend)
    assert queue_worker.run_worker_once(conn, registry).status == "DONE"
    return conn, backend, row["id"]


def _entry(conn, item_id, **kwargs):
    return next(e for e in services.list_library(conn, **kwargs) if e["id"] == item_id)


def test_installed_shows_while_its_console_is_connected(isolated_db, monkeypatch):
    conn, _backend, item_id = _installed_nro(isolated_db, monkeypatch)
    entry = _entry(conn, item_id, connected_device_ids={DEVICE})
    assert entry["status"] == "INSTALLED"
    assert not entry["hide_unverified_badge"]
    assert entry["recent_transfer_until"]


def test_nothing_is_said_once_that_console_is_unplugged(isolated_db, monkeypatch):
    conn, _backend, item_id = _installed_nro(isolated_db, monkeypatch)
    entry = _entry(conn, item_id, connected_device_ids=set())
    assert entry["hide_unverified_badge"]
    assert entry["recent_transfer_until"] is None
    game = services.list_library_view(conn, connected_device_ids=set())["games"][0]
    assert game["installed"] is False


def test_another_console_being_connected_does_not_count(isolated_db, monkeypatch):
    conn, _backend, item_id = _installed_nro(isolated_db, monkeypatch)
    assert _entry(conn, item_id, connected_device_ids={"some-other-switch"})["hide_unverified_badge"]


def test_without_connection_information_nothing_changes(isolated_db, monkeypatch):
    conn, _backend, item_id = _installed_nro(isolated_db, monkeypatch)
    assert not _entry(conn, item_id)["hide_unverified_badge"]


def test_sd_files_gone_from_the_card_are_not_called_installed(isolated_db, monkeypatch):
    conn, _backend, item_id = _installed_nro(isolated_db, monkeypatch)
    entry = _entry(conn, item_id, connected_device_ids={DEVICE}, sd_presence={item_id: False})
    assert entry["hide_unverified_badge"]
    assert not entry["confirmed_on_device"]


def test_sd_files_found_on_the_card_are_on_switch_whoever_put_them_there(isolated_db, monkeypatch):
    conn, _ = isolated_db
    monkeypatch.setattr(scanner, "is_file_stable", lambda _p: True)
    path = config.LIBRARY_DIR / "MyTool.nro"
    path.write_bytes(make_nro("My Tool"))
    scanner.scan_library_once(conn)
    item_id = db.get_library_item(conn, str(path))["id"]

    entry = _entry(conn, item_id, connected_device_ids={DEVICE}, sd_presence={item_id: True})

    assert entry["confirmed_on_device"]


def test_check_sd_files_looks_at_the_card(isolated_db, monkeypatch):
    conn, backend, item_id = _installed_nro(isolated_db, monkeypatch)
    assert check_sd_files(conn, backend) == {item_id: True}

    backend.storage_tree("SD_CARD").nodes.pop("switch/PodRacer/PodRacer.nro")
    assert check_sd_files(conn, backend) == {item_id: False}


def test_check_sd_files_stops_quietly_on_a_console_that_does_not_answer(isolated_db, monkeypatch):
    conn, backend, _item_id = _installed_nro(isolated_db, monkeypatch)

    def silent(*_args, **_kwargs):
        raise RuntimeError("no answer")

    monkeypatch.setattr(backend, "exists", silent)
    assert check_sd_files(conn, backend) == {}
