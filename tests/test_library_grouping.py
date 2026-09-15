"""Tests for the Library grouping UX (switchagent/web/services.py):
list_library_view()'s grouping by base game / update / DLC / mod. The
underlying TITLE_ID classification arithmetic itself
(title_id.classify_title_variant, shared with queue_worker.py's
install-order dependency enforcement) is tested in tests/test_title_id.py,
not duplicated here. Pure service-layer tests against a plain
isolated_db -- no WebContext/FastAPI/MockMtpBackend needed, rows are
inserted directly via db.upsert_library_item().
"""

from __future__ import annotations

from switchagent import db
from switchagent.web import services


def _item(conn, path, *, title_id, size=1000, content_type="GAME_PACKAGE", content_hash=None):
    return db.upsert_library_item(
        conn, absolute_path=path, item_type="FILE", file_type="NSP", size=size, mtime=0.0,
        content_hash=content_hash or path, title_id=title_id, title_id_source="filename",
        status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        content_type=content_type, package_format="NSP",
    )


def _mod_item(conn, path, *, title_id, size=1000, content_hash=None):
    """A real mod folder always has item_type=MOD_FOLDER, unlike _item()'s
    FILE default -- its own folder name is literally the TITLE_ID (the
    atmosphere/contents/<TITLE_ID>/ convention), which is exactly the
    condition find_family_base_name_source()/_resolve_entry_name() key
    off of."""
    return db.upsert_library_item(
        conn, absolute_path=path, item_type="MOD_FOLDER", file_type="ATMOSPHERE_MOD", size=size, mtime=0.0,
        content_hash=content_hash or path, title_id=title_id, title_id_source="atmosphere_path",
        title_id_confident=True, status="AVAILABLE", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
        content_type="ATMOSPHERE_MOD",
    )


# ---------------------------------------------------------------------------
# list_library_view -- grouping
# ---------------------------------------------------------------------------

def test_base_update_dlc_group_into_one_game(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "base.nsp", title_id="01003AF0200B0000")
    _item(conn, "update.nsp", title_id="01003AF0200B0800")
    _item(conn, "dlc1.nsp", title_id="01003AF0200B1001")
    _item(conn, "dlc2.nsp", title_id="01003AF0200B1002")

    view = services.list_library_view(conn, kind="games")
    assert len(view["games"]) == 1
    game = view["games"][0]
    assert game["base"]["absolute_path"] == "base.nsp"
    assert len(game["updates"]) == 1
    assert len(game["dlc"]) == 2
    assert game["variant_count"] == 4


def test_two_different_games_stay_separate(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "gameA.nsp", title_id="0100000000010000")
    _item(conn, "gameB.nsp", title_id="0100000000020000")
    view = services.list_library_view(conn, kind="games")
    assert len(view["games"]) == 2


def test_atmosphere_mods_never_appear_under_games(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "base.nsp", title_id="0100000000010000")
    _item(conn, "mod", title_id="0100000000010000", content_type="ATMOSPHERE_MOD")

    view = services.list_library_view(conn, kind="games")
    assert len(view["games"]) == 1
    assert view["games"][0]["base"]["content_type"] == "GAME_PACKAGE"

    mods_view = services.list_library_view(conn, kind="mods")
    assert len(mods_view["entries"]) == 1
    assert mods_view["entries"][0]["absolute_path"] == "mod"


def test_updates_kind_is_flat_across_all_games(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "baseA.nsp", title_id="0100000000010000")
    _item(conn, "updateA.nsp", title_id="0100000000010800")
    _item(conn, "baseB.nsp", title_id="0100000000020000")
    _item(conn, "updateB.nsp", title_id="0100000000020800")

    view = services.list_library_view(conn, kind="updates")
    paths = {e["absolute_path"] for e in view["entries"]}
    assert paths == {"updateA.nsp", "updateB.nsp"}


def test_dlc_kind_is_flat_across_all_games(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "baseA.nsp", title_id="0100000000010000")
    _item(conn, "dlcA.nsp", title_id="0100000000011001")
    view = services.list_library_view(conn, kind="dlc")
    assert [e["absolute_path"] for e in view["entries"]] == ["dlcA.nsp"]


def test_unknown_title_id_is_its_own_ungrouped_entry_not_dropped(isolated_db):
    conn, _inbox_dir = isolated_db
    db.upsert_library_item(
        conn, absolute_path="needs_review.nsp", item_type="FILE", file_type="NSP", size=10, mtime=0.0,
        content_hash="h", title_id=None, title_id_source=None, status="NEEDS_REVIEW",
        suggested_action=None, suggested_target=None, content_type="GAME_PACKAGE", package_format="NSP",
    )
    view = services.list_library_view(conn, kind="games")
    assert len(view["games"]) == 1
    assert view["games"][0]["base"]["absolute_path"] == "needs_review.nsp"
    assert view["games"][0]["updates"] == []
    assert view["games"][0]["dlc"] == []


