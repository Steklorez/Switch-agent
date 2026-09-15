"""Stage 4.1 regression tests: idempotent retry, honest conflict handling,
and the TOCTOU fix between job confirmation and actual transfer.

These are the six scenarios the Stage 4.1 task explicitly required,
verified empirically (not just by code reading) against the real
queue_worker.py + manifest.py + MockMtpBackend code paths.
"""

from __future__ import annotations

from switchagent import db, preview, queue_worker, scanner
from switchagent.mtp import MockMtpBackend


def _make_mod_item(conn, inbox_dir, title_id: str, files: dict[str, bytes]):
    mod_dir = inbox_dir / "atmosphere" / "contents" / title_id / "romfs"
    mod_dir.mkdir(parents=True)
    for name, content in files.items():
        (mod_dir / name).write_bytes(content)
    rel_path = f"atmosphere/contents/{title_id}"
    db.upsert_inbox_item(
        conn, relative_path=rel_path, item_type="MOD_FOLDER", file_type="ATMOSPHERE_MOD",
        size=sum(len(c) for c in files.values()), mtime=0.0, content_hash="mod-hash",
        title_id=title_id, title_id_source="atmosphere_path", status="ANALYZED",
        suggested_action="COPY_MERGE", suggested_target="SD_CARD",
    )
    item_id = db.get_inbox_item(conn, rel_path)["id"]
    return item_id, mod_dir.parent  # the title_id folder itself (mod_dir is .../<title_id>/romfs)


def _backend(device_id="mock-switch-parent"):
    backend = MockMtpBackend(device_id=device_id)
    backend.add_storage("SD_CARD")
    backend.add_storage("SD_INSTALL")
    registry = queue_worker.DeviceRegistry()
    registry.register(device_id, backend)
    return backend, registry


# -- Test 1: interrupted multi-file retry -----------------------------

def test_interrupted_multi_file_retry_resumes_without_already_exists_error(isolated_db):
    conn, inbox_dir = isolated_db
    title_id = "0100000000010000"
    item_id, mod_root = _make_mod_item(conn, inbox_dir, title_id, {
        "a_first.bin": b"file one content",
        "b_second.bin": b"file two content",
        "c_third.bin": b"file three content",
    })
    backend, registry = _backend()

    report = preview.preview_path(mod_root)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    dest_c = f"atmosphere/contents/{title_id}/romfs/c_third.bin"
    backend.arm_failure("disconnect", dest_path=dest_c)

    outcome1 = queue_worker.run_worker_once(conn, registry)
    assert outcome1.status == "INTERRUPTED"
    files_after_first_attempt = set(backend.storage_tree("SD_CARD").list_files())
    assert f"atmosphere/contents/{title_id}/romfs/a_first.bin" in files_after_first_attempt
    assert f"atmosphere/contents/{title_id}/romfs/b_second.bin" in files_after_first_attempt
    assert dest_c not in files_after_first_attempt

    # Device "reconnects" (device_present was never set False -- only the
    # single armed fault fired) and the job is explicitly retried.
    db.retry_job(conn, job_id)
    outcome2 = queue_worker.run_worker_once(conn, registry)

    assert outcome2.status == "DONE", (
        "retry must resume, not fail on the already-delivered a_first.bin/b_second.bin"
    )
    assert outcome2.error is None
    final_files = set(backend.storage_tree("SD_CARD").list_files())
    assert final_files == files_after_first_attempt | {dest_c}
    # content integrity: the already-delivered files were not touched/duplicated-wrong
    assert backend.storage_tree("SD_CARD").read_file(
        f"atmosphere/contents/{title_id}/romfs/a_first.bin"
    ) == b"file one content"
    assert backend.storage_tree("SD_CARD").read_file(dest_c) == b"file three content"


# -- Test 2: existing destination file with unknown content ------------

def test_existing_destination_with_unknown_content_is_not_overwritten(isolated_db):
    conn, inbox_dir = isolated_db
    title_id = "0100000000020000"
    item_id, mod_root = _make_mod_item(conn, inbox_dir, title_id, {
        "text.bin": b"our confirmed content",
    })
    backend, registry = _backend()

    # Something we did NOT send is already sitting at the destination --
    # simulates a leftover/foreign file, not a prior successful attempt by
    # this job (which would instead be reflected in progress.json).
    backend.connect()
    dest = f"atmosphere/contents/{title_id}/romfs/text.bin"
    backend.ensure_directory("SD_CARD", f"atmosphere/contents/{title_id}/romfs")
    backend.send_file("SD_CARD", dest, _write_tmp(inbox_dir, b"unrelated pre-existing content"))

    report = preview.preview_path(mod_root)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "DESTINATION_CONFLICT"
    assert "already exists" in outcome.error
    # not overwritten -- still the foreign content, not our confirmed content
    assert backend.storage_tree("SD_CARD").read_file(dest) == b"unrelated pre-existing content"


