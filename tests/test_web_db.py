"""Tests for the Web UI's DB additions to switchagent/db.py:
library_items, devices, install_history, and the jobs-table rebuild that
lets a job be created from either an inbox_items row or a library_items
row (see SCHEMA's comments in db.py and docs/WEB-UI.md).
"""

from __future__ import annotations

import sqlite3

import pytest

from switchagent import db


def _add_library_item(conn, path: str, *, content_hash: str = "h1", status: str = "AVAILABLE") -> int:
    return db.upsert_library_item(
        conn, absolute_path=path, item_type="FILE", file_type="NSP", size=100, mtime=0.0,
        content_hash=content_hash, title_id="0100000000010000", title_id_source="filename",
        status=status, suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )


# ---------------------------------------------------------------------------
# library_items
# ---------------------------------------------------------------------------

def test_library_item_upsert_inserts_then_updates_in_place(isolated_db):
    conn, _inbox_dir = isolated_db
    path = r"D:\shared\Download\Game.nsp"
    item_id = _add_library_item(conn, path)

    row = db.get_library_item(conn, path)
    assert row["id"] == item_id
    assert row["size"] == 100

    second_id = db.upsert_library_item(
        conn, absolute_path=path, item_type="FILE", file_type="NSP", size=999, mtime=1.0,
        content_hash="h1", title_id="0100000000010000", title_id_source="filename",
        status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    assert second_id == item_id  # same identity, updated in place
    assert db.get_library_item(conn, path)["size"] == 999
    assert len(db.list_library_items(conn)) == 1


def test_library_items_delete_missing_keeps_present(isolated_db):
    conn, _inbox_dir = isolated_db
    _add_library_item(conn, r"D:\shared\Download\A.nsp", content_hash="ha")
    _add_library_item(conn, r"D:\shared\Download\B.nsp", content_hash="hb")

    removed = db.delete_library_items_missing_from(conn, {r"D:\shared\Download\A.nsp"})
    assert removed == 1
    remaining = [r["absolute_path"] for r in db.list_library_items(conn)]
    assert remaining == [r"D:\shared\Download\A.nsp"]


def test_delete_missing_library_item_referenced_by_a_job_does_not_crash(isolated_db):
    """Regression test for a real bug found 2026-09-11 while building the
    Web UI's 'Retry installation' feature (see tests/test_web_api.py's
    retry tests + STATE.md's Phase 6/8 notes): jobs.library_item_id has a
    bare REFERENCES with no ON DELETE clause, and PRAGMA foreign_keys=ON
    is set for normal connections (db.py ~line 223) -- so hard-deleting a
    library_items row that any job has ever referenced (the common case
    for anything the user actually tried to install) used to raise
    sqlite3.IntegrityError, crashing every Rescan after a previously-
    installed game's file was removed from the Library folder, forever
    (the stale row could never be cleaned up). Fixed: such a row is now
    marked RETIRED instead of deleted -- kept (for Queue/History
    display-name lookups) but no longer AVAILABLE/installable."""
    conn, _inbox_dir = isolated_db
    item_id = _add_library_item(conn, r"D:\shared\Download\A.nsp", content_hash="ha")
    db.create_job(conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
                  target_device_id="mock-switch-parent", library_item_id=item_id)

    removed = db.delete_library_items_missing_from(conn, set())  # the file is gone from disk
    assert removed == 0  # not actually deletable -- a job still references it

    row = db.get_library_item_by_id(conn, item_id)
    assert row is not None  # kept, not hard-deleted
    assert row["status"] == db.LIBRARY_ITEM_RETIRED
    assert "no longer found on disk" in row["error"]


def test_delete_missing_library_items_mixed_referenced_and_unreferenced(isolated_db):
    """One stale row with a job pointing to it, one without, in the SAME
    call -- proves the fix handles both cases correctly together, not
    just the single-row case."""
    conn, _inbox_dir = isolated_db
    referenced_id = _add_library_item(conn, r"D:\shared\Download\A.nsp", content_hash="ha")
    unreferenced_id = _add_library_item(conn, r"D:\shared\Download\B.nsp", content_hash="hb")
    db.create_job(conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
                  target_device_id="mock-switch-parent", library_item_id=referenced_id)

    removed = db.delete_library_items_missing_from(conn, set())
    assert removed == 1  # only the truly-unreferenced one counts

    assert db.get_library_item_by_id(conn, unreferenced_id) is None  # gone
    kept = db.get_library_item_by_id(conn, referenced_id)
    assert kept is not None and kept["status"] == db.LIBRARY_ITEM_RETIRED  # kept, as a record


def test_find_other_library_item_with_hash_detects_duplicate(isolated_db):
    conn, _inbox_dir = isolated_db
    _add_library_item(conn, r"D:\shared\Download\A.nsp", content_hash="same-hash")
    dup = db.find_other_library_item_with_hash(conn, "same-hash", r"D:\shared\Download\B.nsp")
    assert dup is not None
    assert dup["absolute_path"] == r"D:\shared\Download\A.nsp"

    assert db.find_other_library_item_with_hash(conn, "no-such-hash", r"D:\shared\Download\B.nsp") is None


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------

def test_upsert_device_seen_creates_then_refreshes(isolated_db):
    conn, _inbox_dir = isolated_db
    db.upsert_device_seen(conn, "dev-a", "Switch")
    row = db.get_device(conn, "dev-a")
    assert row["last_known_display_name"] == "Switch"
    assert row["friendly_name"] is None
    first_seen = row["first_seen_at"]

    db.upsert_device_seen(conn, "dev-a", "Switch (renamed on-console)")
    row2 = db.get_device(conn, "dev-a")
    assert row2["last_known_display_name"] == "Switch (renamed on-console)"
    assert row2["first_seen_at"] == first_seen  # first_seen_at never moves


def test_set_friendly_name_never_touches_device_id(isolated_db):
    conn, _inbox_dir = isolated_db
    db.upsert_device_seen(conn, "dev-a", "Switch")
    db.set_device_friendly_name(conn, "dev-a", "Моя Switch")
    row = db.get_device(conn, "dev-a")
    assert row["device_id"] == "dev-a"
    assert row["friendly_name"] == "Моя Switch"


def test_set_friendly_name_for_unknown_device_raises(isolated_db):
    conn, _inbox_dir = isolated_db
    with pytest.raises(ValueError):
        db.set_device_friendly_name(conn, "never-seen", "X")


def test_list_devices_orders_most_recently_seen_first(isolated_db):
    conn, _inbox_dir = isolated_db
    db.upsert_device_seen(conn, "dev-a", "Switch A")
    db.upsert_device_seen(conn, "dev-b", "Switch B")
    db.upsert_device_seen(conn, "dev-a", "Switch A")  # refresh -- A is now most recent
    ids = [r["device_id"] for r in db.list_devices(conn)]
    assert ids[0] == "dev-a"


# ---------------------------------------------------------------------------
# jobs: created from library_items, exactly-one-source enforcement
# ---------------------------------------------------------------------------

def test_create_job_requires_exactly_one_source(isolated_db):
    conn, _inbox_dir = isolated_db
    with pytest.raises(ValueError):
        db.create_job(conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL", target_device_id="dev-a")


def test_create_job_rejects_both_sources_at_once(isolated_db):
    conn, _inbox_dir = isolated_db
    item_id = _add_library_item(conn, r"D:\shared\Download\A.nsp")
    with pytest.raises(ValueError):
        db.create_job(
            conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL", target_device_id="dev-a",
            inbox_item_id=1, library_item_id=item_id,
        )


def test_create_job_from_library_item_only(isolated_db):
    conn, _inbox_dir = isolated_db
    item_id = _add_library_item(conn, r"D:\shared\Download\A.nsp")
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL", target_device_id="dev-a",
        library_item_id=item_id,
    )
    row = db.get_job(conn, job_id)
    assert row["library_item_id"] == item_id
    assert row["inbox_item_id"] is None


def test_find_existing_job_for_hash_sees_library_sourced_jobs(isolated_db):
    conn, _inbox_dir = isolated_db
    item_id = _add_library_item(conn, r"D:\shared\Download\A.nsp", content_hash="shared-hash")
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL", target_device_id="dev-a",
        library_item_id=item_id,
    )
    found = db.find_existing_job_for_hash(conn, "shared-hash")
    assert found is not None
    assert found["id"] == job_id


