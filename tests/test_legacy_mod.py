import pytest
from switchagent import config, db, manifest, preview, scanner, queue_worker
from switchagent.web import services
from .conftest import build_zip
from .test_multi_package_archive import _backend


@pytest.mark.parametrize("archived", [False, True])
def test_legacy_titles_mod_is_discovered_and_sent_to_contents(isolated_db, archived):
    conn, _ = isolated_db
    rel = "Russian Language Mod (12.10.2018)/atmosphere/titles/0100a6300150c000/romfs/text.bin"
    if archived:
        source = build_zip(config.LIBRARY_DIR / "Russian.zip", {rel: b"translation"})
    else:
        file = config.LIBRARY_DIR / "Wonder Boy [NSP]" / rel
        file.parent.mkdir(parents=True)
        file.write_bytes(b"translation")
        source = file.parent.parent
    scanner.scan_library_once(conn)
    item = db.get_library_item(conn, str(source))
    assert item["status"] == "AVAILABLE"
    assert item["title_id"] == "0100A6300150C000"
    result = services.create_and_confirm_jobs(conn, [item["id"]], "mock-switch-parent")
    assert not result["errors"] and len(result["created"]) == 1
    job = result["created"][0]["job_id"]
    assert manifest.load_manifest(job).files[0].dest_relative_path == "atmosphere/contents/0100A6300150C000/romfs/text.bin"
    backend, registry = _backend()
    assert queue_worker.run_worker_once(conn, registry).status == "DONE"
    assert source.exists()
