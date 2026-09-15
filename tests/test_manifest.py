"""Direct unit tests for switchagent/manifest.py -- the frozen content
snapshot that fixes the TOCTOU gap between job confirmation and transfer.
"""

from __future__ import annotations

import pytest

from switchagent import config, manifest, preview
from switchagent.model import ContentType

from .conftest import build_zip


def test_build_manifest_for_bare_package_file_uses_inbox_source_kind(isolated_db):
    conn, inbox_dir = isolated_db
    name = "Game [0100000000010000][v0].nsp"
    (inbox_dir / name).write_bytes(b"nsp bytes")
    report = preview.preview_path(inbox_dir / name)

    m = manifest.build_manifest_and_stage(report, job_id=1, target_storage="SD_INSTALL")

    assert m.content_type == ContentType.GAME_PACKAGE.value
    assert len(m.files) == 1
    f = m.files[0]
    assert f.source_kind == "inbox"
    assert f.source_relative_path == name
    assert f.dest_relative_path == name
    assert f.size == len(b"nsp bytes")
    # no local copy made for a bare inbox file
    assert not manifest.job_work_dir(1).joinpath(name).exists()


def test_build_manifest_for_archived_package_freezes_a_local_copy(isolated_db, monkeypatch):
    conn, inbox_dir = isolated_db
    monkeypatch.setattr(config, "WORK_DIR", config.WORK_DIR)  # keep isolated_db's redirect
    path = build_zip(inbox_dir / "pack.zip", {"Game [0100000000020000][v0].nsp": b"zip nsp bytes"})
    report = preview.preview_path(path, extract=True)

    m = manifest.build_manifest_and_stage(report, job_id=7, target_storage="SD_INSTALL")

    f = m.files[0]
    assert f.source_kind == "frozen"
    frozen_path = manifest.resolve_source_path(f, job_id=7)
    assert frozen_path.is_file()
    assert frozen_path.read_bytes() == b"zip nsp bytes"


def test_build_manifest_for_mod_folder_lists_every_file(isolated_db):
    conn, inbox_dir = isolated_db
    mod_dir = inbox_dir / "atmosphere" / "contents" / "0100000000030000" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "a.bin").write_bytes(b"aaa")
    (mod_dir / "b.bin").write_bytes(b"bbbb")
    report = preview.preview_path(mod_dir.parent)

    m = manifest.build_manifest_and_stage(report, job_id=2, target_storage="SD_CARD")

    dest_paths = {f.dest_relative_path for f in m.files}
    assert dest_paths == {
        "atmosphere/contents/0100000000030000/romfs/a.bin",
        "atmosphere/contents/0100000000030000/romfs/b.bin",
    }
    assert all(f.source_kind == "inbox" for f in m.files)


def test_build_manifest_rejects_mixed_content(isolated_db):
    conn, inbox_dir = isolated_db
    path = build_zip(inbox_dir / "mixed.zip", {
        "game.nsp": b"x",
        "atmosphere/contents/0100000000040000/romfs/y.bin": b"y",
    })
    report = preview.preview_path(path)
    with pytest.raises(manifest.ManifestError):
        manifest.build_manifest_and_stage(report, job_id=3, target_storage="SD_INSTALL")


def test_verify_manifest_detects_missing_source(isolated_db):
    conn, inbox_dir = isolated_db
    name = "Game [0100000000050000][v0].nsp"
    (inbox_dir / name).write_bytes(b"nsp bytes")
    report = preview.preview_path(inbox_dir / name)
    m = manifest.build_manifest_and_stage(report, job_id=4, target_storage="SD_INSTALL")

    (inbox_dir / name).unlink()

    mismatch = manifest.verify_manifest_against_source(m, job_id=4)
    assert mismatch is not None
    assert mismatch.kind == "missing"


def test_verify_manifest_detects_changed_content_same_size(isolated_db):
    conn, inbox_dir = isolated_db
    name = "Game [0100000000060000][v0].nsp"
    (inbox_dir / name).write_bytes(b"AAAAAAAAAA")
    report = preview.preview_path(inbox_dir / name)
    m = manifest.build_manifest_and_stage(report, job_id=5, target_storage="SD_INSTALL")

    # Same length, different bytes -- must be caught by the hash check, not
    # just the (cheaper, size-only) fast path.
    (inbox_dir / name).write_bytes(b"BBBBBBBBBB")

    mismatch = manifest.verify_manifest_against_source(m, job_id=5)
    assert mismatch is not None
    assert mismatch.kind == "changed"


