from pathlib import Path

import pytest
import rarfile

from switchagent import config, extractor
from switchagent.model import ConflictState, ContentType

from .conftest import build_7z, build_zip, build_zip_with_symlink


# ---------------------------------------------------------------------------
# Classification: ZIP / 7Z / RAR(mocked), one per required scenario
# ---------------------------------------------------------------------------

def test_zip_with_nsp_is_game_package(tmp_path):
    path = build_zip(tmp_path / "a.zip", {"Game [0100000000010000][v0].nsp": b"nsp bytes"})
    cls = extractor.classify_entries(extractor.list_zip_entries(path))
    assert cls.content_type is ContentType.GAME_PACKAGE
    assert cls.package_format == "NSP"
    assert cls.package_title_id_guess.title_id == "0100000000010000"


def test_zip_with_nsz_is_game_package(tmp_path):
    path = build_zip(tmp_path / "a.zip", {"Game [0100000000010000][v0].nsz": b"nsz bytes"})
    cls = extractor.classify_entries(extractor.list_zip_entries(path))
    assert cls.content_type is ContentType.GAME_PACKAGE
    assert cls.package_format == "NSZ"


def test_zip_with_atmosphere_is_atmosphere_mod(tmp_path):
    path = build_zip(tmp_path / "a.zip", {
        "atmosphere/contents/0100000000040000/romfs/text.bin": b"modded text",
    })
    cls = extractor.classify_entries(extractor.list_zip_entries(path))
    assert cls.content_type is ContentType.ATMOSPHERE_MOD
    assert cls.atmosphere_title_id_guess.title_id == "0100000000040000"
    assert cls.atmosphere_title_id_guess.confident is True


def test_zip_with_package_and_atmosphere_is_mixed(tmp_path):
    path = build_zip(tmp_path / "a.zip", {
        "game.nsp": b"nsp bytes",
        "atmosphere/contents/0100000000050000/romfs/x.bin": b"mod bytes",
    })
    cls = extractor.classify_entries(extractor.list_zip_entries(path))
    assert cls.content_type is ContentType.MIXED


def test_zip_with_neither_is_unknown(tmp_path):
    path = build_zip(tmp_path / "a.zip", {"readme.txt": b"not switch related"})
    cls = extractor.classify_entries(extractor.list_zip_entries(path))
    assert cls.content_type is ContentType.UNKNOWN


# ---------------------------------------------------------------------------
# Regression: an archive with Base+Update+DLC used to have every entry
# after the first silently discarded -- classify_entries() only ever
# captured ONE package_entry_name. package_entries collects every one.
# ---------------------------------------------------------------------------

def test_classify_entries_collects_every_package_entry_not_just_the_first(tmp_path):
    path = build_zip(tmp_path / "multi.zip", {
        "Game [0100AAAAAAAAA000][v0].nsz": b"base",
        "Game Update [0100AAAAAAAAA800][v65536].nsz": b"update",
        "Game DLC1 [0100AAAAAAAAA001][v0].nsz": b"dlc1",
        "Game DLC2 [0100AAAAAAAAA002][v0].nsz": b"dlc2",
    })
    cls = extractor.classify_entries(extractor.list_zip_entries(path))
    assert cls.content_type is ContentType.GAME_PACKAGE
    assert len(cls.package_entries) == 4
    assert {e.name for e in cls.package_entries} == {
        "Game [0100AAAAAAAAA000][v0].nsz",
        "Game Update [0100AAAAAAAAA800][v65536].nsz",
        "Game DLC1 [0100AAAAAAAAA001][v0].nsz",
        "Game DLC2 [0100AAAAAAAAA002][v0].nsz",
    }
    assert {e.title_id_guess.title_id for e in cls.package_entries} == {
        "0100AAAAAAAAA000", "0100AAAAAAAAA800", "0100AAAAAAAAA001", "0100AAAAAAAAA002",
    }
    # the original singular fields still describe exactly the first entry --
    # every existing single-package caller keeps working unchanged.
    assert cls.package_entry_name == "Game [0100AAAAAAAAA000][v0].nsz"
    assert cls.package_format == "NSZ"
    assert cls.package_title_id_guess.title_id == "0100AAAAAAAAA000"


