from switchagent import config, db, scanner
from switchagent.model import ContentType

from .conftest import build_7z, build_zip


def _item(conn, rel_path):
    row = db.get_inbox_item(conn, rel_path)
    assert row is not None, f"no inbox_items row for {rel_path!r}"
    return row


def test_package_file_with_title_id_is_analyzed(isolated_db):
    conn, inbox_dir = isolated_db
    (inbox_dir / "Game [0100000000010000][v0].nsp").write_bytes(b"nsp bytes")

    summary = scanner.scan_once(conn)
    assert summary["new"] == 1

    row = _item(conn, "Game [0100000000010000][v0].nsp")
    assert row["status"] == "ANALYZED"
    assert row["content_type"] == ContentType.GAME_PACKAGE.value
    assert row["title_id"] == "0100000000010000"
    assert row["suggested_action"] == "INSTALL_VIA_DBI"
    assert row["suggested_target"] == "SD_INSTALL"


def test_invalid_title_id_needs_review_not_guessed(isolated_db):
    """No bracketed TITLE_ID in the filename at all -- must not be guessed,
    must not be silently ANALYZED with a made-up value."""
    conn, inbox_dir = isolated_db
    (inbox_dir / "SomeGameNoTitleId.nsp").write_bytes(b"nsp bytes")

    scanner.scan_once(conn)
    row = _item(conn, "SomeGameNoTitleId.nsp")
    assert row["status"] == "NEEDS_REVIEW"
    assert row["title_id"] is None
    assert row["suggested_action"] is None


def test_ambiguous_multi_bracket_title_id_needs_review(isolated_db):
    conn, inbox_dir = isolated_db
    (inbox_dir / "Weird [0100000000010000] [0100000000020000].nsp").write_bytes(b"x")

    scanner.scan_once(conn)
    row = _item(conn, "Weird [0100000000010000] [0100000000020000].nsp")
    assert row["status"] == "NEEDS_REVIEW"
    assert row["title_id"] is None


def test_atmosphere_folder_in_inbox_is_analyzed_as_copy_merge(isolated_db):
    conn, inbox_dir = isolated_db
    mod_dir = inbox_dir / "atmosphere/contents/0100000000010000/romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "asset.bin").write_bytes(b"asset data")

    scanner.scan_once(conn)
    row = _item(conn, "atmosphere/contents/0100000000010000")
    assert row["item_type"] == "MOD_FOLDER"
    assert row["status"] == "ANALYZED"
    assert row["content_type"] == ContentType.ATMOSPHERE_MOD.value
    assert row["title_id"] == "0100000000010000"
    assert row["title_id_confident"] == 1
    assert row["suggested_action"] == "COPY_MERGE"
    assert row["suggested_target"] == "SD_CARD"


def test_zip_mixed_content_needs_review(isolated_db):
    conn, inbox_dir = isolated_db
    build_zip(inbox_dir / "mixed.zip", {
        "game.nsp": b"nsp bytes",
        "atmosphere/contents/0100000000050000/romfs/x.bin": b"mod bytes",
    })

    scanner.scan_once(conn)
    row = _item(conn, "mixed.zip")
    assert row["status"] == "NEEDS_REVIEW"
    assert row["content_type"] == ContentType.MIXED.value
    assert row["suggested_action"] is None


def test_unscannable_zip_needs_review(isolated_db):
    conn, inbox_dir = isolated_db
    build_zip(inbox_dir / "mystery.zip", {"readme.txt": b"nothing switch-related"})

    scanner.scan_once(conn)
    row = _item(conn, "mystery.zip")
    assert row["status"] == "NEEDS_REVIEW"
    assert row["content_type"] == ContentType.UNKNOWN.value


def test_7z_with_nsp_is_analyzed(isolated_db):
    conn, inbox_dir = isolated_db
    build_7z(inbox_dir / "pack.7z", {"Game [0100000000060000][v0].nsp": b"7z nsp bytes"})

    scanner.scan_once(conn)
    row = _item(conn, "pack.7z")
    assert row["status"] == "ANALYZED"
    assert row["content_type"] == ContentType.GAME_PACKAGE.value
    assert row["title_id"] == "0100000000060000"


