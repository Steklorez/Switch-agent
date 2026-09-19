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


# ---------------------------------------------------------------------------
# Relocation: the same file reached by a different path spelling. A Library
# folder re-pointed at the SAME directory over UNC (D:\shared\Download ->
# \192.168.50.2\shared\Download), a changed drive letter, a renamed parent --
# absolute_path is the identity key here, so every one of those used to
# re-index the whole folder as brand new items while the old rows stayed
# behind as ERROR "source file no longer found on disk" (they cannot be
# deleted once a job references them). One file then counted as two:
# Game Details said "Base game: present (2 copies)" for a single base game.
# ---------------------------------------------------------------------------

def _relocate(library_dir, monkeypatch, tmp_path, name="library-elsewhere"):
    """Copies LIBRARY_DIR to a second directory byte-for-byte, mtimes
    included (SMB preserves them exactly -- verified against the real
    duplicated rows this fixes), and points config at the copy. The
    original files are deliberately left on disk: in the case this is
    about, nothing was deleted, the folder is simply reached another way."""
    import os
    import shutil

    other = tmp_path / name
    shutil.copytree(library_dir, other)
    for source in library_dir.rglob("*"):
        st = source.stat()
        os.utime(other / source.relative_to(library_dir), ns=(st.st_atime_ns, st.st_mtime_ns))
    monkeypatch.setattr(config, "LIBRARY_DIR", other)
    return other


def test_the_same_file_reached_by_a_new_path_is_not_indexed_twice(isolated_db, monkeypatch, tmp_path):
    conn, _inbox_dir = isolated_db
    library_dir = config.LIBRARY_DIR
    (library_dir / "Game [0100000000010000][v0].nsp").write_bytes(b"nsp bytes")
    scanner.scan_library_once(conn)
    original = db.list_library_items(conn)[0]

    other = _relocate(library_dir, monkeypatch, tmp_path)
    summary = scanner.scan_library_once(conn)

    assert summary["relocated"] == 1
    assert summary["new"] == 0
    rows = db.list_library_items(conn)
    assert len(rows) == 1, "one file must not count as two just because the path changed"
    assert rows[0]["absolute_path"] == str(other / "Game [0100000000010000][v0].nsp")
    assert rows[0]["status"] == "AVAILABLE"
    # The library did not learn about this file today.
    assert rows[0]["first_seen_at"] == original["first_seen_at"]


def test_a_relocated_file_keeps_the_job_history_pointing_at_it(isolated_db, monkeypatch, tmp_path):
    """The whole reason the old row could not simply be deleted: a job
    references it. The surviving row is the one describing where the file
    actually is now, so jobs are repointed at it rather than left dangling
    (or blocking the merge)."""
    conn, _inbox_dir = isolated_db
    library_dir = config.LIBRARY_DIR
    (library_dir / "Game [0100000000010000][v0].nsp").write_bytes(b"nsp bytes")
    scanner.scan_library_once(conn)
    item_id = db.list_library_items(conn)[0]["id"]
    job_id = db.create_job(
        conn, library_item_id=item_id, action="INSTALL_VIA_DBI",
        target_storage="SD_INSTALL", target_device_id="mock-switch-parent",
    )

    _relocate(library_dir, monkeypatch, tmp_path)
    assert scanner.scan_library_once(conn)["relocated"] == 1

    rows = db.list_library_items(conn)
    assert len(rows) == 1
    assert db.get_job(conn, job_id)["library_item_id"] == rows[0]["id"]


def test_a_relocated_mod_folder_is_merged_by_its_own_fingerprint(isolated_db, monkeypatch, tmp_path):
    conn, _inbox_dir = isolated_db
    library_dir = config.LIBRARY_DIR
    mod_dir = library_dir / "SomeMod" / "atmosphere" / "contents" / "0100000000010000" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "asset.bin").write_bytes(b"asset data")
    scanner.scan_library_once(conn)
    assert len(db.list_library_items(conn)) == 1

    _relocate(library_dir, monkeypatch, tmp_path)
    assert scanner.scan_library_once(conn)["relocated"] == 1
    assert len(db.list_library_items(conn)) == 1


