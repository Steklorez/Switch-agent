"""Queue rows say what they are installing: Game / Update / DLC / Mod.

Asked for directly (2026-09-18): the Queue was a list of filenames, and
telling an update apart from a DLC meant reading the TITLE_ID in the name
and doing the hex arithmetic in your head. The install-confirmation dialog
already tags exactly these kinds, so Queue reuses its vocabulary AND its
colour per kind rather than inventing a second one.

Two sources feed the same badge, and both are covered here:

  * a real job reads its own FROZEN manifest (services._job_variant_role) --
    never the library row, which can have moved on since the job was
    created;
  * an item still waiting to be prepared has no manifest yet, so the
    preparation panel classifies the library row instead
    (preparation._library_item_role).

The rule both share: when the kind cannot actually be told, the answer is
None and the row shows NO badge. A guessed kind on a row that decides what
gets written to a console is worse than no badge at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders, manifest as manifest_mod, preview, queue_worker
from switchagent.web import preparation as preparation_mod
from switchagent.web import services
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

PARENT = "mock-switch-parent"

BASE_TITLE_ID = "0100E65002BB8000"      # low 12 bits 0x000
UPDATE_TITLE_ID = "0100E65002BB8800"    # low 12 bits 0x800
DLC_TITLE_ID = "0100E65002BB9001"       # neither -> DLC


@pytest.fixture
def web_ctx(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)

    db_path = tmp_path / "test.db"
    with db.open_db(db_path):
        pass
    ctx = build_mock_context(db_path)
    with db.open_db(db_path) as conn:
        ctx.refresh_devices(conn)
    yield ctx


@pytest.fixture
def client(web_ctx):
    return TestClient(create_app(web_ctx))


def _queued_job(conn, title_id: str, name: str = "Stardew Valley") -> int:
    """A real, confirmed job with a real frozen manifest -- built the same
    way tests/test_queue_worker.py builds one, because the badge is read
    off that manifest and a hand-written stub would prove nothing."""
    filename = f"{name} [{title_id}][v0].nsp"
    (config.INBOX_DIR / filename).write_bytes(b"payload")
    db.upsert_inbox_item(
        conn, relative_path=filename, item_type="FILE", file_type="NSP",
        size=7, mtime=0.0, content_hash=f"hash-{filename}",
        title_id=title_id, title_id_source="filename", status="ANALYZED",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    item_id = db.get_inbox_item(conn, filename)["id"]
    report = preview.preview_path(config.INBOX_DIR / filename)
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="INSTALL_VIA_DBI", target_device_id=PARENT,
    )
    db.confirm_job(conn, job_id)
    return job_id


# ---------------------------------------------------------------------------
# a real job: classified from its own frozen manifest
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title_id,role,label", [
    (BASE_TITLE_ID, "base", "Game"),
    (UPDATE_TITLE_ID, "update", "Update"),
    (DLC_TITLE_ID, "dlc", "DLC"),
])
def test_a_job_is_labelled_by_what_its_manifest_says_it_is(web_ctx, title_id, role, label):
    with db.open_db(web_ctx.db_path) as conn:
        _queued_job(conn, title_id)
        view = services.list_queue(conn)[0]

    assert view["variant_role"] == role
    assert view["variant_label"] == label


def test_a_job_whose_manifest_is_gone_gets_no_badge_rather_than_a_guess(web_ctx):
    """FAULT-001's defensive-load rule applied to a display concern: an
    unreadable manifest means "cannot tell", and a Queue row that decides
    what gets written to a console must not invent the answer."""
    with db.open_db(web_ctx.db_path) as conn:
        job_id = _queued_job(conn, UPDATE_TITLE_ID)
        manifest_mod.manifest_path_for(job_id).unlink()
        view = services.list_queue(conn)[0]

    assert view["variant_role"] is None
    assert view["variant_label"] is None


def test_the_badge_is_rendered_on_the_queue_page(client, web_ctx):
    with db.open_db(web_ctx.db_path) as conn:
        _queued_job(conn, UPDATE_TITLE_ID)

    html = client.get("/queue").text

    assert 'class="job-tag job-tag-update"' in html
    assert "[Update]" in html


def test_a_row_with_no_classifiable_kind_renders_no_badge_element(client, web_ctx):
    with db.open_db(web_ctx.db_path) as conn:
        job_id = _queued_job(conn, BASE_TITLE_ID)
        manifest_mod.manifest_path_for(job_id).unlink()

    html = client.get("/queue").text

    assert "job-tag" not in html


def test_the_api_carries_the_role_so_the_live_poll_can_rebuild_the_badge(client, web_ctx):
    """queue.js re-renders rows in place every few seconds; without this
    field the badge would appear on load and vanish on the first poll."""
    with db.open_db(web_ctx.db_path) as conn:
        _queued_job(conn, DLC_TITLE_ID)

    payload = client.get("/api/queue").json()

    assert payload[0]["variant_role"] == "dlc"
    assert payload[0]["variant_label"] == "DLC"


# ---------------------------------------------------------------------------
# mods: the badge replaces the " — Mod" suffix, but only where it exists
# ---------------------------------------------------------------------------

def _queued_mod(conn) -> int:
    rel = "Russian Language Mod/atmosphere/titles/0100a6300150c000/romfs/text.bin"
    path = config.INBOX_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"mod bytes")
    db.upsert_inbox_item(
        conn, relative_path="Russian Language Mod", item_type="MOD_FOLDER", file_type="FOLDER",
        size=9, mtime=0.0, content_hash="hash-mod", title_id="0100A6300150C000",
        title_id_source="path", status="ANALYZED",
        suggested_action="COPY_TO_SD", suggested_target="SD_CARD",
    )
    item_id = db.get_inbox_item(conn, "Russian Language Mod")["id"]
    report = preview.preview_path(config.INBOX_DIR / "Russian Language Mod")
    job_id = queue_worker.create_job_from_report(
        conn, report, inbox_item_id=item_id, action="COPY_TO_SD", target_device_id=PARENT,
    )
    db.confirm_job(conn, job_id)
    return job_id


def test_a_mod_is_badged_and_stops_repeating_itself_in_its_name(web_ctx):
    """queue_worker appends " — Mod" to a mod's name precisely because
    Queue and History had nothing else to say it. Queue says it with a
    badge now, so the row must not say it twice."""
    with db.open_db(web_ctx.db_path) as conn:
        _queued_mod(conn)
        view = services.list_queue(conn)[0]

    assert view["variant_role"] == "mod"
    assert view["variant_label"] == "Mod"
    assert not view["display_name"].endswith("— Mod")


def test_a_mod_whose_manifest_is_gone_keeps_the_suffix_instead(web_ctx):
    """The suffix is dropped because the badge replaces it -- so when there
    is no badge, the information must not vanish with it."""
    with db.open_db(web_ctx.db_path) as conn:
        job_id = _queued_mod(conn)
        manifest_mod.manifest_path_for(job_id).unlink()
        view = services.list_queue(conn)[0]

    assert view["variant_role"] is None
    assert view["display_name"].endswith("— Mod")


def test_history_still_says_mod_in_the_name(web_ctx):
    """History has no badge column, so display_name_for_job keeps its
    suffix there -- that is why this is a parameter and not a deletion."""
    with db.open_db(web_ctx.db_path) as conn:
        job_id = _queued_mod(conn)
        row = db.get_job(conn, job_id)
        name = queue_worker.display_name_for_job(conn, row)

    assert name.endswith("— Mod")


# ---------------------------------------------------------------------------
# an item still waiting to be prepared: classified from the library row
# ---------------------------------------------------------------------------

class _Row(dict):
    """library_items rows arrive as sqlite3.Row (subscriptable by column
    name) -- a dict is the same shape for this pure function."""


@pytest.mark.parametrize("title_id,role", [
    (BASE_TITLE_ID, "base"),
    (UPDATE_TITLE_ID, "update"),
    (DLC_TITLE_ID, "dlc"),
])
def test_a_waiting_library_item_is_classified_by_its_title_id(title_id, role):
    row = _Row(item_type="FILE", content_type="GAME_PACKAGE", title_id=title_id)
    assert preparation_mod._library_item_role(row) == role


def test_a_mod_folder_is_a_mod_whatever_its_title_id_looks_like():
    """A mod's own title_id IS its base game's (atmosphere/contents/<id>/
    convention, no variant arithmetic), so classifying it by title_id alone
    would label every mod "Game"."""
    row = _Row(item_type="MOD_FOLDER", content_type="ATMOSPHERE_MOD", title_id=BASE_TITLE_ID)
    assert preparation_mod._library_item_role(row) == "mod"


def test_an_item_with_no_title_id_yet_gets_no_badge():
    """An archive is only classifiable after extraction -- until then the
    honest answer is "unknown"."""
    row = _Row(item_type="FILE", content_type=None, title_id=None)
    assert preparation_mod._library_item_role(row) is None
    assert preparation_mod._library_item_role(None) is None


def test_an_unparsable_title_id_is_refused_not_crashed_on():
    """title_id.classify_title_variant() does int(value, 16) -- a damaged
    row must produce no badge, never a 500 on the Queue page."""
    row = _Row(item_type="FILE", content_type="GAME_PACKAGE", title_id="not-hex")
    assert preparation_mod._library_item_role(row) is None
