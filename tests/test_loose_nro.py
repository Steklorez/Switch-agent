"""A lone .nro in the library: an SD_FILES item of one file, copied to
switch/<name>/<name>.nro and named after the app's own NACP."""

from __future__ import annotations

import struct
from pathlib import Path

from switchagent import config, db, nro, preview, queue_worker, scanner, transfer
from switchagent.model import ContentType
from switchagent.mtp import MockMtpBackend
from switchagent.web import services

from .conftest import build_zip


def make_nro(name: str = "", author: str = "", version: str = "", *, with_assets: bool = True) -> bytes:
    """A minimal but well-formed NRO: header and (by default) an ASET
    section carrying a NACP."""
    body_size = 0x100
    header = bytearray(body_size)
    header[0x10:0x14] = b"NRO0"
    struct.pack_into("<I", header, 0x18, body_size)
    if not with_assets:
        return bytes(header)
    nacp = bytearray(0x4000)
    for i in range(16):
        nacp[i * 0x300:i * 0x300 + len(name)] = name.encode()
        nacp[i * 0x300 + 0x200:i * 0x300 + 0x200 + len(author)] = author.encode()
    nacp[0x3060:0x3060 + len(version)] = version.encode()
    aset = bytearray(0x38)
    aset[0:4] = b"ASET"
    struct.pack_into("<QQ", aset, 0x18, 0x38, len(nacp))
    return bytes(header) + bytes(aset) + bytes(nacp)


def _backend():
    b = MockMtpBackend(device_id="mock-switch")
    b.add_storage("SD_CARD")
    b.add_storage("SD_INSTALL")
    b.connect()
    return b


def _scan(conn, monkeypatch):
    monkeypatch.setattr(scanner, "is_file_stable", lambda _p: True)
    scanner.scan_library_once(conn)


# -- metadata ---------------------------------------------------------------

def test_read_nro_info_reads_nacp_name_author_version(tmp_path):
    path = tmp_path / "app.nro"
    path.write_bytes(make_nro("Checkpoint", "Bernardo Giordano", "3.8.0"))
    assert nro.read_nro_info(path) == nro.NroInfo("Checkpoint", "Bernardo Giordano", "3.8.0")


def test_read_nro_info_without_assets_is_still_an_nro(tmp_path):
    path = tmp_path / "app.nro"
    path.write_bytes(make_nro(with_assets=False))
    assert nro.read_nro_info(path) == nro.NroInfo(None, None, None)


def test_read_nro_info_rejects_a_non_nro(tmp_path):
    path = tmp_path / "fake.nro"
    path.write_bytes(b"not an nro at all" * 10)
    assert nro.read_nro_info(path) is None


# -- scanning ---------------------------------------------------------------

def test_lone_nro_is_indexed_as_sd_files_named_after_the_app(isolated_db, monkeypatch):
    conn, _ = isolated_db
    path = config.LIBRARY_DIR / "checkpoint-release.nro"
    path.write_bytes(make_nro("Checkpoint", "BG", "3.8.0"))

    _scan(conn, monkeypatch)

    row = db.get_library_item(conn, str(path))
    assert row["status"] == "AVAILABLE"
    assert row["content_type"] == ContentType.SD_FILES.value
    assert row["suggested_target"] == "SD_CARD"
    names = [g["name"] for g in services.list_library_view(conn)["games"]]
    assert names == ["Checkpoint 3.8.0"]


def test_nro_without_metadata_is_named_after_its_file(isolated_db, monkeypatch):
    conn, _ = isolated_db
    (config.LIBRARY_DIR / "tool.nro").write_bytes(make_nro(with_assets=False))
    _scan(conn, monkeypatch)
    assert [g["name"] for g in services.list_library_view(conn)["games"]] == ["tool"]


def test_fake_nro_needs_review(isolated_db, monkeypatch):
    conn, _ = isolated_db
    path = config.LIBRARY_DIR / "fake.nro"
    path.write_bytes(b"garbage" * 20)
    _scan(conn, monkeypatch)
    assert db.get_library_item(conn, str(path))["status"] == "NEEDS_REVIEW"


def test_nro_inside_a_switch_folder_stays_part_of_that_folder(isolated_db, monkeypatch):
    conn, _ = isolated_db
    app = config.LIBRARY_DIR / "Release" / "switch" / "App"
    app.mkdir(parents=True)
    (app / "App.nro").write_bytes(make_nro("App"))
    (app / "data.bin").write_bytes(b"data")

    _scan(conn, monkeypatch)

    rows = [r for r in db.list_library_items(conn) if r["status"] != db.LIBRARY_ITEM_RETIRED]
    assert [(Path(r["absolute_path"]).name, r["content_type"]) for r in rows] == [
        ("switch", ContentType.SD_FILES.value),
    ]


