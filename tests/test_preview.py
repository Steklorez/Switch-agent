from switchagent import config, preview
from switchagent.model import ContentType

from .conftest import build_7z, build_zip


def test_preview_package_file(tmp_path):
    path = tmp_path / "Game [0100000000010000][v0].nsp"
    path.write_bytes(b"nsp bytes")
    report = preview.preview_path(path)
    assert report.content_type is ContentType.GAME_PACKAGE
    assert report.title_id == "0100000000010000"
    assert report.destination == "SD install"
    assert report.mode == "INSTALL"


def test_preview_package_file_without_title_id_flags_needs_review(tmp_path):
    path = tmp_path / "Mystery.nsp"
    path.write_bytes(b"nsp bytes")
    report = preview.preview_path(path)
    assert report.title_id is None
    assert "NEEDS_REVIEW" in report.note


def test_preview_atmosphere_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MOCK_SD_CARD_DIR", tmp_path / "mock_sd")
    mod_dir = tmp_path / "inbox" / "atmosphere" / "contents" / "0100000000010000"
    (mod_dir / "romfs").mkdir(parents=True)
    (mod_dir / "romfs" / "asset.bin").write_bytes(b"data")

    report = preview.preview_path(mod_dir)
    assert report.content_type is ContentType.ATMOSPHERE_MOD
    assert report.title_id == "0100000000010000"
    assert report.destination == "SD Card"
    assert report.mode == "MERGE"
    assert report.file_count == 1


def test_preview_mixed_zip_reports_needs_review(tmp_path):
    path = build_zip(tmp_path / "mixed.zip", {
        "game.nsp": b"nsp bytes",
        "atmosphere/contents/0100000000050000/romfs/x.bin": b"mod bytes",
    })
    report = preview.preview_path(path)
    assert report.content_type is ContentType.MIXED
    text = preview.format_report(report)
    assert "TYPE: MIXED" in text
    assert "ACTION: NEEDS_REVIEW" in text


def test_preview_archive_with_extract_flag_actually_extracts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MOCK_SD_CARD_DIR", tmp_path / "mock_sd")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    path = build_zip(tmp_path / "mod.zip", {
        "atmosphere/contents/0100000000040000/romfs/text.bin": b"modded text",
    })
    report = preview.preview_path(path, extract=True)
    assert report.job_id is not None
    assert report.work_dir.exists()
    extracted_file = report.work_dir / "atmosphere/contents/0100000000040000/romfs/text.bin"
    assert extracted_file.read_bytes() == b"modded text"


def test_preview_archive_with_multiple_packages_exposes_every_entry(tmp_path, monkeypatch):
    """The collection PreviewReport.package_entries must
    hold every installable entry (Base+Update+DLC), each pointing at its own
    real, extracted file -- not just the first, which is all
    package_relative_path/title_id (kept for single-package callers) ever
    described before this field existed."""
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    path = build_zip(tmp_path / "multi.zip", {
        "Game [0100AAAAAAAAA000][v0].nsz": b"base bytes",
        "Game Update [0100AAAAAAAAA800][v65536].nsz": b"update bytes bytes",
        "Game DLC1 [0100AAAAAAAAB001][v0].nsz": b"dlc bytes",
    })
    report = preview.preview_path(path, extract=True)
    assert report.content_type is ContentType.GAME_PACKAGE
    assert len(report.package_entries) == 3

    by_variant = {e.variant: e for e in report.package_entries}
    assert set(by_variant) == {"BASE", "UPDATE", "DLC"}
    assert by_variant["BASE"].title_id == "0100AAAAAAAAA000"
    assert by_variant["UPDATE"].title_id == "0100AAAAAAAAA800"
    assert by_variant["DLC"].title_id == "0100AAAAAAAAB001"

    for entry in report.package_entries:
        extracted = report.work_dir / entry.relative_path
        assert extracted.is_file()
        assert entry.size == extracted.stat().st_size  # real bytes, not a declared-size guess


def test_preview_archive_with_one_package_has_one_package_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    path = build_zip(tmp_path / "single.zip", {"Game [0100000000010000][v0].nsp": b"payload"})
    report = preview.preview_path(path, extract=True)
    assert len(report.package_entries) == 1
    assert report.package_entries[0].relative_path == report.package_relative_path
    assert report.package_entries[0].variant == "BASE"


def test_format_report_game_package_matches_expected_shape(tmp_path):
    path = tmp_path / "Game [0100000000010000][v0].nsp"
    path.write_bytes(b"x" * 2048)
    report = preview.preview_path(path)
    text = preview.format_report(report)
    assert "TYPE: GAME_PACKAGE" in text
    assert "FORMAT: NSP" in text
    assert "DESTINATION: SD install" in text


def test_preview_7z_package(tmp_path):
    path = build_7z(tmp_path / "pack.7z", {"Game [0100000000060000][v0].nsp": b"7z nsp bytes"})
    report = preview.preview_path(path)
    assert report.content_type is ContentType.GAME_PACKAGE
    assert report.package_format == "NSP"
    assert report.title_id == "0100000000060000"


def test_format_report_labels_atmosphere_conflicts_as_local_mock_only(tmp_path, monkeypatch):
    """ARCH-004 regression: the CLI's conflict report compares an
    Atmosphere mod against config.MOCK_SD_CARD_DIR (a local placeholder on
    THIS PC), never a real connected Switch -- the printed label must say
    so explicitly, or a real CONFLICT line reads exactly like a live check
    against the user's actual device."""
    mock_sd = tmp_path / "mock_sd"
    monkeypatch.setattr(config, "MOCK_SD_CARD_DIR", mock_sd)

    title_id = "0100000000070000"
    mod_dir = tmp_path / "inbox" / title_id
    (mod_dir / "romfs").mkdir(parents=True)
    (mod_dir / "romfs" / "asset.bin").write_bytes(b"new content")

    # Pre-seed the LOCAL mock destination with DIFFERENT bytes at the same
    # relative path -- guarantees a real ConflictState.CONFLICT, not just NEW.
    existing = mock_sd / "atmosphere" / "contents" / title_id / "romfs"
    existing.mkdir(parents=True)
    (existing / "asset.bin").write_bytes(b"different content already there")

    report = preview.preview_path(mod_dir)
    assert report.conflicts  # sanity: the scenario actually produced conflicts
    text = preview.format_report(report)

    assert "LOCAL PREVIEW CONFLICTS" in text
    assert "NOT your real Switch" in text
    assert str(mock_sd) in text
    # the old, bare/ambiguous label must be gone
    assert "\nCONFLICTS:" not in text
    assert not text.startswith("CONFLICTS:")