# ---------------------------------------------------------------------------
# install_history
# ---------------------------------------------------------------------------

def test_record_and_list_install_history(isolated_db):
    conn, _inbox_dir = isolated_db
    item_id = _add_library_item(conn, r"D:\shared\Download\A.nsp")
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL", target_device_id="dev-a",
        library_item_id=item_id,
    )
    db.record_install_history(
        conn, job_id=job_id, title_id="0100000000010000", display_name="Game A",
        target_device_id="dev-a", target_storage="SD_INSTALL", outcome="DONE_UNVERIFIED",
        bytes_total=100,
    )
    history = db.list_install_history(conn)
    assert len(history) == 1
    assert history[0]["outcome"] == "DONE_UNVERIFIED"
    assert history[0]["display_name"] == "Game A"


# ---------------------------------------------------------------------------
# jobs table rebuild migration -- must preserve pre-existing data from a
# database created before library_item_id existed.
# ---------------------------------------------------------------------------

_OLD_SCHEMA = """
CREATE TABLE inbox_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path   TEXT NOT NULL UNIQUE,
    item_type       TEXT NOT NULL CHECK(item_type IN ('FILE', 'MOD_FOLDER')),
    file_type       TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    content_hash    TEXT,
    title_id        TEXT,
    title_id_source TEXT,
    status          TEXT NOT NULL DEFAULT 'NEW',
    suggested_action TEXT,
    suggested_target TEXT,
    note            TEXT,
    error           TEXT,
    first_seen_at   TEXT NOT NULL,
    last_scanned_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_item_id   INTEGER NOT NULL REFERENCES inbox_items(id),
    action          TEXT NOT NULL,
    target_storage  TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING_CONFIRM',
    bytes_total     INTEGER,
    bytes_done      INTEGER DEFAULT 0,
    attempt_count   INTEGER DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    target_device_id TEXT,
    manifest_path   TEXT
);

CREATE TABLE job_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs(id),
    ts      TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def _build_old_shape_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO inbox_items (id, relative_path, item_type, file_type, size, mtime, status, "
        "first_seen_at, last_scanned_at) VALUES (1, 'game.nsp', 'FILE', 'NSP', 10, 0.0, 'ANALYZED', 't', 't')"
    )
    conn.execute(
        "INSERT INTO jobs (id, inbox_item_id, action, target_storage, status, target_device_id, "
        "manifest_path, created_at) VALUES (1, 1, 'INSTALL_VIA_DBI', 'SD_INSTALL', 'DONE', 'dev-old', "
        "'work/job-1/manifest.json', 't0')"
    )
    conn.execute("INSERT INTO job_log (id, job_id, ts, message) VALUES (1, 1, 't0', 'created')")
    conn.commit()
    conn.close()


def test_jobs_table_migration_preserves_pre_existing_rows_and_fk(tmp_path):
    db_path = tmp_path / "old.db"
    _build_old_shape_db(db_path)

    conn = db.get_connection(db_path)
    db.init_db(conn)  # runs _migrate_jobs_table_for_library_support

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "library_item_id" in cols

    old_job = db.get_job(conn, 1)
    assert old_job["inbox_item_id"] == 1
    assert old_job["library_item_id"] is None
    assert old_job["action"] == "INSTALL_VIA_DBI"
    assert old_job["status"] == "DONE"
    assert old_job["target_device_id"] == "dev-old"
    assert old_job["manifest_path"] == "work/job-1/manifest.json"

    # job_log's FK survives the rebuild (same table name after rename).
    log = db.list_job_log(conn, 1)
    assert len(log) == 1
    assert log[0]["message"] == "created"

    # A brand-new library-sourced job can now be created without violating
    # anything left over from the old shape.
    item_id = _add_library_item(conn, r"D:\shared\Download\New.nsp")
    new_job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL", target_device_id="dev-a",
        library_item_id=item_id,
    )
    assert db.get_job(conn, new_job_id)["library_item_id"] == item_id
    conn.close()


def test_jobs_table_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "old2.db"
    _build_old_shape_db(db_path)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    db.init_db(conn)  # second call must be a safe no-op, not an error
    assert db.get_job(conn, 1)["status"] == "DONE"
    conn.close()


def test_connection_survives_being_used_from_a_different_thread(tmp_path):
    """Regression guard for a real crash (docs/STATE.md's Quake II
    investigation): switchagent/web/app.py's get_conn dependency creates
    the connection in one sync FastAPI dependency call and hands it to
    the route handler, which uses it in a SEPARATE sync call -- two
    independent run_in_threadpool() hops that anyio's thread pool does
    not guarantee land on the same OS thread. A connection from
    get_connection() must not raise sqlite3's default
    "created in a thread, used in another" ProgrammingError."""
    import threading

    db_path = tmp_path / "cross_thread.db"
    conn = db.get_connection(db_path)
    db.init_db(conn)

    result: dict = {}

    def use_from_other_thread():
        try:
            conn.execute("SELECT 1").fetchone()
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001 -- capturing for the assertion below
            result["ok"] = False
            result["error"] = str(exc)

    t = threading.Thread(target=use_from_other_thread)
    t.start()
    t.join()
    conn.close()

    assert result.get("ok"), result.get("error")


# ---------------------------------------------------------------------------
# UI-001..UI-008 foundation migration: batch_id/last_progress_at on jobs,
# user_verified_outcome/_at on install_history, and the two brand-new
# tables (installation_batches, device_storage_mappings) -- must all
# upgrade a real pre-existing database (today's actual shape, i.e.
# AFTER the library_item_id rebuild but BEFORE this migration) in place,
# preserving every existing row untouched.
# ---------------------------------------------------------------------------

_PRE_UI_ROADMAP_SCHEMA = """
CREATE TABLE inbox_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path   TEXT NOT NULL UNIQUE,
    item_type       TEXT NOT NULL CHECK(item_type IN ('FILE', 'MOD_FOLDER')),
    file_type       TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    content_hash    TEXT,
    title_id        TEXT,
    title_id_source TEXT,
    status          TEXT NOT NULL DEFAULT 'NEW',
    suggested_action TEXT,
    suggested_target TEXT,
    note            TEXT,
    error           TEXT,
    first_seen_at   TEXT NOT NULL,
    last_scanned_at TEXT NOT NULL
);