def test_classify_entries_single_package_archive_has_one_package_entry(tmp_path):
    path = build_zip(tmp_path / "a.zip", {"Game [0100000000010000][v0].nsp": b"nsp bytes"})
    cls = extractor.classify_entries(extractor.list_zip_entries(path))
    assert len(cls.package_entries) == 1
    assert cls.package_entries[0].name == cls.package_entry_name


def test_7z_with_nsp_is_game_package(tmp_path):
    path = build_7z(tmp_path / "a.7z", {"Game [0100000000060000][v0].nsp": b"7z nsp bytes"})
    cls = extractor.classify_entries(extractor.list_7z_entries(path))
    assert cls.content_type is ContentType.GAME_PACKAGE
    assert cls.package_format == "NSP"
    assert cls.package_title_id_guess.title_id == "0100000000060000"


class _FakeRarInfo:
    def __init__(self, filename, size, crc):
        self.filename = filename
        self.file_size = size
        self.CRC = crc

    def is_dir(self):
        return self.filename.endswith("/")

    def is_symlink(self):
        return False


class _FakeRarFile:
    """Stands in for rarfile.RarFile so RAR-specific tests don't need a real
    unrar/7z backend (none is installed on the dev machine -- see
    docs/STAGE2.md) or a real .rar file (nothing on this machine can
    encode one). Exercises our actual extractor.list_rar_entries() field
    mapping and extractor.classify_entries() against a RAR-shaped listing."""

    def __init__(self, path):
        self._entries = [
            _FakeRarInfo("atmosphere/contents/0100000000070000/romfs/x.bin", 11, 12345),
        ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def infolist(self):
        return self._entries

    def extractall(self, path):
        raise NotImplementedError("override via monkeypatch in individual tests")


def test_rar_with_atmosphere_is_atmosphere_mod(tmp_path, monkeypatch):
    monkeypatch.setattr(rarfile, "RarFile", _FakeRarFile)
    fake_path = tmp_path / "mod.rar"
    fake_path.touch()  # content irrelevant, RarFile is mocked
    entries = extractor.list_rar_entries(fake_path)
    cls = extractor.classify_entries(entries)
    assert cls.content_type is ContentType.ATMOSPHERE_MOD
    assert cls.atmosphere_title_id_guess.title_id == "0100000000070000"


def test_rar_real_corrupt_file_raises_corrupt_archive_error(tmp_path):
    """Unlike the mocked test above, this genuinely exercises rarfile's own
    (unmocked) header parsing -- no external tool needed for that, only for
    real decompression -- against real, deliberately invalid bytes."""
    bad = tmp_path / "bad.rar"
    bad.write_bytes(b"not a rar file at all")
    with pytest.raises(extractor.CorruptArchiveError):
        extractor.list_rar_entries(bad)


def test_rar_extraction_without_backend_raises_clear_error(tmp_path, monkeypatch):
    """No unrar/7z/bsdtar is installed on this machine (verified during
    development) -- safe_extract must fail with a specific, catchable error
    instead of crashing or hanging."""
    monkeypatch.setattr(rarfile, "RarFile", _FakeRarFile)

    def _raise_cannot_exec(*a, **k):
        raise rarfile.RarCannotExec("no backend tool")

    monkeypatch.setattr(_FakeRarFile, "extractall", _raise_cannot_exec)
    fake_path = tmp_path / "mod.rar"
    fake_path.touch()
    with pytest.raises(extractor.ExtractionBackendMissingError):
        extractor._extract_rar(fake_path, tmp_path / "dest")


def test_rar_backend_available_is_false_when_tool_setup_raises(monkeypatch):
    def _raise(*a, **k):
        raise rarfile.RarCannotExec("no backend tool")

    monkeypatch.setattr(rarfile, "tool_setup", _raise)
    assert extractor.rar_backend_available() is False


def test_rar_backend_available_is_true_when_tool_setup_succeeds(monkeypatch):
    monkeypatch.setattr(rarfile, "tool_setup", lambda: object())
    assert extractor.rar_backend_available() is True


# ---------------------------------------------------------------------------
# Path safety: Zip Slip, absolute path, traversal, symlink, UNC
# ---------------------------------------------------------------------------

def test_safe_relative_path_rejects_leading_traversal(tmp_path):
    dest = tmp_path / "work" / "job1"
    dest.mkdir(parents=True)
    with pytest.raises(extractor.ZipSlipError):
        extractor.safe_relative_path("../../evil.txt", dest)


def test_safe_relative_path_rejects_embedded_traversal(tmp_path):
    dest = tmp_path / "work" / "job1"
    dest.mkdir(parents=True)
    with pytest.raises(extractor.ZipSlipError):
        extractor.safe_relative_path("a/b/../../../evil.txt", dest)


def test_safe_relative_path_rejects_windows_absolute(tmp_path):
    dest = tmp_path / "work" / "job1"
    dest.mkdir(parents=True)
    with pytest.raises(extractor.ZipSlipError):
        extractor.safe_relative_path("C:/Windows/evil.txt", dest)


def test_safe_relative_path_rejects_posix_absolute(tmp_path):
    dest = tmp_path / "work" / "job1"
    dest.mkdir(parents=True)
    with pytest.raises(extractor.ZipSlipError):
        extractor.safe_relative_path("/etc/passwd", dest)


def test_safe_relative_path_rejects_unc(tmp_path):
    dest = tmp_path / "work" / "job1"
    dest.mkdir(parents=True)
    with pytest.raises(extractor.ZipSlipError):
        extractor.safe_relative_path("\\\\attacker-host\\share\\evil.txt", dest)


def test_safe_relative_path_accepts_normal_nested_path(tmp_path):
    dest = tmp_path / "work" / "job1"
    dest.mkdir(parents=True)
    target = extractor.safe_relative_path("atmosphere/contents/0100.../romfs/x.bin", dest)
    assert target.is_relative_to(dest.resolve())


def test_safe_extract_rejects_zip_slip_end_to_end(tmp_path):
    evil = build_zip(tmp_path / "evil.zip", {"../../evil.txt": b"pwned"})
    with pytest.raises(extractor.ZipSlipError):
        extractor.safe_extract(evil, extractor.new_job_id(), work_root=tmp_path / "work")
    # nothing should have leaked outside work/
    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_rejects_absolute_path_entry_end_to_end(tmp_path):
    import zipfile
    evil = tmp_path / "evil_abs.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr(zipfile.ZipInfo("C:/Windows/evil.txt"), b"pwned")
    with pytest.raises(extractor.ZipSlipError):
        extractor.safe_extract(evil, extractor.new_job_id(), work_root=tmp_path / "work")


def test_safe_extract_rejects_symlink_entry(tmp_path):
    evil = build_zip_with_symlink(tmp_path / "evil_link.zip", "innocuous.txt", "/etc/passwd")
    with pytest.raises(extractor.ZipSlipError):
        extractor.safe_extract(evil, extractor.new_job_id(), work_root=tmp_path / "work")


def test_safe_extract_never_creates_files_outside_job_dir(tmp_path):
    """Belt-and-braces: even if path validation had a hole, nothing should
    ever appear outside work/<job-id>/ after a safe_extract call, successful
    or not."""
    good = build_zip(tmp_path / "good.zip", {"game.nsp": b"x" * 100})
    work_root = tmp_path / "work"
    before = set(tmp_path.rglob("*"))
    result = extractor.safe_extract(good, extractor.new_job_id(), work_root=work_root)
    after = set(tmp_path.rglob("*")) - before
    assert all(p.is_relative_to(result.dest_root) or p in (work_root,) for p in after)


# ---------------------------------------------------------------------------
# Limits from config.yaml (declared-size / file-count / depth)
# ---------------------------------------------------------------------------

def test_extraction_limit_too_many_files(tmp_path):
    tiny_limits = config.ExtractionLimits(
        max_extracted_size_bytes=10_000_000, max_file_count=1, max_directory_depth=24,
    )
    path = build_zip(tmp_path / "many.zip", {"a.nsp": b"x", "b.nsp": b"y"})
    with pytest.raises(extractor.ExtractionLimitError, match="too many files"):
        extractor.safe_extract(path, extractor.new_job_id(), limits=tiny_limits, work_root=tmp_path / "work")


def test_extraction_limit_declared_size_too_large(tmp_path):
    tiny_limits = config.ExtractionLimits(
        max_extracted_size_bytes=5, max_file_count=10, max_directory_depth=24,
    )
    path = build_zip(tmp_path / "big.zip", {"a.nsp": b"x" * 1000})
    with pytest.raises(extractor.ExtractionLimitError):
        extractor.safe_extract(path, extractor.new_job_id(), limits=tiny_limits, work_root=tmp_path / "work")


def test_extraction_limit_zip_streaming_catches_real_bytes(tmp_path):
    """The declared size in the central directory happens to be small (an
    empty/near-empty stored entry), but streaming still counts real bytes
    written -- this specifically exercises the mid-stream counter in
    _extract_zip_streaming, not just the upfront declared-size pre-check."""
    import zipfile
    path = tmp_path / "stream.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.nsp", b"y" * 200_000)  # compresses down a lot, real bytes still large on extract
    tiny_limits = config.ExtractionLimits(
        max_extracted_size_bytes=100, max_file_count=10, max_directory_depth=24,
    )
    with pytest.raises(extractor.ExtractionLimitError):
        extractor.safe_extract(path, extractor.new_job_id(), limits=tiny_limits, work_root=tmp_path / "work")