# -- archives holding just an .nro ------------------------------------------

def test_zip_with_a_loose_nro_is_installable(isolated_db, monkeypatch):
    conn, _ = isolated_db
    path = build_zip(config.LIBRARY_DIR / "podracer-switch.zip", {"podracer.nro": make_nro("Podracer")})
    _scan(conn, monkeypatch)
    row = db.get_library_item(conn, str(path))
    assert row["status"] == "AVAILABLE"
    assert row["content_type"] == ContentType.SD_FILES.value


def test_zip_with_two_nro_of_the_same_name_is_not_guessed(tmp_path):
    path = build_zip(tmp_path / "two.zip", {"a/app.nro": make_nro(), "b/app.nro": make_nro()})
    assert scanner.classify_file(path)["content_type"] == ContentType.UNKNOWN.value


def test_zip_with_a_loose_nro_transfers_into_its_own_switch_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    path = build_zip(tmp_path / "podracer-switch.zip", {
        "podracer-1.0/podracer.nro": make_nro("Podracer"),
        "podracer-1.0/README.md": b"readme",
    })

    report = preview.preview_path(path, extract=True)
    backend = _backend()
    outcome = transfer.transfer_report(backend, report)

    assert outcome.ok is True
    assert backend.storage_tree("SD_CARD").list_files() == ["switch/podracer/podracer.nro"]


def test_queued_zip_with_a_loose_nro_lands_on_the_sd_card(isolated_db, monkeypatch):
    conn, _ = isolated_db
    path = build_zip(config.LIBRARY_DIR / "podracer-switch.zip", {"podracer.nro": make_nro("Podracer")})
    _scan(conn, monkeypatch)
    row = db.get_library_item(conn, str(path))

    result = services.create_and_confirm_jobs(conn, [row["id"]], "mock-switch")
    assert not result.get("errors"), result
    backend = _backend()
    registry = queue_worker.DeviceRegistry()
    registry.register("mock-switch", backend)

    assert queue_worker.run_worker_once(conn, registry).status == "DONE"
    assert backend.storage_tree("SD_CARD").list_files() == ["switch/podracer/podracer.nro"]


# -- delivery ---------------------------------------------------------------

def test_lone_nro_transfers_into_its_own_switch_folder(tmp_path):
    path = tmp_path / "DBI.nro"
    path.write_bytes(make_nro("DBI"))

    report = preview.preview_path(path)
    assert report.content_type is ContentType.SD_FILES
    backend = _backend()
    outcome = transfer.transfer_report(backend, report)

    assert outcome.ok is True
    assert backend.storage_tree("SD_CARD").list_files() == ["switch/DBI/DBI.nro"]


def test_queued_nro_lands_on_the_sd_card(isolated_db, monkeypatch):
    conn, _ = isolated_db
    path = config.LIBRARY_DIR / "Goldleaf.nro"
    path.write_bytes(make_nro("Goldleaf", "XorTroll", "1.0.0"))
    _scan(conn, monkeypatch)
    row = db.get_library_item(conn, str(path))

    job_id = queue_worker.create_job_from_report(
        conn, preview.preview_path(path), library_item_id=row["id"], action="COPY_MERGE",
        target_device_id="mock-switch",
    )
    db.confirm_job(conn, job_id)
    backend = _backend()
    registry = queue_worker.DeviceRegistry()
    registry.register("mock-switch", backend)

    assert queue_worker.run_worker_once(conn, registry).status == "DONE"
    assert backend.storage_tree("SD_CARD").list_files() == ["switch/Goldleaf/Goldleaf.nro"]
    assert queue_worker.display_name_for_job(conn, db.get_job(conn, job_id)).startswith("Goldleaf 1.0.0")


def test_nro_already_on_the_card_is_a_conflict_not_an_overwrite(isolated_db, monkeypatch):
    conn, _ = isolated_db
    path = config.LIBRARY_DIR / "Goldleaf.nro"
    path.write_bytes(make_nro("Goldleaf"))
    _scan(conn, monkeypatch)
    row = db.get_library_item(conn, str(path))
    backend = _backend()
    backend.ensure_directory("SD_CARD", "switch/Goldleaf")
    backend.storage_tree("SD_CARD").write_file("switch/Goldleaf/Goldleaf.nro", b"older version")

    job_id = queue_worker.create_job_from_report(
        conn, preview.preview_path(path), library_item_id=row["id"], action="COPY_MERGE",
        target_device_id="mock-switch",
    )
    db.confirm_job(conn, job_id)
    registry = queue_worker.DeviceRegistry()
    registry.register("mock-switch", backend)

    assert queue_worker.run_worker_once(conn, registry).status == "DESTINATION_CONFLICT"
    assert backend.storage_tree("SD_CARD").read_file("switch/Goldleaf/Goldleaf.nro") == b"older version"
