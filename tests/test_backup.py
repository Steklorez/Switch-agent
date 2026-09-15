"""Tests for W3-005's backup creation side (switchagent/backup.py).

Covers: the archive is structurally correct and self-describing; the
manifest's checksums/sizes actually match the stored bytes; the DB snapshot
is a CONSISTENT snapshot of a live WAL-mode database (not a raw byte copy);
game payloads/work/logs/update-cache are excluded by construction; and the
filename/paths follow config.* in every runtime mode.

Also hosts the shared isolated-app-state fixture and the state-A seeding
helpers that tests/test_restore.py imports -- same cross-test-module reuse
tests/test_queue_worker.py already does with test_stage41_recovery.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from switchagent import backup, config, db, known_folders

_CONFIG_YAML = """\
# SwitchAgent configuration -- test fixture.
extraction:
  max_extracted_size_bytes: 40000000000
  max_file_count: 20000
  max_directory_depth: 24

library:
  source_dir: "D:\\\\shared\\\\Download"
"""


# ---------------------------------------------------------------------------
# Shared fixtures / helpers (also used by tests/test_restore.py)
# ---------------------------------------------------------------------------

def make_app_state(tmp_path, monkeypatch, *, runtime_mode: str = "dev") -> SimpleNamespace:
    """An isolated APP_DATA_ROOT layout, with every config.* path attribute
    monkeypatched at it -- the same plain-module-attribute patching
    tests/conftest.py's isolated_db and tests/test_web_api.py's web_ctx
    fixtures already use.

    `runtime_mode` exercises the fact that backup/restore read
    config.DB_PATH/config.CONFIG_YAML_PATH at CALL time: in "portable" mode
    APP_DATA_ROOT is the executable's own directory, in "installed" mode it
    is %LOCALAPPDATA%\\SwitchAgent (see switchagent/paths.py). Both are
    modelled here as a different APP_DATA_ROOT layout -- no real frozen EXE
    or packaged build is needed to prove the path logic follows it."""
    if runtime_mode == "portable":
        app_root = tmp_path / "PortableApp"       # next to the "executable"
    elif runtime_mode == "installed":
        app_root = tmp_path / "LocalAppData" / "SwitchAgent"
    else:
        app_root = tmp_path / "project"

    data_dir = app_root / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "switchagent.db"
    config_path = app_root / "config.yaml"
    library_dir = tmp_path / "library"
    library_dir.mkdir()

    monkeypatch.setattr(config, "APP_DATA_ROOT", app_root)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config, "WORK_DIR", app_root / "work")
    monkeypatch.setattr(config, "LOGS_DIR", app_root / "logs")
    monkeypatch.setattr(config, "INBOX_DIR", app_root / "inbox")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "RUNTIME_MODE", runtime_mode)
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)

    config_path.write_text(_CONFIG_YAML, encoding="utf-8")
    with db.open_db(db_path):
        pass  # create the schema once

    return SimpleNamespace(
        root=app_root, db_path=db_path, config_path=config_path,
        library_dir=library_dir, tmp=tmp_path, runtime_mode=runtime_mode,
    )


@pytest.fixture
def app_state(tmp_path, monkeypatch):
    return make_app_state(tmp_path, monkeypatch)


def add_noise(state: SimpleNamespace) -> dict[str, Path]:
    """Heavy/sensitive things that live under the SAME app data root and
    must never end up inside a backup: staged game payloads, logs, the
    inbox, and the update-check cache."""
    paths = {
        "staged_payload": state.root / "work" / "job-1" / "Game.nsp",
        "log": state.root / "logs" / "switchagent.log",
        "inbox_payload": state.root / "inbox" / "Dropped.nsz",
        "update_cache": state.root / "update_check_cache.json",
        "library_payload": state.library_dir / "Game [0100000000010000][v0].nsp",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PAYLOAD" * 100)
    return paths


def seed_state_a(conn) -> dict:
    """A realistic, cross-table application state: library + inbox items,
    a batch, jobs in several statuses, devices with friendly names,
    UI-007 storage mappings, and install_history rows including UI-003's
    user_verified_outcome/user_verified_at."""
    db.upsert_device_seen(conn, "device-alpha", "Nintendo Switch")
    db.upsert_device_seen(conn, "device-beta", "Nintendo Switch")
    db.set_device_friendly_name(conn, "device-alpha", "Parent's Switch")
    db.set_device_friendly_name(conn, "device-beta", "Child's Switch")

    db.set_device_storage_mapping(conn, "device-alpha", "SD Card", "SD_CARD")
    db.set_device_storage_mapping(conn, "device-alpha", "Install to SD", "SD_INSTALL")
    db.set_device_storage_mapping(conn, "device-beta", "SD Card", "SD_CARD")

    lib_a = db.upsert_library_item(
        conn, absolute_path="D:\\shared\\Download\\Alpha [0100000000010000][v0].nsp",
        item_type="FILE", file_type="NSP", size=1234, mtime=111.0,
        content_hash="hash-alpha", title_id="0100000000010000",
        title_id_source="filename", status="AVAILABLE",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        content_type="GAME_PACKAGE", package_format="NSP", title_id_confident=True,
    )
    lib_b = db.upsert_library_item(
        conn, absolute_path="D:\\shared\\Download\\Beta [0100000000020000][v0].nsz",
        item_type="FILE", file_type="NSZ", size=4321, mtime=222.0,
        content_hash="hash-beta", title_id="0100000000020000",
        title_id_source="filename", status="AVAILABLE",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        content_type="GAME_PACKAGE", package_format="NSZ",
    )
    inbox_a = db.upsert_inbox_item(
        conn, relative_path="Dropped.nsz", item_type="FILE", file_type="NSZ",
        size=999, mtime=333.0, content_hash="hash-inbox", title_id="0100000000030000",
        title_id_source="filename", status="ANALYZED",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )

    batch_id = db.create_installation_batch(conn, target_device_id="device-alpha")

    job_done = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
        target_device_id="device-alpha", library_item_id=lib_a, batch_id=batch_id,
    )
    db.update_job_status(conn, job_done, "DONE_UNVERIFIED", finished_at=db.now_iso(), bytes_done=1234)

    job_pending = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
        target_device_id="device-beta", library_item_id=lib_b,
    )

    job_inbox = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
        target_device_id="device-alpha", inbox_item_id=inbox_a,
    )
    db.update_job_status(conn, job_inbox, "FAILED", error="device went away", finished_at=db.now_iso())

    history_verified = db.record_install_history(
        conn, job_id=job_done, title_id="0100000000010000", display_name="Alpha",
        target_device_id="device-alpha", target_storage="SD_INSTALL",
        outcome="DONE_UNVERIFIED", bytes_total=1234,
    )
    db.set_user_verified_outcome(conn, history_verified, "SUCCESS")

    history_failed = db.record_install_history(
        conn, job_id=job_inbox, title_id="0100000000030000", display_name="Dropped",
        target_device_id="device-alpha", target_storage="SD_INSTALL",
        outcome="FAILED", error="device went away", bytes_total=999,
    )

    return {
        "library_item_ids": [lib_a, lib_b], "inbox_item_id": inbox_a, "batch_id": batch_id,
        "job_ids": [job_done, job_pending, job_inbox],
        "history_verified_id": history_verified, "history_failed_id": history_failed,
    }


# Every user-visible table, dumped in a stable order -- the comparison unit
# for "the state on disk is exactly state A again" (see test_restore.py's
# end-to-end proof). job_log is included too: a restore that silently lost
# the audit trail would still be a lossy restore.
_SNAPSHOT_TABLES = (
    "inbox_items", "library_items", "jobs", "job_log", "devices",
    "install_history", "installation_batches", "device_storage_mappings",
)


def snapshot_db(db_path: Path) -> dict[str, list[tuple]]:
    """Reads every row of every user-visible table straight off disk,
    through a brand-new connection -- deliberately not through any
    connection a test already holds, so it proves what is actually
    persisted rather than what some cache believes."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result: dict[str, list[tuple]] = {}
        for table in _SNAPSHOT_TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            # key=repr, not natural ordering: rows mix None and int in the
            # same column, which is not orderable in Python 3.
            result[table] = sorted(
                (tuple(sorted(dict(row).items())) for row in rows), key=repr,
            )
        return result
    finally:
        conn.close()