CREATE TABLE library_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    absolute_path       TEXT NOT NULL UNIQUE,
    item_type           TEXT NOT NULL CHECK(item_type IN ('FILE', 'MOD_FOLDER')),
    file_type           TEXT NOT NULL,
    content_type        TEXT,
    package_format      TEXT,
    size                INTEGER NOT NULL,
    mtime               REAL NOT NULL,
    content_hash        TEXT,
    title_id            TEXT,
    title_id_source     TEXT,
    title_id_confident  INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'AVAILABLE',
    suggested_action    TEXT,
    suggested_target    TEXT,
    note                TEXT,
    error               TEXT,
    details_json        TEXT,
    first_seen_at       TEXT NOT NULL,
    last_scanned_at     TEXT NOT NULL
);

CREATE TABLE jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_item_id    INTEGER REFERENCES inbox_items(id),
    library_item_id  INTEGER REFERENCES library_items(id),
    action           TEXT NOT NULL,
    target_storage   TEXT,
    status           TEXT NOT NULL DEFAULT 'PENDING_CONFIRM',
    bytes_total      INTEGER,
    bytes_done       INTEGER DEFAULT 0,
    attempt_count    INTEGER DEFAULT 0,
    error            TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    target_device_id TEXT,
    manifest_path    TEXT,
    CHECK ((inbox_item_id IS NOT NULL) + (library_item_id IS NOT NULL) = 1)
);