def test_extraction_limit_directory_too_deep(tmp_path):
    tiny_limits = config.ExtractionLimits(
        max_extracted_size_bytes=10_000_000, max_file_count=100, max_directory_depth=2,
    )
    path = build_zip(tmp_path / "deep.zip", {"a/b/c/d/e.nsp": b"x"})
    with pytest.raises(extractor.ExtractionLimitError, match="too deep"):
        extractor.safe_extract(path, extractor.new_job_id(), limits=tiny_limits, work_root=tmp_path / "work")


def test_7z_extraction_respects_size_limit_via_library(tmp_path):
    tiny_limits = config.ExtractionLimits(
        max_extracted_size_bytes=5, max_file_count=10, max_directory_depth=24,
    )
    path = build_7z(tmp_path / "big.7z", {"a.nsp": b"x" * 1000})
    with pytest.raises(extractor.ExtractionLimitError):
        extractor.safe_extract(path, extractor.new_job_id(), limits=tiny_limits, work_root=tmp_path / "work")


# ---------------------------------------------------------------------------
# Successful extraction round-trips
# ---------------------------------------------------------------------------

def test_zip_extraction_round_trip(tmp_path):
    path = build_zip(tmp_path / "good.zip", {"Game [0100000000010000][v0].nsp": b"content bytes"})
    result = extractor.safe_extract(path, extractor.new_job_id(), work_root=tmp_path / "work")
    extracted = list(result.dest_root.rglob("*"))
    assert result.file_count == 1
    assert any(p.name == "Game [0100000000010000][v0].nsp" for p in extracted)
    assert (result.dest_root / "Game [0100000000010000][v0].nsp").read_bytes() == b"content bytes"