def read_manifest(archive_path: Path) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        return json.loads(archive.read(backup.MANIFEST_ENTRY_NAME).decode("utf-8"))


# ---------------------------------------------------------------------------
# archive structure
# ---------------------------------------------------------------------------

def test_backup_produces_a_readable_zip_with_exactly_the_expected_entries(app_state):
    with db.open_db(app_state.db_path) as conn:
        seed_state_a(conn)
    add_noise(app_state)

    result = backup.create_backup(app_state.tmp / "out.zip")

    assert result.path.is_file()
    assert result.size_bytes > 0
    with zipfile.ZipFile(result.path) as archive:
        assert sorted(archive.namelist()) == sorted([
            backup.DB_ENTRY_NAME, backup.CONFIG_ENTRY_NAME, backup.MANIFEST_ENTRY_NAME,
        ])
        assert archive.testzip() is None


def test_backup_filename_is_the_dated_switchagent_pattern():
    stamped = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)
    assert backup.backup_filename(stamped) == "SwitchAgent-Backup-2026-09-12.zip"


def test_created_backup_reports_the_dated_filename(app_state):
    stamped = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    result = backup.create_backup(app_state.tmp / "out.zip", now=stamped)
    assert result.filename == "SwitchAgent-Backup-2025-01-02.zip"