def test_two_live_copies_of_one_file_are_still_two_items(isolated_db, monkeypatch, tmp_path):
    """A relocation is "the row I did not visit IS the row I did". Two
    copies sitting in two watched folders are both visited, so neither is
    stale and nothing is merged -- that is SKIP_DUPLICATE's job, not this
    one, and collapsing them would silently lose a real file."""
    import os
    import shutil

    conn, _inbox_dir = isolated_db
    library_dir = config.LIBRARY_DIR
    source = library_dir / "Game [0100000000010000][v0].nsp"
    source.write_bytes(b"nsp bytes")
    (library_dir / "second").mkdir()
    copy = library_dir / "second" / "Game [0100000000010000][v0].nsp"
    shutil.copy2(source, copy)
    st = source.stat()
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))

    summary = scanner.scan_library_once(conn)
    assert summary["relocated"] == 0
    assert len(db.list_library_items(conn)) == 2


def test_an_unreachable_library_folder_is_never_merged_away(isolated_db, monkeypatch, tmp_path):
    """An unplugged drive already keeps its index (test above). It must not
    lose it to a file that merely looks identical under a folder that IS
    reachable -- the offline copy is not a stale spelling of it."""
    conn, _inbox_dir = isolated_db
    offline = tmp_path / "offline-drive"
    offline.mkdir()
    (offline / "Game [0100000000010000][v0].nsp").write_bytes(b"nsp bytes")
    monkeypatch.setattr(config, "LIBRARY_DIR", offline)
    scanner.scan_library_once(conn)
    assert len(db.list_library_items(conn)) == 1

    online = _relocate(offline, monkeypatch, tmp_path, name="still-plugged-in")
    shutil_rmtree_offline = offline
    import shutil
    shutil.rmtree(shutil_rmtree_offline)  # the drive is gone, its rows must stay
    monkeypatch.setattr(config, "library_dirs", lambda: (online, offline))

    summary = scanner.scan_library_once(conn)
    assert summary["relocated"] == 0
    assert len(db.list_library_items(conn)) == 2


# ---------------------------------------------------------------------------
# Cancellation. A Library folder pointed at something the size of a real
# Downloads directory takes minutes per pass, and until this existed there
# was no way out of one: ScanState had no stop flag, scan_library_once() had
# no cancellation hook, and set_library_dir() refused to change the folder
# list while a scan ran -- so the one action that would end an unwanted scan
# was the one action blocked by it.
# ---------------------------------------------------------------------------

def test_a_scan_stops_when_asked_and_keeps_what_it_already_indexed(isolated_db):
    conn, _inbox_dir = isolated_db
    for n in range(6):
        (config.LIBRARY_DIR / f"Game {n} [010000000001{n:04}][v0].nsp").write_bytes(b"nsp bytes")

    seen = []

    def stop_after_two():
        return len(seen) >= 2

    summary = scanner.scan_library_once(conn, on_file=seen.append, should_stop=stop_after_two)

    assert summary["cancelled"] is True
    indexed = db.list_library_items(conn)
    assert 0 < len(indexed) < 6, "a cancelled pass keeps its partial work, and stops early"


def test_a_cancelled_scan_never_deletes_what_it_did_not_reach(isolated_db):
    """The two whole-library passes at the bottom of scan_library_once()
    both reason from seen_absolute_paths as if it were complete. Running
    either after a half-finished walk would delete (or merge away) rows for
    files the pass simply never got to -- a "stop" that silently emptied
    the library would be far worse than the wait it saved."""
    conn, _inbox_dir = isolated_db
    for n in range(4):
        (config.LIBRARY_DIR / f"Game {n} [010000000001{n:04}][v0].nsp").write_bytes(b"nsp bytes")
    scanner.scan_library_once(conn)
    assert len(db.list_library_items(conn)) == 4

    summary = scanner.scan_library_once(conn, should_stop=lambda: True)

    assert summary["cancelled"] is True
    assert summary["removed"] == 0 and summary["relocated"] == 0
    assert len(db.list_library_items(conn)) == 4, "nothing may be dropped on the strength of a partial walk"


def test_stopping_before_anything_is_walked_is_still_a_clean_cancel(isolated_db):
    conn, _inbox_dir = isolated_db
    (config.LIBRARY_DIR / "Game [0100000000010000][v0].nsp").write_bytes(b"nsp bytes")

    summary = scanner.scan_library_once(conn, should_stop=lambda: True)

    assert summary["cancelled"] is True
    assert summary["new"] == 0
    assert db.list_library_items(conn) == []
