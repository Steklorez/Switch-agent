"""Integration tests: existing pipeline (scanner/extractor/preview) wired to
an abstract MtpBackend via transfer.py, running against MockMtpBackend.
Covers the "existing pipeline -> mock MTP -> files land in expected
destination" scenario end to end.
"""

from __future__ import annotations

from switchagent import preview, transfer
from switchagent.model import ContentType
from switchagent.mtp import MockMtpBackend, TransferStatus

from .conftest import build_7z, build_zip


def _backend():
    b = MockMtpBackend()
    b.add_storage("SD_CARD")
    b.add_storage("SD_INSTALL")
    b.connect()
    return b


# 13. Full pipeline integration ---------------------------------------------

def test_bare_nsp_file_flows_through_preview_and_transfer_to_sd_install(tmp_path):
    pkg = tmp_path / "Game [0100000000010000][v0].nsp"
    pkg.write_bytes(b"real nsp payload")

    report = preview.preview_path(pkg)
    assert report.content_type is ContentType.GAME_PACKAGE

    backend = _backend()
    outcome = transfer.transfer_report(backend, report)

    assert outcome.ok is True
    assert outcome.storage == "SD_INSTALL"
    assert backend.storage_tree("SD_INSTALL").list_files() == ["Game [0100000000010000][v0].nsp"]
    assert backend.storage_tree("SD_INSTALL").read_file("Game [0100000000010000][v0].nsp") == b"real nsp payload"


def test_zip_with_nsp_extracts_then_transfers_to_sd_install(tmp_path, monkeypatch):
    from switchagent import config
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")

    path = build_zip(tmp_path / "pack.zip", {"Game2 [0100000000020000][v0].nsp": b"zip nsp payload"})
    report = preview.preview_path(path, extract=True)
    assert report.content_type is ContentType.GAME_PACKAGE
    assert report.work_dir is not None

    backend = _backend()
    outcome = transfer.transfer_report(backend, report)

    assert outcome.ok is True
    assert backend.storage_tree("SD_INSTALL").read_file("Game2 [0100000000020000][v0].nsp") == b"zip nsp payload"


def test_7z_atmosphere_mod_extracts_then_transfers_to_sd_card(tmp_path, monkeypatch):
    from switchagent import config
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "MOCK_SD_CARD_DIR", tmp_path / "mock_sd")

    path = build_7z(tmp_path / "mod.7z", {
        "atmosphere/contents/0100000000030000/romfs/a.bin": b"romfs payload",
        "atmosphere/contents/0100000000030000/exefs/b.bin": b"exefs payload",
    })
    report = preview.preview_path(path, extract=True)
    assert report.content_type is ContentType.ATMOSPHERE_MOD
    assert report.mod_source_dir is not None

    backend = _backend()
    outcome = transfer.transfer_report(backend, report)

    assert outcome.ok is True
    assert outcome.files_sent == 2
    files = backend.storage_tree("SD_CARD").list_files()
    assert "atmosphere/contents/0100000000030000/romfs/a.bin" in files
    assert "atmosphere/contents/0100000000030000/exefs/b.bin" in files
    assert backend.storage_tree("SD_CARD").read_file(
        "atmosphere/contents/0100000000030000/romfs/a.bin"
    ) == b"romfs payload"


def test_atmosphere_mod_folder_directly_from_inbox_transfers_without_extraction(tmp_path):
    mod_dir = tmp_path / "atmosphere" / "contents" / "0100000000040000"
    (mod_dir / "romfs").mkdir(parents=True)
    (mod_dir / "romfs" / "text.bin").write_bytes(b"mod text")

    report = preview.preview_path(mod_dir)
    assert report.content_type is ContentType.ATMOSPHERE_MOD
    assert report.mod_source_dir == mod_dir

    backend = _backend()
    outcome = transfer.transfer_report(backend, report)
    assert outcome.ok is True
    assert backend.storage_tree("SD_CARD").read_file(
        "atmosphere/contents/0100000000040000/romfs/text.bin"
    ) == b"mod text"


# -- transfer.py's own guardrails, not just the happy path -------------

def test_mixed_content_is_never_transferred(tmp_path):
    path = build_zip(tmp_path / "mixed.zip", {
        "game.nsp": b"x",
        "atmosphere/contents/0100000000050000/romfs/y.bin": b"y",
    })
    report = preview.preview_path(path)
    assert report.content_type is ContentType.MIXED

    backend = _backend()
    outcome = transfer.transfer_report(backend, report)
    assert outcome.ok is False
    assert "NEEDS_REVIEW" in outcome.error
    assert backend.storage_tree("SD_CARD").list_files() == []
    assert backend.storage_tree("SD_INSTALL").list_files() == []


def test_unextracted_archive_package_refuses_to_transfer(tmp_path):
    """preview_path() without extract=True never stages a local file --
    transfer.py must say so clearly rather than pretend there's nothing to
    send, or silently extract as a side effect."""
    path = build_zip(tmp_path / "pack.zip", {"Game [0100000000010000][v0].nsp": b"x"})
    report = preview.preview_path(path, extract=False)
    assert report.work_dir is None

    backend = _backend()
    outcome = transfer.transfer_report(backend, report)
    assert outcome.ok is False
    assert outcome.error  # must explain itself, not just silently do nothing
    # must NOT have sent the zip container itself as if it were the package
    assert backend.storage_tree("SD_INSTALL").list_files() == []


def test_unextracted_archive_mod_refuses_to_transfer(tmp_path):
    path = build_zip(tmp_path / "mod.zip", {
        "atmosphere/contents/0100000000060000/romfs/x.bin": b"x",
    })
    report = preview.preview_path(path, extract=False)
    assert report.mod_source_dir is None

    backend = _backend()
    outcome = transfer.transfer_report(backend, report)
    assert outcome.ok is False
    assert "not staged" in outcome.error


def test_transfer_stops_at_first_failure_and_reports_partial_progress(tmp_path):
    mod_dir = tmp_path / "atmosphere" / "contents" / "0100000000070000" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "a_first.bin").write_bytes(b"a")
    (mod_dir / "b_second.bin").write_bytes(b"b")

    report = preview.preview_path(mod_dir.parent)
    assert report.content_type is ContentType.ATMOSPHERE_MOD

    backend = _backend()
    backend.arm_failure("error", dest_path="atmosphere/contents/0100000000070000/romfs/a_first.bin")
    outcome = transfer.transfer_report(backend, report)

    assert outcome.ok is False
    assert outcome.files_sent == 0
    assert outcome.files_total == 2
    # must not have gone on to attempt b_second.bin after a_first.bin failed
    assert backend.storage_tree("SD_CARD").list_files() == []


def test_reported_status_never_claims_success_on_disconnect_mid_batch(tmp_path):
    mod_dir = tmp_path / "atmosphere" / "contents" / "0100000000080000" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "a.bin").write_bytes(b"a" * 10)
    (mod_dir / "b.bin").write_bytes(b"b" * 10)

    report = preview.preview_path(mod_dir.parent)
    backend = _backend()
    backend.arm_failure("disconnect", dest_path="atmosphere/contents/0100000000080000/romfs/a.bin")
    outcome = transfer.transfer_report(backend, report)

    assert outcome.ok is False
    assert outcome.files_sent == 0
    assert backend.is_connected is False