CREATE TABLE job_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs(id),
    ts      TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE devices (
    device_id                TEXT PRIMARY KEY,
    friendly_name            TEXT,
    last_known_display_name  TEXT,
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL
);

CREATE TABLE install_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER NOT NULL REFERENCES jobs(id),
    title_id           TEXT,
    display_name       TEXT NOT NULL,
    target_device_id   TEXT NOT NULL,
    target_storage     TEXT,
    outcome            TEXT NOT NULL,
    error              TEXT,
    bytes_total        INTEGER,
    created_at         TEXT NOT NULL
);
"""


def _build_pre_ui_roadmap_db(path) -> int:
    """Builds a DB in exactly the shape this project had immediately
    before the UI-001..UI-008 foundation migration (library_item_id
    already exists, batch_id/last_progress_at/user_verified_*/
    device_storage_mappings/installation_batches do not). Returns the
    pre-existing job id, for the caller to assert on after migrating."""
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_UI_ROADMAP_SCHEMA)
    conn.execute(
        "INSERT INTO library_items (id, absolute_path, item_type, file_type, size, mtime, status, "
        "first_seen_at, last_scanned_at) VALUES (1, 'D:\\Download\\Game.nsp', 'FILE', 'NSP', 10, 0.0, "
        "'AVAILABLE', 't', 't')"
    )
    conn.execute(
        "INSERT INTO jobs (id, library_item_id, action, target_storage, status, target_device_id, "
        "manifest_path, created_at) VALUES (1, 1, 'INSTALL_VIA_DBI', 'SD_INSTALL', 'DONE_UNVERIFIED', "
        "'dev-old', 'work/job-1/manifest.json', 't0')"
    )
    conn.execute(
        "INSERT INTO install_history (id, job_id, title_id, display_name, target_device_id, "
        "target_storage, outcome, created_at) VALUES (1, 1, '0100000000010000', 'Game', 'dev-old', "
        "'SD_INSTALL', 'DONE_UNVERIFIED', 't0')"
    )
    conn.commit()
    conn.close()
    return 1


def test_foundation_migration_preserves_pre_existing_jobs_and_history(tmp_path):
    db_path = tmp_path / "pre_ui_roadmap.db"
    job_id = _build_pre_ui_roadmap_db(db_path)

    conn = db.get_connection(db_path)
    db.init_db(conn)

    job = db.get_job(conn, job_id)
    assert job["status"] == "DONE_UNVERIFIED"
    assert job["target_device_id"] == "dev-old"
    assert job["manifest_path"] == "work/job-1/manifest.json"
    # The two new columns exist and are NULL for a pre-existing row --
    # never backfilled with an invented value.
    assert job["batch_id"] is None
    assert job["last_progress_at"] is None

    history = db.list_install_history(conn)
    assert len(history) == 1
    assert history[0]["outcome"] == "DONE_UNVERIFIED"
    assert history[0]["user_verified_outcome"] is None
    assert history[0]["user_verified_at"] is None
    conn.close()


def test_foundation_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "pre_ui_roadmap2.db"
    _build_pre_ui_roadmap_db(db_path)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    db.init_db(conn)  # second call must be a safe no-op, not an error

    assert db.get_job(conn, 1)["status"] == "DONE_UNVERIFIED"
    conn.close()


def test_foundation_migration_new_tables_are_usable_after_upgrade(tmp_path):
    """Not just "columns exist" -- the new tables must be fully
    functional immediately after upgrading a pre-existing database."""
    db_path = tmp_path / "pre_ui_roadmap3.db"
    _build_pre_ui_roadmap_db(db_path)

    conn = db.get_connection(db_path)
    db.init_db(conn)

    batch_id = db.create_installation_batch(conn, target_device_id="dev-new")
    assert db.get_installation_batch(conn, batch_id)["target_device_id"] == "dev-new"

    db.set_device_storage_mapping(conn, "dev-new", "5: SD Card install", "SD_INSTALL")
    mapping = db.get_device_storage_mapping(conn, "dev-new", "5: SD Card install")
    assert mapping["logical_name"] == "SD_INSTALL"
    conn.close()


def test_foundation_migration_can_set_user_verified_outcome_on_preexisting_row(tmp_path):
    db_path = tmp_path / "pre_ui_roadmap4.db"
    _build_pre_ui_roadmap_db(db_path)

    conn = db.get_connection(db_path)
    db.init_db(conn)

    db.set_user_verified_outcome(conn, 1, "SUCCESS")
    row = db.get_install_history_by_id(conn, 1)
    assert row["user_verified_outcome"] == "SUCCESS"
    assert row["user_verified_at"] is not None
    # The original, immutable transport outcome is untouched.
    assert row["outcome"] == "DONE_UNVERIFIED"
    conn.close()


# ---------------------------------------------------------------------------
# STAB-002: migration stress -- a richer synthetic legacy DB covering every
# row shape the mandate explicitly lists (old jobs without batch_id, old
# history without user verification, old devices, old interrupted jobs, old
# DONE_UNVERIFIED, old manifests, old library rows), plus idempotency
# across EVERY affected table, not just jobs.
# ---------------------------------------------------------------------------

def _build_stress_test_legacy_db(path) -> dict:
    """A single legacy DB (pre-UI-roadmap shape, same as
    _PRE_UI_ROADMAP_SCHEMA above) populated with one row of each kind the
    mandate calls out by name. Returns the ids the caller needs to assert
    on."""
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_UI_ROADMAP_SCHEMA)
    conn.execute(
        "INSERT INTO inbox_items (id, relative_path, item_type, file_type, size, mtime, status, "
        "first_seen_at, last_scanned_at) VALUES (1, 'Old.nsp', 'FILE', 'NSP', 5, 0.0, 'ANALYZED', 't', 't')"
    )
    conn.execute(
        "INSERT INTO library_items (id, absolute_path, item_type, file_type, size, mtime, status, "
        "first_seen_at, last_scanned_at) VALUES (1, 'D:\\Download\\Old.nsp', 'FILE', 'NSP', 5, 0.0, "
        "'AVAILABLE', 't', 't')"
    )
    conn.execute(
        "INSERT INTO devices (device_id, friendly_name, last_known_display_name, first_seen_at, "
        "last_seen_at) VALUES ('dev-legacy', 'Old Switch', 'Switch', 't0', 't1')"
    )
    # job 1: old DONE_UNVERIFIED, backed by a real manifest.json on disk
    # (written by the caller, see test below -- load_manifest() derives its
    # path purely from job_id + config.WORK_DIR, never from this column).
    conn.execute(
        "INSERT INTO jobs (id, library_item_id, action, target_storage, status, target_device_id, "
        "manifest_path, created_at) VALUES (1, 1, 'INSTALL_VIA_DBI', 'SD_INSTALL', 'DONE_UNVERIFIED', "
        "'dev-legacy', 'work/job-1/manifest.json', 't0')"
    )
    conn.execute(
        "INSERT INTO install_history (id, job_id, title_id, display_name, target_device_id, "
        "target_storage, outcome, created_at) VALUES (1, 1, '0100000000010000', 'Old', 'dev-legacy', "
        "'SD_INSTALL', 'DONE_UNVERIFIED', 't0')"
    )
    # job 2: old INTERRUPTED -- crashed mid-transfer in a previous run,
    # never resolved before this DB was upgraded.
    conn.execute(
        "INSERT INTO jobs (id, inbox_item_id, action, target_storage, status, target_device_id, "
        "bytes_done, attempt_count, error, created_at, started_at) VALUES (2, 1, 'INSTALL_VIA_DBI', "
        "'SD_INSTALL', 'INTERRUPTED', 'dev-legacy', 1024, 1, 'device disconnected mid-transfer', 't0', 't0')"
    )
    conn.commit()
    conn.close()
    return {"done_unverified_job_id": 1, "interrupted_job_id": 2, "history_id": 1, "device_id": "dev-legacy"}


def test_stab002_richer_legacy_db_migrates_every_row_type_without_loss(tmp_path):
    db_path = tmp_path / "legacy_stress.db"
    ids = _build_stress_test_legacy_db(db_path)

    conn = db.get_connection(db_path)
    db.init_db(conn)

    # old library row
    lib_row = db.get_library_item(conn, "D:\\Download\\Old.nsp")
    assert lib_row["status"] == "AVAILABLE"

    # old device
    device = db.get_device(conn, ids["device_id"])
    assert device["friendly_name"] == "Old Switch"

    # old DONE_UNVERIFIED job + its history, batch_id/user-verification
    # columns present and NULL (never invented)
    done_job = db.get_job(conn, ids["done_unverified_job_id"])
    assert done_job["status"] == "DONE_UNVERIFIED"
    assert done_job["batch_id"] is None
    history = db.get_install_history_by_id(conn, ids["history_id"])
    assert history["outcome"] == "DONE_UNVERIFIED"
    assert history["user_verified_outcome"] is None

    # old INTERRUPTED job
    interrupted_job = db.get_job(conn, ids["interrupted_job_id"])
    assert interrupted_job["status"] == "INTERRUPTED"
    assert interrupted_job["bytes_done"] == 1024
    assert interrupted_job["batch_id"] is None

    conn.close()


def test_stab002_legacy_manifest_file_still_loads_after_migration(tmp_path, monkeypatch):
    """A manifest.json written by a PRE-migration version of this app must
    still be loadable by the CURRENT manifest.load_manifest() after the DB
    itself has been upgraded -- the manifest format is independent of, and
    must not be broken by, any SQLite schema migration."""
    from switchagent import config, manifest

    work_dir = tmp_path / "work"
    monkeypatch.setattr(config, "WORK_DIR", work_dir)

    db_path = tmp_path / "legacy_with_manifest.db"
    ids = _build_stress_test_legacy_db(db_path)

    job_dir = work_dir / f"job-{ids['done_unverified_job_id']}"
    job_dir.mkdir(parents=True)
    (job_dir / "manifest.json").write_text(
        '{"content_type": "GAME_PACKAGE", "title_id": "0100000000010000", '
        '"target_storage": "SD_INSTALL", "files": [{"dest_relative_path": "Old.nsp", '
        '"source_kind": "library", "source_relative_path": "Old.nsp", "size": 5, '
        '"sha256": "deadbeef"}]}',
        encoding="utf-8",
    )

    conn = db.get_connection(db_path)
    db.init_db(conn)  # DB migration must not touch work/ at all

    loaded = manifest.load_manifest(ids["done_unverified_job_id"])
    assert loaded.target_storage == "SD_INSTALL"
    assert loaded.files[0].dest_relative_path == "Old.nsp"
    conn.close()


def test_stab002_repeated_init_db_is_idempotent_across_every_table(tmp_path):
    """Broader than test_foundation_migration_is_idempotent (which only
    checks the jobs table) -- every table this richer legacy DB touches
    must be stable across repeated init_db() calls, and ARCH-003's
    duplicate-index cleanup must apply cleanly even starting from this
    much older schema shape."""
    db_path = tmp_path / "legacy_idempotent.db"
    ids = _build_stress_test_legacy_db(db_path)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    db.init_db(conn)
    db.init_db(conn)  # three calls -- must be a safe no-op each time

    assert len(db.list_jobs(conn)) == 2
    assert len(db.list_install_history(conn)) == 1
    assert db.get_device(conn, ids["device_id"]) is not None
    assert db.get_library_item(conn, "D:\\Download\\Old.nsp") is not None

    index_names = [
        row["name"] for row in conn.execute("PRAGMA index_list(jobs)").fetchall()
    ]
    assert index_names.count("idx_jobs_item") == 0
    # exactly one index still covers inbox_item_id, however it is named
    inbox_item_indexes = [n for n in index_names if "inbox_item" in n]
    assert len(inbox_item_indexes) == 1
    conn.close()


def test_stab002_jobs_indexes_self_heal_on_a_jobs_table_with_no_indexes_at_all(tmp_path):
    """Regression: found by the stress test above. The ONLY place
    idx_jobs_inbox_item/idx_jobs_library_item were ever created was inside
    _migrate_jobs_table_for_library_support's rebuild branch, which is
    skipped entirely once library_item_id already exists. A `jobs` table
    that already has library_item_id but was never created BY this
    application's own rebuild (e.g. hand-built, like this test's raw
    executescript -- deliberately worst-case, with no indexes at all,
    unlike the more realistic _build_stress_test_legacy_db above) used to
    end up with ZERO indexes on inbox_item_id after init_db(), because
    _drop_duplicate_jobs_indexes() would drop the only one present
    (idx_jobs_item) while the rebuild that would have created its
    canonical replacement never ran. init_db() must now self-heal this
    regardless of history."""
    db_path = tmp_path / "no_indexes_at_all.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE inbox_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, relative_path TEXT NOT NULL UNIQUE,
            item_type TEXT NOT NULL, file_type TEXT NOT NULL, size INTEGER NOT NULL,
            mtime REAL NOT NULL, content_hash TEXT, title_id TEXT, title_id_source TEXT,
            status TEXT NOT NULL DEFAULT 'NEW', suggested_action TEXT, suggested_target TEXT,
            note TEXT, error TEXT, first_seen_at TEXT NOT NULL, last_scanned_at TEXT NOT NULL
        );
        CREATE TABLE library_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, absolute_path TEXT NOT NULL UNIQUE,
            item_type TEXT NOT NULL, file_type TEXT NOT NULL, size INTEGER NOT NULL,
            mtime REAL NOT NULL, content_hash TEXT, title_id TEXT,
            status TEXT NOT NULL DEFAULT 'AVAILABLE', first_seen_at TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL
        );
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_item_id INTEGER REFERENCES inbox_items(id),
            library_item_id INTEGER REFERENCES library_items(id),
            action TEXT NOT NULL, target_storage TEXT, status TEXT NOT NULL DEFAULT 'PENDING_CONFIRM',
            bytes_total INTEGER, bytes_done INTEGER DEFAULT 0, attempt_count INTEGER DEFAULT 0,
            error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            target_device_id TEXT, manifest_path TEXT,
            CHECK ((inbox_item_id IS NOT NULL) + (library_item_id IS NOT NULL) = 1)
        );
    """)
    conn.commit()
    conn.close()

    conn = db.get_connection(db_path)
    db.init_db(conn)

    index_names = {row["name"] for row in conn.execute("PRAGMA index_list(jobs)").fetchall()}
    assert "idx_jobs_inbox_item" in index_names
    assert "idx_jobs_library_item" in index_names
    assert "idx_jobs_item" not in index_names
    conn.close()


