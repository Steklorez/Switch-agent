import pytest
from switchagent import config, db, manifest, preview, scanner, queue_worker
from switchagent.web import services
from .test_multi_package_archive import _backend


def test_multiple_roots_scan_and_transfer_keep_sources_distinct(isolated_db, tmp_path):
    conn, _ = isolated_db
    first = config.LIBRARY_DIR
    second = tmp_path / "Second library"
    second.mkdir()
    config.set_library_source_dirs([first, second])
    name = "Game [0100000000010000][v0].nsp"
    (first / name).write_bytes(b"first")
    (second / name).write_bytes(b"second")
    mod = second / "Russian" / "atmosphere" / "titles" / "0100000000010000" / "romfs"
    mod.mkdir(parents=True)
    (mod / "text.bin").write_bytes(b"translation")
    assert scanner.scan_library_once(conn)["new"] == 3
    assert scanner.scan_library_once(conn)["unchanged"] == 3
    for source in (second / name, mod.parent):
        row = db.get_library_item(conn, str(source))
        result = services.create_and_confirm_jobs(conn, [row["id"]], "mock-switch-parent")
        assert not result["errors"]
        job_id = result["created"][0]["job_id"]
        loaded = manifest.load_manifest(job_id)
        assert manifest.resolve_source_path(loaded.files[0], job_id).is_relative_to(second)
        backend, registry = _backend()
        assert queue_worker.run_worker_once(conn, registry).status == "DONE"
    assert (first / name).read_bytes() == b"first"
    assert (second / name).read_bytes() == b"second"
    config.set_library_source_dirs([first])
    scanner.scan_library_once(conn)
    # Keep job history references, but make removed sources unavailable --
    # which is precisely what RETIRED names (db.LIBRARY_ITEM_RETIRED): a
    # record, not content, and not something to review either.
    assert db.get_library_item(conn, str(second / name))["status"] == db.LIBRARY_ITEM_RETIRED
    with pytest.raises(manifest.ManifestError, match="no longer configured"):
        manifest.resolve_source_path(loaded.files[0], job_id)
    assert (mod / "text.bin").exists()


def test_offline_library_keeps_index_and_config_preserves_other_sections(isolated_db, tmp_path):
    conn, _ = isolated_db
    config.CONFIG_YAML_PATH.write_text("# keep\nextraction:\n  max_file_count: 123\n", encoding="utf-8")
    second = tmp_path / "removable"
    second.mkdir()
    config.set_library_source_dirs([config.LIBRARY_DIR, second])
    path = second / "Game [0100000000010000].nsp"
    path.write_bytes(b"data")
    scanner.scan_library_once(conn)
    path.unlink()
    second.rmdir()
    assert scanner.scan_library_once(conn)["removed"] == 0
    assert "# keep\nextraction:\n  max_file_count: 123\n" in config.CONFIG_YAML_PATH.read_text(encoding="utf-8")