def test_verify_manifest_passes_when_unchanged(isolated_db):
    conn, inbox_dir = isolated_db
    name = "Game [0100000000070000][v0].nsp"
    (inbox_dir / name).write_bytes(b"stable content")
    report = preview.preview_path(inbox_dir / name)
    m = manifest.build_manifest_and_stage(report, job_id=6, target_storage="SD_INSTALL")

    assert manifest.verify_manifest_against_source(m, job_id=6) is None


def test_progress_round_trip(isolated_db):
    assert manifest.load_progress(job_id=42) == set()
    manifest.mark_delivered(job_id=42, dest_relative_path="a.bin")
    manifest.mark_delivered(job_id=42, dest_relative_path="b.bin")
    assert manifest.load_progress(job_id=42) == {"a.bin", "b.bin"}


# -- FAULT-001: manifest/progress corruption is refused, never guessed at --
# (mock-only, per the mandate -- these exercise resolve_source_path()'s
# path-safety directly, not through a real device.)

def test_resolve_source_path_rejects_unknown_source_kind(isolated_db):
    conn, inbox_dir = isolated_db
    bad_file = manifest.ManifestFile(
        dest_relative_path="game.nsp", source_kind="bogus",
        source_relative_path="game.nsp", size=1, sha256="deadbeef",
    )
    with pytest.raises(manifest.ManifestError, match="unknown source_kind"):
        manifest.resolve_source_path(bad_file, job_id=1)


def test_resolve_source_path_rejects_a_relative_path_that_would_escape_its_root(isolated_db):
    conn, inbox_dir = isolated_db
    escaping = manifest.ManifestFile(
        dest_relative_path="game.nsp", source_kind="inbox",
        source_relative_path="../../outside.nsp", size=1, sha256="deadbeef",
    )
    with pytest.raises(manifest.ManifestError, match="escape"):
        manifest.resolve_source_path(escaping, job_id=1)


def test_resolve_source_path_rejects_an_absolute_path(isolated_db, tmp_path):
    conn, inbox_dir = isolated_db
    outside = tmp_path / "elsewhere" / "secret.bin"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not yours")
    absolute = manifest.ManifestFile(
        dest_relative_path="game.nsp", source_kind="inbox",
        source_relative_path=str(outside), size=1, sha256="deadbeef",
    )
    with pytest.raises(manifest.ManifestError, match="escape"):
        manifest.resolve_source_path(absolute, job_id=1)


def test_resolve_source_path_accepts_ordinary_valid_paths_unchanged(isolated_db):
    """Regression guard: the new escape/unknown-source_kind checks must
    never reject any normal, correctly-built manifest file -- only
    genuinely invalid data."""
    conn, inbox_dir = isolated_db
    name = "Game [0100000000080000][v0].nsp"
    (inbox_dir / name).write_bytes(b"nsp bytes")
    report = preview.preview_path(inbox_dir / name)
    m = manifest.build_manifest_and_stage(report, job_id=8, target_storage="SD_INSTALL")

    resolved = manifest.resolve_source_path(m.files[0], job_id=8)
    assert resolved.is_file()
    assert resolved.read_bytes() == b"nsp bytes"


def test_verify_manifest_against_source_propagates_invalid_path_as_manifest_error(isolated_db):
    conn, inbox_dir = isolated_db
    bad_manifest = manifest.Manifest(
        content_type=ContentType.GAME_PACKAGE.value, title_id="0100000000081000",
        target_storage="SD_INSTALL",
        files=(manifest.ManifestFile(
            dest_relative_path="game.nsp", source_kind="inbox",
            source_relative_path="../escape.nsp", size=1, sha256="deadbeef",
        ),),
    )
    with pytest.raises(manifest.ManifestError, match="escape"):
        manifest.verify_manifest_against_source(bad_manifest, job_id=99)