# ---------------------------------------------------------------------------
# ARCH-002: VERIFYING is RESERVED (never assigned by any real code path,
# but intentionally kept in JOB_STATUSES, not removed -- see db.py's own
# comment). A row that somehow already has this status (a hand-edited DB,
# or some future/external tool) must survive migration and render
# gracefully everywhere, never crash.
# ---------------------------------------------------------------------------

def test_a_verifying_status_job_survives_migration_and_renders_gracefully(isolated_db):
    conn, _inbox_dir = isolated_db
    item_id = _add_library_item(conn, r"D:\shared\Download\A.nsp")
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
        target_device_id="dev-a", library_item_id=item_id,
    )
    conn.execute("UPDATE jobs SET status = 'VERIFYING' WHERE id = ?", (job_id,))
    conn.commit()

    db.init_db(conn)  # re-init, as every request/worker tick already does
    row = db.get_job(conn, job_id)
    assert row["status"] == "VERIFYING"  # untouched, never silently rewritten

    from switchagent.web import services
    view = services._job_view(conn, row)  # must not raise for an unmapped status
    assert view["status"] == "VERIFYING"


# ---------------------------------------------------------------------------
# ARCH-003: idx_jobs_item (the original stage-3 SCHEMA's own CREATE INDEX)
# is an exact duplicate of idx_jobs_inbox_item, silently recreated on every
# init_db() call after the jobs-table rebuild has already dropped it once.
# ---------------------------------------------------------------------------

def test_no_duplicate_jobs_index_after_repeated_init_db_calls(isolated_db):
    conn, _inbox_dir = isolated_db  # isolated_db's own fixture already calls open_db() -> init_db() once
    db.init_db(conn)
    db.init_db(conn)
    db.init_db(conn)

    names = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
        )
    }
    assert names == {"idx_jobs_inbox_item", "idx_jobs_library_item", "idx_jobs_status"}
    assert "idx_jobs_item" not in names


def test_pre_existing_duplicate_index_is_cleaned_up_on_next_init_db(tmp_path):
    """Simulates the REAL production database's actual state (confirmed
    via direct inspection, 2026-09-12): idx_jobs_item already exists
    alongside idx_jobs_inbox_item from before this fix -- the very next
    init_db() call must clean it up, not just prevent NEW occurrences."""
    db_path = tmp_path / "has_duplicate_index.db"
    conn = db.get_connection(db_path)
    db.init_db(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_item ON jobs(inbox_item_id)")
    conn.commit()
    names_before = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
        )
    }
    assert "idx_jobs_item" in names_before  # the bug, reproduced

    db.init_db(conn)
    names_after = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
        )
    }
    assert "idx_jobs_item" not in names_after
    conn.close()