def test_manifest_has_every_required_field(app_state):
    from switchagent import __version__

    stamped = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)
    result = backup.create_backup(app_state.tmp / "out.zip", now=stamped)
    manifest = read_manifest(result.path)

    assert manifest["backup_format_version"] == 1
    assert manifest["app_version"] == __version__
    assert manifest["created_at"] == "2026-09-12T08:30:00+00:00"
    # Parses as ISO 8601 and really is UTC.
    parsed = datetime.fromisoformat(manifest["created_at"])
    assert parsed.utcoffset().total_seconds() == 0

    names = {entry["name"] for entry in manifest["files"]}
    assert names == {backup.DB_ENTRY_NAME, backup.CONFIG_ENTRY_NAME}
    for entry in manifest["files"]:
        assert isinstance(entry["size"], int) and entry["size"] > 0
        assert len(entry["sha256"]) == 64
    assert manifest["size"] == sum(entry["size"] for entry in manifest["files"])


def test_manifest_checksums_and_sizes_match_the_stored_bytes(app_state):
    import hashlib

    with db.open_db(app_state.db_path) as conn:
        seed_state_a(conn)

    result = backup.create_backup(app_state.tmp / "out.zip")
    manifest = read_manifest(result.path)

    with zipfile.ZipFile(result.path) as archive:
        for entry in manifest["files"]:
            payload = archive.read(entry["name"])
            assert len(payload) == entry["size"], entry["name"]
            assert hashlib.sha256(payload).hexdigest() == entry["sha256"], entry["name"]


def test_manifest_json_is_not_listed_in_its_own_files_list(app_state):
    """It cannot checksum itself -- restore.py allows exactly this one
    unlisted entry and nothing else."""
    result = backup.create_backup(app_state.tmp / "out.zip")
    manifest = read_manifest(result.path)
    assert backup.MANIFEST_ENTRY_NAME not in {entry["name"] for entry in manifest["files"]}


def test_backup_entries_are_plain_regular_files_not_symlinks_or_directories(app_state):
    import stat as stat_mod

    result = backup.create_backup(app_state.tmp / "out.zip")
    with zipfile.ZipFile(result.path) as archive:
        for info in archive.infolist():
            mode = info.external_attr >> 16
            assert not info.is_dir()
            assert stat_mod.S_ISREG(mode), info.filename
            assert not stat_mod.S_ISLNK(mode), info.filename


# ---------------------------------------------------------------------------
# what is NOT in a backup
# ---------------------------------------------------------------------------

