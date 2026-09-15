"""Tests for scanner.scan_library_once() -- the Web UI's indexer for
config.LIBRARY_DIR (default D:\\shared\\Download), kept deliberately
separate from scan_once()/inbox_items (see docs/WEB-UI.md). Reuses the
exact same classification rules as inbox scanning (test_scanner.py already
covers those exhaustively); these tests are about the library-specific
wiring: absolute-path identity, the AVAILABLE status label, and
library-scoped duplicate detection.
"""

from __future__ import annotations

from switchagent import config, db, scanner
from switchagent.model import ContentType


def _item(conn, absolute_path):
    row = db.get_library_item(conn, absolute_path)
    assert row is not None, f"no library_items row for {absolute_path!r}"
    return row


def test_package_file_is_indexed_as_available(isolated_db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Package indexing must neither hash nor wait")
    monkeypatch.setattr(scanner, "sha256_file", forbidden)
    monkeypatch.setattr(scanner, "is_file_stable", forbidden)
    conn, _inbox_dir = isolated_db
    path = config.LIBRARY_DIR / "Game [0100000000010000][v0].nsp"
    path.write_bytes(b"nsp bytes")

    summary = scanner.scan_library_once(conn)
    assert summary["new"] == 1

    row = _item(conn, str(path))
    assert row["status"] == "AVAILABLE"
    assert row["content_type"] == ContentType.GAME_PACKAGE.value
    assert row["title_id"] == "0100000000010000"
    assert row["suggested_target"] == "SD_INSTALL"
    assert row["content_hash"] is None


def test_missing_title_id_is_needs_review_not_guessed(isolated_db):
    conn, _inbox_dir = isolated_db
    path = config.LIBRARY_DIR / "SomeGameNoTitleId.nsp"
    path.write_bytes(b"nsp bytes")

    scanner.scan_library_once(conn)
    row = _item(conn, str(path))
    assert row["status"] == "NEEDS_REVIEW"
    assert row["title_id"] is None


def test_atmosphere_folder_is_indexed_as_copy_merge(isolated_db):
    conn, _inbox_dir = isolated_db
    mod_dir = config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "asset.bin").write_bytes(b"asset data")

    scanner.scan_library_once(conn)
    row = _item(conn, str(config.LIBRARY_DIR / "SomeMod" / "atmosphere" / "contents" / "0100000000010000"))
    assert row["item_type"] == "MOD_FOLDER"
    assert row["status"] == "AVAILABLE"
    assert row["content_type"] == ContentType.ATMOSPHERE_MOD.value
    assert row["suggested_action"] == "COPY_MERGE"


def test_rescan_of_unchanged_file_does_not_rehash(isolated_db):
    conn, _inbox_dir = isolated_db
    path = config.LIBRARY_DIR / "Game [0100000000010000][v0].nsp"
    path.write_bytes(b"nsp bytes")
    scanner.scan_library_once(conn)

    summary2 = scanner.scan_library_once(conn)
    assert summary2["unchanged"] == 1
    assert summary2["new"] == 0


def test_deleted_file_is_removed_from_library(isolated_db):
    conn, _inbox_dir = isolated_db
    path = config.LIBRARY_DIR / "Game [0100000000010000][v0].nsp"
    path.write_bytes(b"nsp bytes")
    scanner.scan_library_once(conn)
    assert len(db.list_library_items(conn)) == 1

    path.unlink()
    summary = scanner.scan_library_once(conn)
    assert summary["removed"] == 1
    assert db.list_library_items(conn) == []


def test_fast_index_does_not_claim_byte_identity_under_two_paths(isolated_db):
    conn, _inbox_dir = isolated_db
    original = config.LIBRARY_DIR / "Game [0100000000010000][v0].nsp"
    original.write_bytes(b"identical payload")
    scanner.scan_library_once(conn)

    (config.LIBRARY_DIR / "subfolder").mkdir()
    copy = config.LIBRARY_DIR / "subfolder" / "Game (redownload) [0100000000010000][v0].nsp"
    copy.write_bytes(b"identical payload")
    scanner.scan_library_once(conn)

    dup_row = _item(conn, str(copy))
    assert dup_row["status"] == "AVAILABLE"
    assert dup_row["content_hash"] is None
    original_row = _item(conn, str(original))
    assert original_row["status"] == "AVAILABLE"


def test_fast_index_does_not_infer_title_from_inbox_hash(isolated_db):
    conn, inbox_dir = isolated_db
    inbox_path = inbox_dir / "Game [0100000000010000][v0].nsp"
    inbox_path.write_bytes(b"identical content")
    scanner.scan_once(conn)
    assert db.get_inbox_item(conn, "Game [0100000000010000][v0].nsp")["status"] == "ANALYZED"

    library_copy = config.LIBRARY_DIR / "Game_redownloaded.nsp"
    library_copy.write_bytes(b"identical content")
    scanner.scan_library_once(conn)

    row = _item(conn, str(library_copy))
    assert row["status"] == "NEEDS_REVIEW"
    assert row["content_hash"] is None
    # neither the inbox original nor the library copy is touched/deleted
    assert inbox_path.exists()
    assert library_copy.exists()


def test_scan_never_creates_a_job(isolated_db):
    """Regression guard (Web UI spec point 25): scanning must never, by
    itself, create a job -- job creation is an explicit, separate action
    (see queue_worker.create_job_from_report(), always called from the
    Web UI's own /api/jobs handler, never from a scan)."""
    conn, _inbox_dir = isolated_db
    (config.LIBRARY_DIR / "Game [0100000000010000][v0].nsp").write_bytes(b"x")
    scanner.scan_library_once(conn)
    assert db.list_jobs(conn) == []


def test_scan_reports_error_if_library_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "does_not_exist")
    with db.open_db(tmp_path / "test.db") as conn:
        summary = scanner.scan_library_once(conn)
    assert summary["new"] == 0
    assert "error" in summary