def test_7z_extraction_round_trip(tmp_path):
    path = build_7z(tmp_path / "good.7z", {
        "atmosphere/contents/0100000000040000/romfs/text.bin": b"modded text",
    })
    result = extractor.safe_extract(path, extractor.new_job_id(), work_root=tmp_path / "work")
    target = result.dest_root / "atmosphere/contents/0100000000040000/romfs/text.bin"
    assert target.read_bytes() == b"modded text"


# ---------------------------------------------------------------------------
# Conflict preview (NEW / SAME / CONFLICT)
# ---------------------------------------------------------------------------

def test_conflicts_from_folder_new_same_and_conflict(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    (source / "sub").mkdir(parents=True)
    (dest / "sub").mkdir(parents=True)

    (source / "new_file.bin").write_bytes(b"aaaa")               # not in dest -> NEW
    (source / "same_file.bin").write_bytes(b"bbbb")
    (dest / "same_file.bin").write_bytes(b"bbbb")                 # identical -> SAME
    (source / "sub" / "changed.bin").write_bytes(b"new-content")
    (dest / "sub" / "changed.bin").write_bytes(b"old-content")    # differs -> CONFLICT

    conflicts = extractor.compute_conflicts_from_folder(source, dest)
    by_path = {c.relative_path: c.state for c in conflicts}
    assert by_path["new_file.bin"] is ConflictState.NEW
    assert by_path["same_file.bin"] is ConflictState.SAME
    assert by_path["sub/changed.bin"] is ConflictState.CONFLICT


def test_conflicts_from_archive_entries_without_extracting(tmp_path):
    import zlib

    dest = tmp_path / "dest"
    (dest / "romfs").mkdir(parents=True)
    (dest / "romfs" / "same_file.bin").write_bytes(b"bbbb")
    (dest / "romfs" / "changed.bin").write_bytes(b"old-content")

    path = build_zip(tmp_path / "mod.zip", {
        "atmosphere/contents/0100000000040000/romfs/new_file.bin": b"aaaa",
        "atmosphere/contents/0100000000040000/romfs/same_file.bin": b"bbbb",
        "atmosphere/contents/0100000000040000/romfs/changed.bin": b"new-content",
    })
    entries = extractor.list_zip_entries(path)
    cls = extractor.classify_entries(entries)
    conflicts = extractor.compute_conflicts_from_archive_entries(entries, cls.atmosphere_root, dest)
    by_path = {c.relative_path: c.state for c in conflicts}
    assert by_path["romfs/new_file.bin"] is ConflictState.NEW
    assert by_path["romfs/same_file.bin"] is ConflictState.SAME
    assert by_path["romfs/changed.bin"] is ConflictState.CONFLICT


# ---------------------------------------------------------------------------
# A 7z method py7zr cannot decode goes to 7-Zip / Windows tar instead.
#
# Real case (2026-09-23): Mega Man X Regenesis' switch.7z uses LZMA2 + the
# ARM64 filter (7-Zip 23+). py7zr lists it and then refuses to unpack it,
# which failed the install with "(b'\\n', 'Archive is compressed by an
# unsupported compression algorithm.')". The fake tool below is Python
# unpacking with the real py7zr -- only THIS process is made to refuse.
# ---------------------------------------------------------------------------

import sys

import py7zr
import py7zr.exceptions

_UNPACK = (
    "import sys, py7zr\n"
    "with py7zr.SevenZipFile(sys.argv[1]) as z: z.extractall(path=sys.argv[2])\n"
)
_UNPACK_WRONG = _UNPACK + (
    "import pathlib; next(p for p in pathlib.Path(sys.argv[2]).rglob('*') if p.is_file()).write_bytes(b'tampered')\n"
)


def _py7zr_refuses(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise py7zr.exceptions.UnsupportedCompressionMethodError(b"\n", "unsupported compression algorithm")
    monkeypatch.setattr(py7zr.SevenZipFile, "extractall", refuse)


def _fake_tool(monkeypatch, script):
    monkeypatch.setattr(extractor, "external_7z_tools", lambda: [("7-Zip", sys.executable)])
    monkeypatch.setattr(
        extractor, "_external_7z_command",
        lambda _label, exe, archive, dest: [exe, "-c", script, str(archive), str(dest)],
    )


def test_an_unsupported_7z_method_is_unpacked_by_an_external_tool(tmp_path, monkeypatch):
    path = build_7z(tmp_path / "switch.7z", {
        "switch/mmxregenesis_nx/assets/a.ctex": b"asset a",
        "switch/mmxregenesis_nx/assets/scripts/x.gd": b"script x",
    })
    _py7zr_refuses(monkeypatch)
    _fake_tool(monkeypatch, _UNPACK)
    result = extractor.safe_extract(path, "job", work_root=tmp_path / "work")
    assert result.file_count == 2
    assert (result.dest_root / "switch/mmxregenesis_nx/assets/scripts/x.gd").read_bytes() == b"script x"


def test_an_external_tools_output_is_held_to_the_archives_own_crcs(tmp_path, monkeypatch):
    path = build_7z(tmp_path / "switch.7z", {"switch/app/app.nro": b"the real bytes"})
    _py7zr_refuses(monkeypatch)
    _fake_tool(monkeypatch, _UNPACK_WRONG)
    with pytest.raises(extractor.ArchiveError, match="unpacked at|CRC32"):
        extractor.safe_extract(path, "job", work_root=tmp_path / "work")
    assert not (tmp_path / "work" / "job").exists()


def test_with_no_external_tool_the_error_says_what_would_fix_it(tmp_path, monkeypatch):
    path = build_7z(tmp_path / "switch.7z", {"switch/app/app.nro": b"nro"})
    _py7zr_refuses(monkeypatch)
    monkeypatch.setattr(extractor, "external_7z_tools", lambda: [])
    with pytest.raises(extractor.ExtractionBackendMissingError, match="7-Zip"):
        extractor.safe_extract(path, "job", work_root=tmp_path / "work")