def test_backup_excludes_game_payloads_work_logs_and_update_cache(app_state):
    """The heavy/irrelevant things that share APP_DATA_ROOT must never be
    in the archive -- excluded by construction (backup.py only ever writes
    two explicitly-passed payload paths), proven here on real files."""
    noise = add_noise(app_state)
    result = backup.create_backup(app_state.tmp / "out.zip")

    with zipfile.ZipFile(result.path) as archive:
        names = archive.namelist()
        blob = b"".join(archive.read(name) for name in names)

    assert names == [backup.DB_ENTRY_NAME, backup.CONFIG_ENTRY_NAME, backup.MANIFEST_ENTRY_NAME]
    for label, path in noise.items():
        assert path.name not in names, label
    # And none of the payload bytes leaked in under some other entry name.
    assert b"PAYLOAD" not in blob
    # A backup is small: nothing payload-sized got in.
    assert result.size_bytes < 200_000


# ---------------------------------------------------------------------------
# snapshot consistency -- the reason this module exists
# ---------------------------------------------------------------------------

def test_db_snapshot_is_self_contained_and_needs_no_wal_sidecar(app_state, tmp_path):
    """A raw byte copy of a live WAL database is not a database: recent
    commits can live only in the `-wal` sidecar. The snapshot inside the
    archive must be readable entirely on its own."""
    conn = db.get_connection(app_state.db_path)
    db.init_db(conn)
    try:
        seed_state_a(conn)
        assert Path(str(app_state.db_path) + "-wal").exists(), "fixture should be in WAL mode"

        result = backup.create_backup(tmp_path / "out.zip")
    finally:
        conn.close()

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(result.path) as archive:
        archive.extract(backup.DB_ENTRY_NAME, extracted)

    snapshot_path = extracted / backup.DB_ENTRY_NAME
    assert not Path(str(snapshot_path) + "-wal").exists()

    check = sqlite3.connect(snapshot_path)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 2
        assert check.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 2
        assert check.execute("SELECT COUNT(*) FROM install_history").fetchone()[0] == 2
    finally:
        check.close()


def test_snapshot_excludes_a_concurrently_open_uncommitted_transaction(app_state, tmp_path):
    """Proves the SQLite online backup API is genuinely being used: an
    UNCOMMITTED row held open by another live connection must not appear in
    the snapshot, and taking the snapshot must not deadlock against it."""
    writer = db.get_connection(app_state.db_path)
    db.init_db(writer)
    try:
        db.upsert_device_seen(writer, "device-committed", "Committed")
        writer.execute(
            "INSERT INTO devices (device_id, friendly_name, last_known_display_name, "
            "first_seen_at, last_seen_at) VALUES ('device-uncommitted', NULL, 'Ghost', ?, ?)",
            (db.now_iso(), db.now_iso()),
        )  # deliberately NOT committed

        result = backup.create_backup(tmp_path / "out.zip")
    finally:
        writer.rollback()
        writer.close()

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(result.path) as archive:
        archive.extract(backup.DB_ENTRY_NAME, extracted)

    check = sqlite3.connect(extracted / backup.DB_ENTRY_NAME)
    try:
        ids = {row[0] for row in check.execute("SELECT device_id FROM devices")}
    finally:
        check.close()
    assert "device-committed" in ids
    assert "device-uncommitted" not in ids


def test_snapshot_is_not_a_raw_byte_copy_of_the_live_file(app_state, tmp_path):
    """Guard against the implementation regressing to shutil.copy2: a live
    WAL database's own bytes and a proper snapshot of it differ (the
    snapshot has the WAL's committed content folded in) -- AND the
    snapshot is independently proven to be a genuine, queryable copy of
    the real data (not merely "different bytes", which a truncated,
    empty, or garbage file would also satisfy)."""
    conn = db.get_connection(app_state.db_path)
    db.init_db(conn)
    try:
        seed_state_a(conn)
        live_bytes = app_state.db_path.read_bytes()
        result = backup.create_backup(tmp_path / "out.zip")
    finally:
        conn.close()

    with zipfile.ZipFile(result.path) as archive:
        snapshot_bytes = archive.read(backup.DB_ENTRY_NAME)
    assert snapshot_bytes != live_bytes

    snapshot_db_path = tmp_path / "extracted_snapshot.db"
    snapshot_db_path.write_bytes(snapshot_bytes)
    snap_conn = sqlite3.connect(snapshot_db_path)
    try:
        row = snap_conn.execute(
            "SELECT friendly_name FROM devices WHERE device_id = 'device-alpha'"
        ).fetchone()
        assert row == ("Parent's Switch",)
        table_count = snap_conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        assert table_count > 1  # a real schema, not a zero-length/garbage file
    finally:
        snap_conn.close()