def _write_tmp(inbox_dir, content: bytes):
    p = inbox_dir / "_scratch_source.bin"
    p.write_bytes(content)
    return p


# -- Test 3: source changed after confirmation (TOCTOU) -----------------

def test_source_changed_after_confirmation_is_not_sent(isolated_db):
    conn, inbox_dir = isolated_db
    name = "GameA [0100000000030000][v0].nsp"
    (inbox_dir / name).write_bytes(b"content A - what the user confirmed")
    db.upsert_inbox_item(
        conn, relative_path=name, item_type="FILE", file_type="NSP",
        size=30, mtime=0.0, content_hash="hashA", title_id="0100000000030000",
        title_id_source="filename", status="ANALYZED",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    item_id = db.get_inbox_item(conn, name)["id"]
    backend, registry = _backend()

    report = preview.preview_path(inbox_dir / name)  # user previews A
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="INSTALL_VIA_DBI", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)  # user confirms A

    # File changes in inbox AFTER confirmation (e.g. a different release
    # re-downloaded under the identical filename).
    (inbox_dir / name).write_bytes(b"content B - NOT what was confirmed, must never be sent")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "SOURCE_CHANGED"
    assert db.get_job(conn, job_id)["status"] == "SOURCE_CHANGED"
    assert backend.storage_tree("SD_INSTALL").list_files() == [], "nothing must be sent -- neither A nor B"


# -- Test 4: source deleted after confirmation ---------------------------