def test_duplicate_base_copies_both_kept_not_overwritten(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "base_copy1.nsp", title_id="0100000000010000", content_hash="same-hash")
    _item(conn, "base_copy2.nsp", title_id="0100000000010000", content_hash="same-hash")

    view = services.list_library_view(conn, kind="games")
    assert len(view["games"]) == 1
    game = view["games"][0]
    assert game["base"] is not None
    assert len(game["duplicates"]) == 1
    all_paths = {game["base"]["absolute_path"]} | {d["absolute_path"] for d in game["duplicates"]}
    assert all_paths == {"base_copy1.nsp", "base_copy2.nsp"}


def test_search_and_format_filters_still_apply_to_grouped_view(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "Zelda base.nsp", title_id="0100000000010000")
    _item(conn, "Mario base.nsp", title_id="0100000000020000")
    view = services.list_library_view(conn, kind="games", search="zelda")
    assert len(view["games"]) == 1
    assert "Zelda" in view["games"][0]["name"]


def test_dlc_without_a_scanned_base_still_shows_up(isolated_db):
    """The base game isn't in the library (never downloaded) -- the DLC
    must still be visible, not silently discarded because its family has
    no base entry."""
    conn, _inbox_dir = isolated_db
    _item(conn, "orphan_dlc.nsp", title_id="0100000000011001")
    view = services.list_library_view(conn, kind="games")
    assert len(view["games"]) == 1
    assert view["games"][0]["base"] is None
    assert len(view["games"][0]["dlc"]) == 1


# ---------------------------------------------------------------------------
# Mod display name -- real bug report: a Bread and Fred mod showed as
# "Mod -- 0100AF401B6A4000" (its own TITLE_ID) instead of "Bread and Fred",
# because a mod folder's OWN name is always just its TITLE_ID (the
# atmosphere/contents/<TITLE_ID>/ convention leaves no product name in the
# path at all). Fixed via queue_worker.find_family_base_name_source(),
# shared by both the Library grouping here and Queue/History
# (tests/test_queue_worker.py has the display_name_for_job()-level
# coverage for that side).
# ---------------------------------------------------------------------------

def test_mod_with_no_family_shows_bare_title_id_honestly(isolated_db):
    """Nothing else in the library shares this TITLE_ID -- must NOT invent
    a name, the bare TITLE_ID is the honest, correct fallback."""
    conn, _inbox_dir = isolated_db
    _mod_item(conn, "SomeMod/atmosphere/contents/0100AF401B6A4000", title_id="0100AF401B6A4000")
    view = services.list_library_view(conn, kind="mods")
    assert len(view["entries"]) == 1
    assert view["entries"][0]["name"] == "0100AF401B6A4000"


def test_mod_borrows_base_games_name(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(
        conn, "Bread and Fred [0100AF401B6A4000][v0] (0.41 GB).nsp",
        title_id="0100AF401B6A4000", content_hash="base",
    )
    _mod_item(
        conn, "Bread and Fred [NSZ]/Russian Language Mod/atmosphere/contents/0100AF401B6A4000",
        title_id="0100AF401B6A4000",
    )

    # Flat "mods" view (its own dedicated tab) picks up the resolved name too.
    mods_view = services.list_library_view(conn, kind="mods")
    assert len(mods_view["entries"]) == 1
    assert mods_view["entries"][0]["name"] == "Bread and Fred [0100AF401B6A4000][v0] (0.41 GB)"

    # Nested under the game in the grouped "games" view as well.
    games_view = services.list_library_view(conn, kind="games")
    assert len(games_view["games"]) == 1
    mod_entry = games_view["games"][0]["mods"][0]
    assert mod_entry["name"] == "Bread and Fred [0100AF401B6A4000][v0] (0.41 GB)"
    # TITLE_ID remains separately available as technical info, never
    # discarded just because it's no longer used as the display name.
    assert mod_entry["title_id"] == "0100AF401B6A4000"


def test_mod_falls_back_to_update_name_when_no_base_indexed(isolated_db):
    """The base game isn't downloaded/indexed, but an Update is -- borrow
    ITS name rather than showing nothing better than the bare TITLE_ID."""
    conn, _inbox_dir = isolated_db
    _item(
        conn, "Bread and Fred Update [0100AF401B6A4800][v131072].nsp",
        title_id="0100AF401B6A4800", content_hash="update",
    )
    _mod_item(conn, "SomeMod/atmosphere/contents/0100AF401B6A4000", title_id="0100AF401B6A4000")

    view = services.list_library_view(conn, kind="mods")
    assert view["entries"][0]["name"] == "Bread and Fred Update [0100AF401B6A4800][v131072]"


def test_mod_prefers_base_over_update_when_both_indexed(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "Bread and Fred [0100AF401B6A4000][v0].nsp", title_id="0100AF401B6A4000", content_hash="base")
    _item(
        conn, "Bread and Fred Update [0100AF401B6A4800][v131072].nsp",
        title_id="0100AF401B6A4800", content_hash="update",
    )
    _mod_item(conn, "SomeMod/atmosphere/contents/0100AF401B6A4000", title_id="0100AF401B6A4000")

    view = services.list_library_view(conn, kind="mods")
    assert view["entries"][0]["name"] == "Bread and Fred [0100AF401B6A4000][v0]"