# ---------------------------------------------------------------------------
# paths / runtime modes / edge cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app_data_layout", ["portable", "installed"])
def test_backup_defaults_follow_config_paths_at_two_different_app_data_locations(tmp_path, monkeypatch, app_data_layout):
    """backup.create_backup() resolves db_path/config_path from
    config.DB_PATH/config.CONFIG_YAML_PATH at CALL time -- it never reads
    config.RUNTIME_MODE itself, so this test (unlike its name used to
    imply) does NOT independently re-prove portable-vs-installed
    runtime-mode resolution; that real resolution chain
    (sys.frozen/portable.flag -> switchagent/paths.py) is tested on its
    own in tests/test_paths.py, and exercised live end-to-end (a real
    packaged EXE, both portable and installed-style layouts) by W3-010's
    own runtime checks. What this test DOES prove: backup.create_backup()
    isn't hardcoded to one particular directory shape -- it correctly
    follows config.DB_PATH/CONFIG_YAML_PATH to two structurally different
    example locations (one shaped like a portable layout, one shaped
    like an installed one), which is the actual, narrower claim its own
    assertions below back up."""
    state = make_app_state(tmp_path, monkeypatch, runtime_mode=app_data_layout)
    with db.open_db(state.db_path) as conn:
        seed_state_a(conn)

    result = backup.create_backup(tmp_path / "out.zip")  # no explicit paths

    manifest = read_manifest(result.path)
    assert {entry["name"] for entry in manifest["files"]} == {
        backup.DB_ENTRY_NAME, backup.CONFIG_ENTRY_NAME,
    }
    with zipfile.ZipFile(result.path) as archive:
        assert archive.read(backup.CONFIG_ENTRY_NAME) == state.config_path.read_bytes()


def test_backup_without_a_config_yaml_omits_it_rather_than_storing_an_empty_one(app_state):
    app_state.config_path.unlink()
    result = backup.create_backup(app_state.tmp / "out.zip")

    with zipfile.ZipFile(result.path) as archive:
        assert backup.CONFIG_ENTRY_NAME not in archive.namelist()
    manifest = read_manifest(result.path)
    assert {entry["name"] for entry in manifest["files"]} == {backup.DB_ENTRY_NAME}


def test_backup_with_no_database_fails_loudly(app_state):
    app_state.db_path.unlink()
    with pytest.raises(backup.BackupError, match="database not found"):
        backup.create_backup(app_state.tmp / "out.zip")


def test_failed_backup_leaves_no_partial_file_at_the_destination(app_state):
    app_state.db_path.unlink()
    destination = app_state.tmp / "out.zip"
    with pytest.raises(backup.BackupError):
        backup.create_backup(destination)
    assert not destination.exists()


def test_backup_leaves_no_staging_directory_behind(app_state):
    destination = app_state.tmp / "nested" / "out.zip"
    backup.create_backup(destination)
    assert sorted(p.name for p in destination.parent.iterdir()) == ["out.zip"]


def test_two_backups_of_the_same_state_have_identical_payload_checksums(app_state):
    with db.open_db(app_state.db_path) as conn:
        seed_state_a(conn)

    stamped = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)
    first = read_manifest(backup.create_backup(app_state.tmp / "a.zip", now=stamped).path)
    second = read_manifest(backup.create_backup(app_state.tmp / "b.zip", now=stamped).path)
    assert first == second


def test_backup_does_not_disturb_the_live_database(app_state):
    with db.open_db(app_state.db_path) as conn:
        seed_state_a(conn)
    before = snapshot_db(app_state.db_path)

    backup.create_backup(app_state.tmp / "out.zip")

    assert snapshot_db(app_state.db_path) == before
