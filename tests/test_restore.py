"""Tests for W3-005's restore side (switchagent/restore.py) and the two
HTTP routes in switchagent/web/app.py.

The single most important test here is
test_state_a_backup_then_mutate_to_b_then_restore_gives_back_exactly_a --
the end-to-end correctness proof for the whole feature. Everything else
either supports it or covers a specific refusal/safety property.

Every "must be rejected" test asserts BOTH that the archive is refused AND
that the live state is byte-for-byte what it was -- a validation that
rejects an archive after already having mutated something would be worse
than no validation at all.

Shares tests/test_backup.py's isolated-app-state fixture and state-A
seeding helpers (same cross-test-module reuse tests/test_queue_worker.py
already does with test_stage41_recovery).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from switchagent import backup, config, db, restore
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

from .test_backup import app_state, make_app_state, read_manifest, seed_state_a, snapshot_db  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# helpers: building deliberately-bad archives
# ---------------------------------------------------------------------------

def _entries_of(archive_path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive_path) as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def rebuild_archive(
    source: Path, target: Path, *,
    entries: dict[str, bytes] | None = None,
    manifest: dict | None = None,
    symlink: tuple[str, str] | None = None,
) -> Path:
    """Rewrites a genuine backup into a tampered one. `entries` replaces/
    adds payload entries, `manifest` replaces manifest.json wholesale,
    `symlink` adds an entry encoded as a unix symlink (same technique
    tests/conftest.py's build_zip_with_symlink already uses)."""
    payload = _entries_of(source)
    current_manifest = json.loads(payload.pop(backup.MANIFEST_ENTRY_NAME).decode("utf-8"))
    if entries:
        payload.update(entries)
    if manifest is not None:
        current_manifest = manifest

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100644) << 16
            archive.writestr(info, data)
        if symlink is not None:
            link_name, link_target = symlink
            info = zipfile.ZipInfo(link_name)
            info.external_attr = (0xA1FF) << 16  # S_IFLNK | 0o777
            archive.writestr(info, link_target.encode())
        archive.writestr(
            backup.MANIFEST_ENTRY_NAME,
            json.dumps(current_manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
    return target


def manifest_entry(name: str, data: bytes) -> dict:
    return {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class RecordingHooks:
    """Records the exact order the runtime hooks fire in."""

    def __init__(self):
        self.calls: list[str] = []

    def _record(self, label):
        def _hook():
            self.calls.append(label)
        return _hook

    def as_hooks(self) -> restore.RestoreRuntimeHooks:
        return restore.RestoreRuntimeHooks(
            pause_worker=self._record("pause_worker"),
            resume_worker=self._record("resume_worker"),
            stop_watcher=self._record("stop_watcher"),
            start_watcher=self._record("start_watcher"),
            quiesce_worker=self._record("quiesce_worker"),
            start_worker=self._record("start_worker"),
        )


# A LIVE worker thread refreshes the `devices` table on every tick (see
# WebContext.refresh_devices -> db.upsert_device_seen), so that one table is
# legitimately a moving target in the two tests below that run a real
# worker. Everything else is stable, and is what those tests compare.
_WORKER_VOLATILE_TABLES = frozenset({"devices"})


def snapshot_db_stable(db_path: Path) -> dict:
    return {
        table: rows for table, rows in snapshot_db(db_path).items()
        if table not in _WORKER_VOLATILE_TABLES
    }


def assert_state_usable(state) -> None:
    """The application must still be able to open, read AND write its
    database after any refused/failed restore."""
    conn = db.get_connection(state.db_path)
    try:
        db.init_db(conn)
        db.upsert_device_seen(conn, "device-usability-probe", "probe")
        assert db.get_device(conn, "device-usability-probe") is not None
        conn.execute("DELETE FROM devices WHERE device_id = 'device-usability-probe'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seeded(app_state):  # noqa: F811 -- pytest fixture injection
    with db.open_db(app_state.db_path) as conn:
        seed_state_a(conn)
    return app_state


@pytest.fixture
def good_archive(seeded):
    return backup.create_backup(seeded.tmp / "good.zip").path


# ---------------------------------------------------------------------------
# THE core correctness proof
# ---------------------------------------------------------------------------

def test_state_a_backup_then_mutate_to_b_then_restore_gives_back_exactly_a(app_state):  # noqa: F811
    """Build state A, back it up, mutate the LIVE state into a materially
    different state B (add rows, change rows, delete rows, edit
    config.yaml), restore the A backup, then read the database and
    config.yaml straight off disk and assert they are exactly A again --
    not B, and not some merge of the two."""
    with db.open_db(app_state.db_path) as conn:
        ids = seed_state_a(conn)

    state_a_db = snapshot_db(app_state.db_path)
    state_a_config = app_state.config_path.read_bytes()

    archive = backup.create_backup(app_state.tmp / "state-a.zip").path

    # -- mutate into a materially different state B -----------------------
    with db.open_db(app_state.db_path) as conn:
        # added
        db.upsert_device_seen(conn, "device-gamma", "Third Switch")
        db.set_device_friendly_name(conn, "device-gamma", "Only in B")
        db.upsert_library_item(
            conn, absolute_path="D:\\shared\\Download\\OnlyInB.nsp",
            item_type="FILE", file_type="NSP", size=7, mtime=7.0,
            content_hash="hash-only-b", title_id="0100000000090000",
            title_id_source="filename", status="AVAILABLE",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        )
        # changed
        db.set_device_friendly_name(conn, "device-alpha", "RENAMED IN B")
        db.set_device_storage_mapping(conn, "device-alpha", "SD Card", "SD_INSTALL")
        db.set_user_verified_outcome(conn, ids["history_verified_id"], "FAILED")
        db.update_job_status(conn, ids["job_ids"][1], "DONE", finished_at=db.now_iso())
        # deleted
        conn.execute("DELETE FROM install_history WHERE id = ?", (ids["history_failed_id"],))
        conn.execute("DELETE FROM device_storage_mappings WHERE device_id = 'device-beta'")
        conn.commit()
    app_state.config_path.write_text("# completely different config for state B\n", encoding="utf-8")

    state_b_db = snapshot_db(app_state.db_path)
    assert state_b_db != state_a_db, "the mutation must actually have changed something"

    # -- restore A ---------------------------------------------------------
    result = restore.restore_backup(
        archive,
        db_path=app_state.db_path,
        config_path=app_state.config_path,
        emergency_backup_dir=app_state.tmp / "emergency",
    )
    assert result["restored"] is True

    # -- read straight off disk and compare against A ----------------------
    restored_db = snapshot_db(app_state.db_path)
    assert restored_db == state_a_db
    assert restored_db != state_b_db
    assert app_state.config_path.read_bytes() == state_a_config

    # Spot-check the specific mutations are gone, so a failure here names
    # the lost thing instead of just "the dicts differ".
    conn = sqlite3.connect(app_state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        names = {row["device_id"]: row["friendly_name"] for row in conn.execute("SELECT * FROM devices")}
        assert names["device-alpha"] == "Parent's Switch"
        assert "device-gamma" not in names
        assert conn.execute(
            "SELECT COUNT(*) FROM library_items WHERE content_hash = 'hash-only-b'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT user_verified_outcome FROM install_history WHERE id = ?",
            (ids["history_verified_id"],),
        ).fetchone()[0] == "SUCCESS"
        assert conn.execute(
            "SELECT COUNT(*) FROM install_history WHERE id = ?", (ids["history_failed_id"],)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM device_storage_mappings WHERE device_id = 'device-beta'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    assert_state_usable(app_state)


# ---------------------------------------------------------------------------
# round-trip: each thing the mandate names explicitly
# ---------------------------------------------------------------------------

def _wipe_and_restore(state, archive) -> None:
    """Destroy the live DB entirely, then restore -- so "it survived" can
    never be an artifact of the row simply never having been touched."""
    with db.open_db(state.db_path) as conn:
        for table in ("job_log", "install_history", "jobs", "device_storage_mappings",
                      "installation_batches", "library_items", "inbox_items", "devices"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    state.config_path.write_text("# wiped\n", encoding="utf-8")
    restore.restore_backup(
        archive, db_path=state.db_path, config_path=state.config_path,
        emergency_backup_dir=state.tmp / "emergency",
    )


def test_valid_restore_round_trip_reports_success(seeded, good_archive):
    result = restore.restore_backup(
        good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
        emergency_backup_dir=seeded.tmp / "emergency",
    )
    assert result["restored"] is True
    assert result["config_restored"] is True
    assert sorted(result["replaced"]) == sorted([backup.DB_ENTRY_NAME, backup.CONFIG_ENTRY_NAME])
    assert result["backup_format_version"] == 1


def test_db_rows_survive_a_backup_restore_cycle(seeded, good_archive):
    before = snapshot_db(seeded.db_path)
    _wipe_and_restore(seeded, good_archive)
    assert snapshot_db(seeded.db_path) == before


def test_config_yaml_survives(seeded, good_archive):
    original = seeded.config_path.read_bytes()
    _wipe_and_restore(seeded, good_archive)
    assert seeded.config_path.read_bytes() == original
    assert b"D:\\\\shared\\\\Download" in seeded.config_path.read_bytes()


def test_device_friendly_names_survive(seeded, good_archive):
    _wipe_and_restore(seeded, good_archive)
    with db.open_db(seeded.db_path) as conn:
        names = {row["device_id"]: row["friendly_name"] for row in db.list_devices(conn)}
    assert names == {"device-alpha": "Parent's Switch", "device-beta": "Child's Switch"}


def test_storage_mappings_survive(seeded, good_archive):
    _wipe_and_restore(seeded, good_archive)
    with db.open_db(seeded.db_path) as conn:
        alpha = {row["raw_storage_name"]: row["logical_name"]
                 for row in db.list_device_storage_mappings(conn, "device-alpha")}
        beta = {row["raw_storage_name"]: row["logical_name"]
                for row in db.list_device_storage_mappings(conn, "device-beta")}
    assert alpha == {"SD Card": "SD_CARD", "Install to SD": "SD_INSTALL"}
    assert beta == {"SD Card": "SD_CARD"}


def test_user_verified_outcome_and_timestamp_survive(seeded, good_archive):
    with db.open_db(seeded.db_path) as conn:
        rows = db.list_install_history(conn)
        verified = [r for r in rows if r["user_verified_outcome"] is not None]
        assert len(verified) == 1
        expected_outcome = verified[0]["user_verified_outcome"]
        expected_at = verified[0]["user_verified_at"]
    assert expected_outcome == "SUCCESS"
    assert expected_at

    _wipe_and_restore(seeded, good_archive)

    with db.open_db(seeded.db_path) as conn:
        rows = db.list_install_history(conn)
        verified = [r for r in rows if r["user_verified_outcome"] is not None]
    assert len(verified) == 1
    assert verified[0]["user_verified_outcome"] == expected_outcome
    assert verified[0]["user_verified_at"] == expected_at


def test_batches_and_install_history_survive(seeded, good_archive):
    with db.open_db(seeded.db_path) as conn:
        batches_before = [dict(r) for r in conn.execute("SELECT * FROM installation_batches")]
        history_before = [dict(r) for r in db.list_install_history(conn)]

    _wipe_and_restore(seeded, good_archive)

    with db.open_db(seeded.db_path) as conn:
        assert [dict(r) for r in conn.execute("SELECT * FROM installation_batches")] == batches_before
        assert [dict(r) for r in db.list_install_history(conn)] == history_before
        # the job -> batch link survived too, not just the batch row
        jobs = {r["id"]: r["batch_id"] for r in db.list_jobs(conn)}
    assert batches_before[0]["id"] in set(jobs.values())


def test_library_and_inbox_items_survive(seeded, good_archive):
    """A backup that omitted library_items/inbox_items would silently empty
    the user's whole scanned library on restore."""
    _wipe_and_restore(seeded, good_archive)
    with db.open_db(seeded.db_path) as conn:
        assert len(db.list_library_items(conn)) == 2
        assert len(db.list_inbox_items(conn)) == 1


# ---------------------------------------------------------------------------
# validation refusals -- and the live state must be untouched every time
# ---------------------------------------------------------------------------

def _assert_refused_without_touching_state(state, archive, match):
    before_db = snapshot_db(state.db_path)
    before_config = state.config_path.read_bytes()

    with pytest.raises(restore.RestoreValidationError, match=match):
        restore.restore_backup(
            archive, db_path=state.db_path, config_path=state.config_path,
            emergency_backup_dir=state.tmp / "emergency",
        )

    assert snapshot_db(state.db_path) == before_db
    assert state.config_path.read_bytes() == before_config
    assert_state_usable(state)


def test_path_traversal_entry_is_rejected(seeded, good_archive):
    payload = b"pwned"
    manifest = read_manifest(good_archive)
    manifest["files"].append(manifest_entry("../evil.txt", payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "traversal.zip",
        entries={"../evil.txt": payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(seeded, tampered, "path-traversal")
    assert not (seeded.tmp / "evil.txt").exists()


def test_nested_traversal_entry_is_rejected(seeded, good_archive):
    payload = b"pwned"
    manifest = read_manifest(good_archive)
    manifest["files"].append(manifest_entry("data/../../evil.txt", payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "traversal2.zip",
        entries={"data/../../evil.txt": payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(seeded, tampered, "path-traversal")


def test_nested_non_traversal_entry_is_rejected(seeded, good_archive):
    """This project's own backup format is flat by design (three entries,
    no subdirectories) -- a nested entry that contains NO ".." at all
    (unlike the sibling traversal test above) must still be refused, by
    _check_entry_name()'s own separate "unexpected nested path entry"
    branch. Without this test, that branch had zero coverage: an entry
    like "data/switchagent.db" would sail through the traversal check
    (no ".." anywhere in it) and only this second, distinct guard stops
    it."""
    payload = b"pwned"
    manifest = read_manifest(good_archive)
    manifest["files"].append(manifest_entry("data/evil.txt", payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "nested.zip",
        entries={"data/evil.txt": payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(seeded, tampered, "nested path")


def test_absolute_path_entry_is_rejected(seeded, good_archive):
    payload = b"pwned"
    manifest = read_manifest(good_archive)
    manifest["files"].append(manifest_entry("/etc/evil.txt", payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "absolute.zip",
        entries={"/etc/evil.txt": payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(seeded, tampered, "absolute path")


def test_windows_drive_absolute_path_entry_is_rejected(seeded, good_archive):
    payload = b"pwned"
    manifest = read_manifest(good_archive)
    manifest["files"].append(manifest_entry("C:/Windows/evil.txt", payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "drive.zip",
        entries={"C:/Windows/evil.txt": payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(seeded, tampered, "absolute path")


def test_symlink_entry_is_rejected(seeded, good_archive):
    manifest = read_manifest(good_archive)
    manifest["files"].append(manifest_entry("link.txt", b"C:/Windows/System32"))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "symlink.zip",
        manifest=manifest, symlink=("link.txt", "C:/Windows/System32"),
    )
    _assert_refused_without_touching_state(seeded, tampered, "symlink")


def test_executable_entry_is_rejected(seeded, good_archive):
    payload = b"MZ\x90\x00"
    manifest = read_manifest(good_archive)
    manifest["files"].append(manifest_entry("payload.exe", payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "exe.zip",
        entries={"payload.exe": payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(seeded, tampered, "executable file")


def test_bad_sha256_checksum_is_rejected(seeded, good_archive):
    manifest = read_manifest(good_archive)
    for entry in manifest["files"]:
        if entry["name"] == backup.DB_ENTRY_NAME:
            entry["sha256"] = "0" * 64
    tampered = rebuild_archive(good_archive, seeded.tmp / "badsum.zip", manifest=manifest)
    _assert_refused_without_touching_state(seeded, tampered, "SHA-256")


def test_tampered_db_content_is_caught_by_its_checksum(seeded, good_archive):
    """The realistic version of the above: the payload changed, the
    manifest did not."""
    original = _entries_of(good_archive)[backup.DB_ENTRY_NAME]
    mutated = bytearray(original)
    mutated[200:210] = b"TAMPERED!!"
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "tampered.zip",
        entries={backup.DB_ENTRY_NAME: bytes(mutated)},
    )
    _assert_refused_without_touching_state(seeded, tampered, "SHA-256")


def test_entry_larger_than_its_manifest_claim_is_rejected_mid_stream(seeded, good_archive):
    """_extract_verified()'s decompression-bomb defense: it refuses the
    moment a real entry's stream exceeds the size its OWN manifest entry
    claims, mid-extraction -- before the whole (potentially huge) entry is
    ever written to disk. Built without any raw byte-level zip hacking:
    the archive's actual DB entry is left as-is (real, correctly-sized
    data), but its manifest entry is replaced with one that under-declares
    the size (and, necessarily, the wrong SHA-256 for that false size) --
    the exact "manifest lies about how big this entry is" shape a crafted
    malicious archive would take. If the mid-stream guard didn't exist,
    this would only be caught by the final size/checksum check, AFTER
    already streaming the entire (real-sized) entry to disk."""
    real_db_bytes = _entries_of(good_archive)[backup.DB_ENTRY_NAME]
    assert len(real_db_bytes) > 100  # sanity: a real seeded DB, not a stub
    manifest = read_manifest(good_archive)
    for entry in manifest["files"]:
        if entry["name"] == backup.DB_ENTRY_NAME:
            entry.update(manifest_entry(backup.DB_ENTRY_NAME, real_db_bytes[:64]))  # false, tiny claim
    tampered = rebuild_archive(good_archive, seeded.tmp / "oversized_entry.zip", manifest=manifest)
    _assert_refused_without_touching_state(seeded, tampered, "larger than its manifest entry claims")


def test_too_many_archive_entries_is_rejected(seeded, good_archive, monkeypatch):
    """MAX_ARCHIVE_ENTRIES exists to bound how many central-directory
    entries this code will even iterate over before trusting anything --
    monkeypatches the real production constant down to a size the existing
    3-entry good_archive already exceeds, rather than manufacturing 65
    dummy entries to reach the real default. Proves the actual guard
    clause fires, not merely that "some" rejection happens."""
    monkeypatch.setattr(restore, "MAX_ARCHIVE_ENTRIES", 2)
    _assert_refused_without_touching_state(seeded, good_archive, "more than the 2")


def test_declared_total_size_beyond_the_safety_limit_is_rejected(seeded, good_archive, monkeypatch):
    """MAX_TOTAL_UNCOMPRESSED_BYTES is read from the zip's own central
    directory (info.file_size) BEFORE a single byte is extracted -- this
    monkeypatches the real production constant down below the existing
    good_archive's real total size, exactly like the entry-count test
    above, so the guard is proven against real archive metadata rather
    than requiring an archive that is actually gigabytes in size."""
    monkeypatch.setattr(restore, "MAX_TOTAL_UNCOMPRESSED_BYTES", 10)
    _assert_refused_without_touching_state(seeded, good_archive, "uncompressed size exceeds the safety limit")


def test_extra_file_not_listed_in_the_manifest_is_rejected(seeded, good_archive):
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "extra.zip", entries={"sneaky.txt": b"not in the manifest"},
    )
    _assert_refused_without_touching_state(seeded, tampered, "not listed in its own manifest")


def test_file_listed_in_the_manifest_but_missing_is_rejected(seeded, good_archive):
    manifest = read_manifest(good_archive)
    manifest["files"].append(manifest_entry("ghost.txt", b"nope"))
    tampered = rebuild_archive(good_archive, seeded.tmp / "ghost.zip", manifest=manifest)
    _assert_refused_without_touching_state(seeded, tampered, "missing file")


def test_corrupted_non_sqlite_db_file_is_rejected(seeded, good_archive):
    payload = b"this is definitely not a SQLite database" * 20
    manifest = read_manifest(good_archive)
    for entry in manifest["files"]:
        if entry["name"] == backup.DB_ENTRY_NAME:
            entry.update(manifest_entry(backup.DB_ENTRY_NAME, payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "notadb.zip",
        entries={backup.DB_ENTRY_NAME: payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(seeded, tampered, "not a valid SQLite database")


def test_structurally_corrupt_sqlite_db_is_rejected(seeded, good_archive):
    """A file that still has a valid SQLite header but a mangled body --
    the case a plain "does it start with 'SQLite format 3'?" check would
    wave through."""
    original = bytearray(_entries_of(good_archive)[backup.DB_ENTRY_NAME])
    for i in range(200, min(len(original), 6000)):
        original[i] ^= 0xFF
    payload = bytes(original)
    manifest = read_manifest(good_archive)
    for entry in manifest["files"]:
        if entry["name"] == backup.DB_ENTRY_NAME:
            entry.update(manifest_entry(backup.DB_ENTRY_NAME, payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "corrupt.zip",
        entries={backup.DB_ENTRY_NAME: payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(
        seeded, tampered, "(integrity check|not a valid SQLite database|not a SwitchAgent database)",
    )


def test_a_valid_but_unrelated_sqlite_database_is_rejected(seeded, good_archive, tmp_path):
    """Openable, integrity-clean SQLite -- but not a SwitchAgent database.
    Must be refused, never silently 'migrated' into one."""
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    payload = foreign.read_bytes()

    manifest = read_manifest(good_archive)
    for entry in manifest["files"]:
        if entry["name"] == backup.DB_ENTRY_NAME:
            entry.update(manifest_entry(backup.DB_ENTRY_NAME, payload))
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "foreign.zip",
        entries={backup.DB_ENTRY_NAME: payload}, manifest=manifest,
    )
    _assert_refused_without_touching_state(seeded, tampered, "not a SwitchAgent database")


def test_unknown_backup_format_version_is_rejected(seeded, good_archive):
    manifest = read_manifest(good_archive)
    manifest["backup_format_version"] = 999
    tampered = rebuild_archive(good_archive, seeded.tmp / "v999.zip", manifest=manifest)
    _assert_refused_without_touching_state(seeded, tampered, "unsupported backup format version")


def test_malformed_manifest_json_is_rejected(seeded, good_archive):
    payload = _entries_of(good_archive)
    with zipfile.ZipFile(seeded.tmp / "badjson.zip", "w") as archive:
        for name, data in payload.items():
            if name == backup.MANIFEST_ENTRY_NAME:
                data = b"{not json at all"
            archive.writestr(name, data)
    _assert_refused_without_touching_state(seeded, seeded.tmp / "badjson.zip", "not valid JSON")


def test_archive_without_a_manifest_is_rejected(seeded, good_archive):
    payload = _entries_of(good_archive)
    payload.pop(backup.MANIFEST_ENTRY_NAME)
    with zipfile.ZipFile(seeded.tmp / "nomanifest.zip", "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    _assert_refused_without_touching_state(seeded, seeded.tmp / "nomanifest.zip", "no manifest.json")


def test_archive_whose_manifest_lists_no_database_is_rejected(seeded, good_archive):
    payload = _entries_of(good_archive)
    config_bytes = payload[backup.CONFIG_ENTRY_NAME]
    manifest = {
        "backup_format_version": 1, "app_version": "0.1.0",
        "created_at": "2026-09-12T00:00:00+00:00",
        "files": [manifest_entry(backup.CONFIG_ENTRY_NAME, config_bytes)],
        "size": len(config_bytes),
    }
    with zipfile.ZipFile(seeded.tmp / "nodb.zip", "w") as archive:
        archive.writestr(backup.CONFIG_ENTRY_NAME, config_bytes)
        archive.writestr(backup.MANIFEST_ENTRY_NAME, json.dumps(manifest).encode())
    _assert_refused_without_touching_state(seeded, seeded.tmp / "nodb.zip", "nothing to restore")


def test_a_file_that_is_not_a_zip_at_all_is_rejected(seeded):
    bogus = seeded.tmp / "notazip.zip"
    bogus.write_bytes(b"I am a PDF, honestly" * 50)
    _assert_refused_without_touching_state(seeded, bogus, "not a readable zip")


def test_a_missing_archive_is_rejected(seeded):
    _assert_refused_without_touching_state(seeded, seeded.tmp / "nope.zip", "not found")


# ---------------------------------------------------------------------------
# runtime safety sequence
# ---------------------------------------------------------------------------

def test_restore_is_refused_while_a_job_is_running(seeded, good_archive):
    """Step 1: an active physical transfer means an immediate, total
    refusal -- not even a pause, let alone a mutation."""
    with db.open_db(seeded.db_path) as conn:
        running_job = db.list_jobs(conn)[0]["id"]
        db.update_job_status(conn, running_job, "RUNNING", started_at=db.now_iso())

    before_db = snapshot_db(seeded.db_path)
    hooks = RecordingHooks()

    with pytest.raises(restore.RestoreRefused, match="RUNNING"):
        restore.restore_backup(
            good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
            hooks=hooks.as_hooks(), emergency_backup_dir=seeded.tmp / "emergency",
        )

    assert hooks.calls == [], "a refused restore must not even pause the worker"
    assert snapshot_db(seeded.db_path) == before_db
    assert not (seeded.tmp / "emergency").exists(), "no emergency backup should have been taken"
    assert_state_usable(seeded)


def test_restore_is_allowed_when_no_job_is_running(seeded, good_archive):
    with db.open_db(seeded.db_path) as conn:
        for row in db.list_jobs(conn):
            db.update_job_status(conn, row["id"], "DONE", finished_at=db.now_iso())
    result = restore.restore_backup(
        good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
        emergency_backup_dir=seeded.tmp / "emergency",
    )
    assert result["restored"] is True


def test_runtime_hooks_fire_in_the_mandated_order(seeded, good_archive):
    hooks = RecordingHooks()
    restore.restore_backup(
        good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
        hooks=hooks.as_hooks(), emergency_backup_dir=seeded.tmp / "emergency",
    )
    assert hooks.calls == [
        "pause_worker",      # step 2
        "stop_watcher",      # step 3
        "quiesce_worker",    # releases the DB handle for step 8's replace
        "start_worker",      # step 9  -- fresh DB connection
        "start_watcher",     # step 10
        "resume_worker",     # step 10 -- lifts the pause from step 2
    ]


def test_worker_and_watcher_are_restored_even_when_validation_fails(seeded, good_archive):
    """A rejected archive must leave the application just as usable as a
    successful restore does -- never paused/stopped forever."""
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "extra.zip", entries={"sneaky.txt": b"nope"},
    )
    hooks = RecordingHooks()
    with pytest.raises(restore.RestoreValidationError):
        restore.restore_backup(
            tampered, db_path=seeded.db_path, config_path=seeded.config_path,
            hooks=hooks.as_hooks(), emergency_backup_dir=seeded.tmp / "emergency",
        )
    assert hooks.calls == ["pause_worker", "stop_watcher", "start_watcher", "resume_worker"]
    # quiesce_worker is never reached -- the replace was never attempted.
    assert "quiesce_worker" not in hooks.calls


def test_an_emergency_backup_of_the_current_state_is_kept(seeded, good_archive):
    """Step 4: the user's way back to the state they just replaced. It must
    still be on disk AFTER a successful restore, and must itself be a
    valid, restorable archive of the PRE-restore state."""
    pre_restore = snapshot_db(seeded.db_path)
    emergency_dir = seeded.tmp / "emergency"

    # make the incoming archive differ from the current state
    with db.open_db(seeded.db_path) as conn:
        db.upsert_device_seen(conn, "device-only-before-restore", "Temporary")
    pre_restore = snapshot_db(seeded.db_path)

    result = restore.restore_backup(
        good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
        emergency_backup_dir=emergency_dir,
    )

    emergency = Path(result["emergency_backup_path"])
    assert emergency.is_file()
    assert emergency.parent == emergency_dir

    verdict = restore.inspect_archive(emergency)
    assert verdict["valid"] is True

    # Restoring the emergency archive really does bring back the pre-restore state.
    restore.restore_backup(
        emergency, db_path=seeded.db_path, config_path=seeded.config_path,
        emergency_backup_dir=emergency_dir,
    )
    assert snapshot_db(seeded.db_path) == pre_restore


def test_failure_at_the_replace_rolls_back_cleanly_and_state_stays_usable(seeded, good_archive, monkeypatch):
    """Simulates a failure partway through step 8 (the config replace blows
    up AFTER the database has already been swapped). The emergency backup
    must be rolled back in, leaving the application on its ORIGINAL state
    and fully usable."""
    # Make the incoming archive genuinely different from the live state, so
    # a rollback that silently did nothing would still be detectable.
    with db.open_db(seeded.db_path) as conn:
        db.upsert_device_seen(conn, "device-present-before-restore", "Before")
        db.set_device_friendly_name(conn, "device-alpha", "NAME BEFORE RESTORE")
    seeded.config_path.write_text("# config before restore\n", encoding="utf-8")

    before_db = snapshot_db(seeded.db_path)
    before_config = seeded.config_path.read_bytes()

    real_replace = restore._replace_file
    calls = {"n": 0}

    def exploding_replace(source, target):
        calls["n"] += 1
        if calls["n"] == 2:  # the DB is already in place; fail on config.yaml
            raise OSError("simulated failure midway through the replace")
        return real_replace(source, target)

    monkeypatch.setattr(restore, "_replace_file", exploding_replace)

    with pytest.raises(restore.RestoreFailed) as excinfo:
        restore.restore_backup(
            good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
            emergency_backup_dir=seeded.tmp / "emergency",
        )

    assert excinfo.value.rolled_back is True
    assert "restored from the safety backup" in str(excinfo.value)

    monkeypatch.undo()
    assert snapshot_db(seeded.db_path) == before_db
    assert seeded.config_path.read_bytes() == before_config
    assert_state_usable(seeded)


def test_no_incoming_temp_files_are_left_behind_after_a_failed_replace(seeded, good_archive, monkeypatch):
    def always_explode(source, target):
        raise OSError("nope")

    monkeypatch.setattr(restore, "_replace_file", always_explode)
    with pytest.raises(restore.RestoreFailed):
        restore.restore_backup(
            good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
            emergency_backup_dir=seeded.tmp / "emergency",
        )
    monkeypatch.undo()

    leftovers = [p.name for p in seeded.db_path.parent.iterdir() if ".restore-incoming" in p.name]
    leftovers += [p.name for p in seeded.config_path.parent.iterdir() if ".restore-incoming" in p.name]
    assert leftovers == []
    assert_state_usable(seeded)


def test_restore_leaves_no_stale_wal_sidecar_next_to_the_new_database(seeded, good_archive):
    """A `-wal` belonging to the PREVIOUS database, left next to a freshly
    restored one, is a corruption risk -- not a cosmetic leftover."""
    live = db.get_connection(seeded.db_path)
    db.init_db(live)
    db.upsert_device_seen(live, "device-makes-a-wal", "wal")
    assert Path(str(seeded.db_path) + "-wal").exists()
    live.close()

    restore.restore_backup(
        good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
        emergency_backup_dir=seeded.tmp / "emergency",
    )

    assert not Path(str(seeded.db_path) + "-wal").exists()
    assert not Path(str(seeded.db_path) + "-shm").exists()
    with db.open_db(seeded.db_path) as conn:
        assert db.get_device(conn, "device-makes-a-wal") is None  # that row was never in the backup
        assert db.get_device(conn, "device-alpha") is not None


def test_restore_runs_current_migrations_against_an_older_candidate(seeded, tmp_path):
    """Step 7: an OLDER backup's schema must be migrated forward by the
    CURRENT db.init_db(), not silently used stale. Modelled with a
    candidate database that predates UI-003's install_history columns."""
    old_db = tmp_path / "old.db"
    conn = db.get_connection(old_db)
    db.init_db(conn)
    seed_state_a(conn)
    # Roll the schema back to before UI-003 added these two columns.
    conn.execute("ALTER TABLE install_history DROP COLUMN user_verified_outcome")
    conn.execute("ALTER TABLE install_history DROP COLUMN user_verified_at")
    conn.commit()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(install_history)")}
    assert "user_verified_outcome" not in columns
    conn.close()

    old_config = tmp_path / "old-config.yaml"
    old_config.write_text("# an older config\n", encoding="utf-8")
    archive = backup.create_backup(
        tmp_path / "old-backup.zip", db_path=old_db, config_path=old_config,
    ).path

    restore.restore_backup(
        archive, db_path=seeded.db_path, config_path=seeded.config_path,
        emergency_backup_dir=seeded.tmp / "emergency",
    )

    with db.open_db(seeded.db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(install_history)")}
    assert "user_verified_outcome" in columns
    assert "user_verified_at" in columns
    assert_state_usable(seeded)


def test_restored_database_is_immediately_writable_by_the_application(seeded, good_archive):
    """Step 9/10 in practice: after a restore the app must be able to open
    the database and keep working -- including the WAL-mode connection
    db.get_connection() always makes."""
    restore.restore_backup(
        good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
        emergency_backup_dir=seeded.tmp / "emergency",
    )
    with db.open_db(seeded.db_path) as conn:
        job_id = db.create_job(
            conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
            target_device_id="device-alpha", library_item_id=db.list_library_items(conn)[0]["id"],
        )
        assert db.get_job(conn, job_id) is not None
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ---------------------------------------------------------------------------
# the real Windows file-lock scenario: a LIVE worker thread holding an open
# sqlite3 connection while the restore tries to replace that very file
# ---------------------------------------------------------------------------

def test_restore_succeeds_while_the_web_worker_thread_is_running(seeded):
    """WebContext's worker thread holds an open sqlite3 connection for its
    whole lifetime, and on Windows os.replace() over a file with an open
    handle fails outright. This is the end-to-end proof that the
    backup_service adapter genuinely quiesces that thread around the
    replace -- and brings it (and the pause flag) back afterwards.

    Exercised through backup_service.restore_from_upload(), i.e. exactly
    the path POST /api/restore takes."""
    from switchagent.web import backup_service

    ctx = build_mock_context(seeded.db_path)
    ctx.start_worker()
    try:
        assert ctx.worker_running

        state_a = snapshot_db_stable(seeded.db_path)
        archive = backup.create_backup(seeded.tmp / "live.zip", db_path=seeded.db_path).path

        with db.open_db(seeded.db_path) as conn:
            # FK-safe mutation: install_history has no dependents, and
            # library_items rows are referenced by jobs so they are updated
            # rather than deleted.
            conn.execute("DELETE FROM install_history")
            conn.execute("UPDATE library_items SET status = 'MUTATED-IN-B'")
            conn.commit()
        assert snapshot_db_stable(seeded.db_path) != state_a

        with archive.open("rb") as handle:
            result = backup_service.restore_from_upload(ctx, handle)

        assert result["restored"] is True
        assert snapshot_db_stable(seeded.db_path) == state_a
        # the friendly names the seed set really did come back too
        with db.open_db(seeded.db_path) as conn:
            names = {row["device_id"]: row["friendly_name"] for row in db.list_devices(conn)}
        assert names["device-alpha"] == "Parent's Switch"
        # the worker is back, and not left paused
        assert ctx.worker_running
        assert not ctx.worker_paused.is_set()
    finally:
        ctx.stop_worker()

    assert_state_usable(seeded)


def test_quiescing_the_worker_is_load_bearing_not_ceremonial(seeded, good_archive):
    """Negative control for the test above: with the SAME live worker
    thread but no quiesce hook, the replace must actually fail -- which is
    what proves the quiesce step is doing real work rather than being
    decoration. The failure must still roll back cleanly."""
    ctx = build_mock_context(seeded.db_path)
    ctx.start_worker()
    try:
        before = snapshot_db_stable(seeded.db_path)
        before_config = seeded.config_path.read_bytes()
        hooks = restore.RestoreRuntimeHooks(
            pause_worker=ctx.worker_paused.set,
            resume_worker=ctx.worker_paused.clear,
        )  # quiesce_worker deliberately left as the default no-op

        with pytest.raises(restore.RestoreFailed) as excinfo:
            restore.restore_backup(
                good_archive, db_path=seeded.db_path, config_path=seeded.config_path,
                hooks=hooks, emergency_backup_dir=seeded.tmp / "emergency",
            )
        # The database is the FIRST thing replaced, so its failure means
        # nothing was mutated at all -- the user must be told exactly that,
        # not handed a scary "rollback attempted" message.
        assert excinfo.value.rolled_back is True
        assert "Nothing was changed" in str(excinfo.value)
        assert snapshot_db_stable(seeded.db_path) == before
        assert seeded.config_path.read_bytes() == before_config
    finally:
        ctx.stop_worker()

    assert_state_usable(seeded)


# ---------------------------------------------------------------------------
# runtime modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app_data_layout", ["portable", "installed"])
def test_full_round_trip_at_two_different_app_data_locations(tmp_path, monkeypatch, app_data_layout):
    """restore.restore_backup() resolves db_path/config_path from
    config.DB_PATH/config.CONFIG_YAML_PATH at CALL time -- it never reads
    config.RUNTIME_MODE itself (same non-claim as
    test_backup_defaults_follow_config_paths_at_two_different_app_data_locations
    in test_backup.py -- see that test's docstring for why "portable vs
    installed" is really tests/test_paths.py's + W3-010's own claim to
    prove, not this one's). What IS proven here, twice, at two genuinely
    different example locations: the full round trip -- backup, mutate,
    restore -- works with zero explicit paths passed, wherever
    config.DB_PATH/CONFIG_YAML_PATH/APP_DATA_ROOT happen to point."""
    state = make_app_state(tmp_path, monkeypatch, runtime_mode=app_data_layout)
    with db.open_db(state.db_path) as conn:
        seed_state_a(conn)

    state_a = snapshot_db(state.db_path)
    archive = backup.create_backup(tmp_path / "out.zip").path  # defaults

    with db.open_db(state.db_path) as conn:
        conn.execute("DELETE FROM devices")
        conn.commit()
    state.config_path.write_text("# clobbered\n", encoding="utf-8")

    result = restore.restore_backup(archive)  # defaults, no explicit paths

    assert snapshot_db(state.db_path) == state_a
    # The emergency backup landed under THIS location's app data root.
    assert Path(result["emergency_backup_path"]).is_relative_to(state.root)
    assert_state_usable(state)


# ---------------------------------------------------------------------------
# inspect_archive (read-only)
# ---------------------------------------------------------------------------

def test_inspect_archive_reports_a_good_archive_without_touching_state(seeded, good_archive):
    before = snapshot_db(seeded.db_path)
    verdict = restore.inspect_archive(good_archive)
    assert verdict["valid"] is True
    assert verdict["backup_format_version"] == 1
    assert verdict["includes_config"] is True
    assert {entry["name"] for entry in verdict["files"]} == {
        backup.DB_ENTRY_NAME, backup.CONFIG_ENTRY_NAME,
    }
    assert snapshot_db(seeded.db_path) == before


def test_inspect_archive_reports_why_a_bad_archive_is_invalid(seeded, good_archive):
    tampered = rebuild_archive(
        good_archive, seeded.tmp / "extra.zip", entries={"sneaky.txt": b"nope"},
    )
    verdict = restore.inspect_archive(tampered)
    assert verdict["valid"] is False
    assert "not listed in its own manifest" in verdict["reason"]


# ---------------------------------------------------------------------------
# HTTP routes (GET /api/backup, POST /api/restore)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(seeded):
    from fastapi.testclient import TestClient

    ctx = build_mock_context(seeded.db_path)
    return TestClient(create_app(ctx)), ctx


def test_get_api_backup_streams_a_dated_zip_download(client, seeded):
    test_client, _ctx = client
    response = test_client.get("/api/backup")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "SwitchAgent-Backup-" in disposition and disposition.rstrip('"').endswith(".zip")

    downloaded = seeded.tmp / "downloaded.zip"
    downloaded.write_bytes(response.content)
    verdict = restore.inspect_archive(downloaded)
    assert verdict["valid"] is True


def test_post_api_restore_round_trips_through_http(client, seeded):
    test_client, _ctx = client
    state_a = snapshot_db(seeded.db_path)
    archive_bytes = test_client.get("/api/backup").content

    with db.open_db(seeded.db_path) as conn:
        conn.execute("DELETE FROM devices")
        conn.commit()
    assert snapshot_db(seeded.db_path) != state_a

    response = test_client.post(
        "/api/restore",
        files={"file": ("SwitchAgent-Backup-2026-09-12.zip", archive_bytes, "application/zip")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["restored"] is True
    assert snapshot_db(seeded.db_path) == state_a


def test_post_api_restore_returns_409_while_a_job_is_running(client, seeded):
    test_client, _ctx = client
    archive_bytes = test_client.get("/api/backup").content
    with db.open_db(seeded.db_path) as conn:
        db.update_job_status(conn, db.list_jobs(conn)[0]["id"], "RUNNING", started_at=db.now_iso())

    before = snapshot_db(seeded.db_path)
    response = test_client.post(
        "/api/restore", files={"file": ("backup.zip", archive_bytes, "application/zip")},
    )
    assert response.status_code == 409
    assert "RUNNING" in response.json()["detail"]
    assert snapshot_db(seeded.db_path) == before


def test_post_api_restore_returns_400_for_an_invalid_archive(client, seeded):
    test_client, _ctx = client
    before = snapshot_db(seeded.db_path)
    response = test_client.post(
        "/api/restore", files={"file": ("backup.zip", b"not a zip at all", "application/zip")},
    )
    assert response.status_code == 400
    assert "zip" in response.json()["detail"].lower()
    assert snapshot_db(seeded.db_path) == before


def test_post_api_restore_returns_400_for_an_empty_upload(client, seeded):
    before = snapshot_db(seeded.db_path)
    before_config = seeded.config_path.read_bytes()

    test_client, _ctx = client
    response = test_client.post(
        "/api/restore", files={"file": ("backup.zip", b"", "application/zip")},
    )
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()
    assert snapshot_db(seeded.db_path) == before
    assert seeded.config_path.read_bytes() == before_config


def test_settings_page_renders_the_backup_restore_section(client):
    test_client, _ctx = client
    response = test_client.get("/settings")
    assert response.status_code == 200
    assert "Backup / Restore" in response.text
    assert 'href="/api/backup"' in response.text
    assert 'id="restore-file-input"' in response.text
    assert 'id="restore-btn"' in response.text
    assert "/static/backup.js" in response.text


def test_backup_js_is_served_and_drives_the_restore_endpoint(client):
    test_client, _ctx = client
    response = test_client.get("/static/backup.js")
    assert response.status_code == 200
    text = response.text
    assert "/api/restore" in text

    # Not just "confirm( appears somewhere" -- confirm() must run BEFORE
    # the POST to /api/restore, with an early-return guard between them,
    # so declining the dialog genuinely prevents the destructive call
    # rather than merely being decorative text elsewhere in the file.
    confirm_index = text.index("confirm(")
    restore_call_index = text.index('fetch("/api/restore"')
    assert confirm_index < restore_call_index
    between = text[confirm_index:restore_call_index]
    assert "if (!confirmed)" in between
    assert "return" in between.split("if (!confirmed)", 1)[1].split("}")[0]


def test_backup_route_does_not_leave_temp_directories_behind(client, seeded):
    import tempfile

    test_client, _ctx = client
    temp_root = Path(tempfile.gettempdir())
    before = {p.name for p in temp_root.glob("switchagent-backup-dl-*")}
    assert test_client.get("/api/backup").status_code == 200
    after = {p.name for p in temp_root.glob("switchagent-backup-dl-*")}
    assert after <= before