def test_source_deleted_after_confirmation_fails_safely(isolated_db):
    conn, inbox_dir = isolated_db
    name = "GameA [0100000000040000][v0].nsp"
    (inbox_dir / name).write_bytes(b"content A")
    db.upsert_inbox_item(
        conn, relative_path=name, item_type="FILE", file_type="NSP",
        size=9, mtime=0.0, content_hash="hashA", title_id="0100000000040000",
        title_id_source="filename", status="ANALYZED",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    item_id = db.get_inbox_item(conn, name)["id"]
    backend, registry = _backend()

    report = preview.preview_path(inbox_dir / name)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="INSTALL_VIA_DBI", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    (inbox_dir / name).unlink()  # user deletes the source after confirming

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "FAILED"
    assert "no longer exists" in outcome.error
    assert backend.storage_tree("SD_INSTALL").list_files() == [], "nothing else must be sent in its place"


# -- Test 6: explicit confirmation boundary -----------------------------

def test_scan_alone_never_creates_a_job(isolated_db):
    conn, inbox_dir = isolated_db
    (inbox_dir / "Game [0100000000050000][v0].nsp").write_bytes(b"nsp bytes")

    summary = scanner.scan_once(conn)

    assert summary["new"] == 1
    assert db.list_jobs(conn) == [], "scanning/analyzing must never create a job on its own"


def test_scan_of_obviously_installable_file_still_does_not_create_a_job(isolated_db):
    """Even a file that scans as unambiguously ANALYZED/INSTALL_VIA_DBI --
    the clearest possible case for auto-install -- must not turn into a job
    without an explicit, separate confirmation action."""
    conn, inbox_dir = isolated_db
    (inbox_dir / "Game [0100000000060000][v0].nsp").write_bytes(b"nsp bytes")
    scanner.scan_once(conn)

    item = db.get_inbox_item(conn, "Game [0100000000060000][v0].nsp")
    assert item["status"] == "ANALYZED"
    assert item["suggested_action"] == "INSTALL_VIA_DBI"
    assert db.list_jobs(conn) == [], "a clear ANALYZED/INSTALL_VIA_DBI suggestion is not a confirmation"


# -- FAULT-002: source mutation matrix (mock-only, per the mandate) -----
# The TOCTOU guard itself (manifest.verify_manifest_against_source(), called
# before any device is touched) is already exercised by tests 3/4 above
# (source changed / source deleted, both bare-package-file cases) -- these
# fill in the specific scenarios the mandate names by their own label that
# weren't yet covered under their own name: an explicit size change (as
# opposed to same-size-different-bytes, already covered in
# tests/test_manifest.py), a rename, and the Atmosphere-mod (multi-file)
# case in both directions (an existing mod file mutated/deleted, and a NEW
# file added to the mod folder after confirmation -- which must be ignored,
# not sent, since the manifest is frozen at confirmation time).

def test_source_size_change_after_confirmation_is_source_changed(isolated_db):
    """Distinct from test_manifest.py's existing same-size-different-bytes
    case -- this is the cheaper, checked-first size-mismatch path, at the
    full worker level, not just verify_manifest_against_source() in
    isolation."""
    conn, inbox_dir = isolated_db
    name = "Game [0100000000073000][v0].nsp"
    (inbox_dir / name).write_bytes(b"short")
    db.upsert_inbox_item(
        conn, relative_path=name, item_type="FILE", file_type="NSP",
        size=5, mtime=0.0, content_hash="hashY", title_id="0100000000073000",
        title_id_source="filename", status="ANALYZED",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    item_id = db.get_inbox_item(conn, name)["id"]
    backend, registry = _backend()
    report = preview.preview_path(inbox_dir / name)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="INSTALL_VIA_DBI", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    (inbox_dir / name).write_bytes(b"a much longer replacement file entirely")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "SOURCE_CHANGED"
    assert backend.storage_tree("SD_INSTALL").list_files() == []


def test_source_renamed_after_confirmation_is_treated_as_missing(isolated_db):
    """A rename is, from the frozen manifest's point of view, indistinguishable
    from a delete -- verify_manifest_against_source() only ever re-checks
    its OWN frozen source_relative_path, it never searches for a
    same-content file that moved to a new name. Locks in that this is the
    correct, intended fail-safe behavior, not an unnoticed gap."""
    conn, inbox_dir = isolated_db
    name = "Game [0100000000072000][v0].nsp"
    (inbox_dir / name).write_bytes(b"original nsp bytes")
    db.upsert_inbox_item(
        conn, relative_path=name, item_type="FILE", file_type="NSP",
        size=19, mtime=0.0, content_hash="hashX", title_id="0100000000072000",
        title_id_source="filename", status="ANALYZED",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    item_id = db.get_inbox_item(conn, name)["id"]
    backend, registry = _backend()
    report = preview.preview_path(inbox_dir / name)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="INSTALL_VIA_DBI", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    (inbox_dir / name).rename(inbox_dir / "Renamed [0100000000072000][v0].nsp")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "FAILED"
    assert "no longer exists" in outcome.error
    assert backend.storage_tree("SD_INSTALL").list_files() == []


def test_mod_file_modified_after_confirmation_is_source_changed(isolated_db):
    conn, inbox_dir = isolated_db
    title_id = "0100000000070000"
    item_id, mod_root = _make_mod_item(conn, inbox_dir, title_id, {"a.bin": b"original content"})
    backend, registry = _backend()
    report = preview.preview_path(mod_root)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    # Same size, different bytes -- must be caught by the hash check.
    (mod_root / "romfs" / "a.bin").write_bytes(b"mutated content!")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "SOURCE_CHANGED"
    assert backend.storage_tree("SD_CARD").list_files() == [], "nothing must be sent once drift is detected"


def test_mod_file_deleted_after_confirmation_fails_safely(isolated_db):
    conn, inbox_dir = isolated_db
    title_id = "0100000000071000"
    item_id, mod_root = _make_mod_item(conn, inbox_dir, title_id, {
        "a.bin": b"content a", "b.bin": b"content b",
    })
    backend, registry = _backend()
    report = preview.preview_path(mod_root)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    (mod_root / "romfs" / "a.bin").unlink()

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "FAILED"
    assert "no longer exists" in outcome.error
    assert backend.storage_tree("SD_CARD").list_files() == [], (
        "nothing sent -- not even the still-present sibling file b.bin; the TOCTOU "
        "guard checks every manifest file before touching the device at all"
    )


def test_new_file_added_to_mod_folder_after_confirmation_is_ignored(isolated_db):
    """The manifest is frozen at confirmation time -- a file added to the
    live source folder afterward is simply never part of this job's
    manifest, so it is correctly never checked and never sent. This is the
    intended, documented behavior (manifest.py's own module docstring),
    not a gap -- locked in explicitly since the mandate names 'mod
    mutation' as its own scenario."""
    conn, inbox_dir = isolated_db
    title_id = "0100000000074000"
    item_id, mod_root = _make_mod_item(conn, inbox_dir, title_id, {"a.bin": b"original content"})
    backend, registry = _backend()
    report = preview.preview_path(mod_root)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="COPY_MERGE", target_device_id="mock-switch-parent",
    )
    db.confirm_job(conn, job_id)

    (mod_root / "romfs" / "b_added_later.bin").write_bytes(b"should never be sent")

    outcome = queue_worker.run_worker_once(conn, registry)

    assert outcome.status == "DONE"
    sent = backend.storage_tree("SD_CARD").list_files()
    assert any(f.endswith("a.bin") for f in sent)
    assert not any(f.endswith("b_added_later.bin") for f in sent), (
        "a file added to the source folder after confirmation must never be sent -- "
        "the manifest is frozen, not re-scanned"
    )