def test_duplicate_sha256_same_file_different_name_is_skipped(isolated_db):
    """The literal 'duplicate SHA-256, identical files' scenario: the
    exact same bytes dropped twice under two different filenames."""
    conn, inbox_dir = isolated_db
    (inbox_dir / "Game [0100000000010000][v0].nsp").write_bytes(b"identical content")
    (inbox_dir / "Game_backup_copy.nsp").write_bytes(b"identical content")

    scanner.scan_once(conn)

    original = _item(conn, "Game [0100000000010000][v0].nsp")
    copy = _item(conn, "Game_backup_copy.nsp")
    assert original["status"] == "ANALYZED"
    assert copy["status"] == "SKIP_DUPLICATE"
    assert copy["content_hash"] == original["content_hash"]
    assert "Game [0100000000010000][v0].nsp" in copy["note"]


def test_duplicate_sha256_across_different_archives_is_skipped(isolated_db):
    """Same idea, but the duplicate content is packaged inside two
    different zip archives with different names."""
    conn, inbox_dir = isolated_db
    build_zip(inbox_dir / "release_a.zip", {"Game [0100000000010000][v0].nsp": b"same bytes"})
    build_zip(inbox_dir / "release_b_reupload.zip", {"Game [0100000000010000][v0].nsp": b"same bytes"})

    scanner.scan_once(conn)

    a = _item(conn, "release_a.zip")
    b = _item(conn, "release_b_reupload.zip")
    statuses = {a["status"], b["status"]}
    assert statuses == {"ANALYZED", "SKIP_DUPLICATE"}
    assert a["content_hash"] == b["content_hash"]
    # original is untouched, not deleted
    assert (inbox_dir / "release_a.zip").exists()
    assert (inbox_dir / "release_b_reupload.zip").exists()


def test_cross_root_duplicate_flagged_when_already_available_in_library(isolated_db):
    """ARCH-005: inbox/ and the library (config.LIBRARY_DIR) are two
    independent roots scanned by two independent passes -- before this fix,
    find_other_analyzed_item_with_hash() only ever looked at inbox_items,
    so a copy that already existed in the library (already scanned,
    AVAILABLE) was never detected when the same content later showed up in
    inbox/ too. Must be flagged SKIP_DUPLICATE, never deleted/changed."""
    conn, inbox_dir = isolated_db
    library_path = config.LIBRARY_DIR / "Game [0100000000010000][v0].nsp"
    library_path.write_bytes(b"identical content")
    scanner.scan_library_once(conn)
    assert db.get_library_item(conn, str(library_path))["status"] == "AVAILABLE"

    # Fast indexing leaves hashes empty. Cross-root dedup applies only
    # when an actual content hash has been recorded by a deep analysis.
    conn.execute("UPDATE library_items SET content_hash = ? WHERE absolute_path = ?",
                 (scanner.sha256_file(library_path), str(library_path)))
    conn.commit()

    (inbox_dir / "Game_backup_copy.nsp").write_bytes(b"identical content")
    scanner.scan_once(conn)

    copy = _item(conn, "Game_backup_copy.nsp")
    assert copy["status"] == "SKIP_DUPLICATE"
    assert "library" in copy["note"]
    # neither the library original nor the inbox copy is touched/deleted
    assert library_path.exists()
    assert (inbox_dir / "Game_backup_copy.nsp").exists()


def test_rescanning_unchanged_file_does_not_rehash(isolated_db, monkeypatch):
    conn, inbox_dir = isolated_db
    (inbox_dir / "Game [0100000000010000][v0].nsp").write_bytes(b"nsp bytes")
    scanner.scan_once(conn)

    calls = []
    original = scanner.sha256_file

    def spy(path, *a, **k):
        calls.append(path)
        return original(path, *a, **k)

    monkeypatch.setattr(scanner, "sha256_file", spy)
    summary = scanner.scan_once(conn)
    assert summary["unchanged"] == 1
    assert calls == [], "unchanged file must not be re-hashed"


def test_removed_file_is_cleaned_up_from_db(isolated_db):
    conn, inbox_dir = isolated_db
    path = inbox_dir / "Game [0100000000010000][v0].nsp"
    path.write_bytes(b"nsp bytes")
    scanner.scan_once(conn)
    assert db.get_inbox_item(conn, "Game [0100000000010000][v0].nsp") is not None

    path.unlink()
    summary = scanner.scan_once(conn)
    assert summary["removed"] == 1
    assert db.get_inbox_item(conn, "Game [0100000000010000][v0].nsp") is None
