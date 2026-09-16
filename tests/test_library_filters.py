"""W3-003: Library search / filters / sorting.

Service-layer tests against services.list_library_view() (the Library
page's own read), plus a few HTTP-level ones for the toolbar's wiring and
its on-screen wording. The pre-existing basic filters (`kind`, `format`)
are already covered by tests/test_library_grouping.py and are deliberately
not re-tested here -- this file is about the capability W3-003 ADDS on top
of them.

The family-level sort semantics this file locks in (see
services._FAMILY_SORT_KEYS for the same list in prose):
  size          = SUM of every variant's size in the family
  date_added    = MAX(first_seen_at) across every variant
  last_scanned  = MAX(last_scanned_at) across every variant
  name          = the family's display name, case-insensitive
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders
from switchagent.web import services
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

PARENT = "mock-switch-parent"
CHILD = "mock-switch-child"

GAME_A_BASE = "0100000000010000"
GAME_A_UPDATE = "0100000000010800"
GAME_A_DLC = "0100000000011001"
GAME_B_BASE = "0100000000020000"
GAME_C_BASE = "0100000000030000"
GAME_D_BASE = "0100000000040000"  # the base title_id this family is keyed by -- deliberately never seeded itself
GAME_D_UPDATE = "0100000000040800"


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


@pytest.fixture
def conn(web_ctx):
    with db.open_db(web_ctx.db_path) as c:
        yield c


def _package(conn, path, title_id, *, size=1000, status="AVAILABLE", content_hash=None,
             first_seen=None, last_scanned=None, error=None, mtime=0.0):
    item_id = db.upsert_library_item(
        conn, absolute_path=path, item_type="FILE", file_type="NSP", size=size, mtime=mtime,
        content_hash=content_hash or path, title_id=title_id, title_id_source="filename",
        status=status, suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        content_type="GAME_PACKAGE", package_format="NSP", error=error,
    )
    _stamp(conn, item_id, first_seen, last_scanned)
    return item_id


def _mod(conn, path, title_id, *, size=500, first_seen=None, last_scanned=None):
    item_id = db.upsert_library_item(
        conn, absolute_path=path, item_type="MOD_FOLDER", file_type="ATMOSPHERE_MOD", size=size,
        mtime=0.0, content_hash=path, title_id=title_id, title_id_source="atmosphere_path",
        title_id_confident=True, status="AVAILABLE", suggested_action="COPY_MERGE",
        suggested_target="SD_CARD", content_type="ATMOSPHERE_MOD",
    )
    _stamp(conn, item_id, first_seen, last_scanned)
    return item_id


def _stamp(conn, item_id, first_seen, last_scanned):
    """first_seen_at/last_scanned_at are written by the scanner, not by
    upsert's public parameters -- set them directly so the sort semantics
    can be asserted against exact, known values."""
    if first_seen is not None:
        conn.execute("UPDATE library_items SET first_seen_at = ? WHERE id = ?", (first_seen, item_id))
    if last_scanned is not None:
        conn.execute("UPDATE library_items SET last_scanned_at = ? WHERE id = ?", (last_scanned, item_id))
    conn.commit()


def _history(conn, *, title_id, device_id, outcome, user_verified=None, storage="SD_INSTALL"):
    inbox_id = _inbox_source(conn)
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage=storage,
        target_device_id=device_id, inbox_item_id=inbox_id,
    )
    db.update_job_status(conn, job_id, outcome)
    history_id = db.record_install_history(
        conn, job_id=job_id, title_id=title_id, display_name="seeded",
        target_device_id=device_id, target_storage=storage, outcome=outcome, bytes_total=1,
    )
    if user_verified is not None:
        db.set_user_verified_outcome(conn, history_id, user_verified)
    return history_id


def _inbox_source(conn) -> int:
    existing = conn.execute("SELECT id FROM inbox_items LIMIT 1").fetchone()
    if existing is not None:
        return existing["id"]
    return db.upsert_inbox_item(
        conn, relative_path="seed.nsp", item_type="FILE", file_type="NSP", size=1, mtime=0.0,
        content_hash="seed", title_id=None, title_id_source=None, status="AVAILABLE",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )


def _family_ids(view) -> list[str]:
    return [g["base_title_id"] for g in view["games"]]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_matches_display_name(conn):
    _package(conn, "Zelda Adventure.nsp", GAME_A_BASE)
    _package(conn, "Mario Kart.nsp", GAME_B_BASE)
    view = services.list_library_view(conn, search="zelda")
    assert _family_ids(view) == [GAME_A_BASE]


def test_search_matches_filename_even_when_the_display_name_differs(conn):
    """A mod's DISPLAY name is borrowed from its family's base game (the
    Bread and Fred fix) -- its own file/folder path is still searchable."""
    _package(conn, "Bread and Fred [0100AF401B6A4000][v0].nsp", "0100AF401B6A4000")
    _mod(conn, "RussianLanguageMod/atmosphere/contents/0100AF401B6A4000", "0100AF401B6A4000")

    mods = services.list_library_view(conn, kind="mods", search="russianlanguagemod")["entries"]
    assert len(mods) == 1
    # ...and it still DISPLAYS the borrowed family name, unchanged.
    assert mods[0]["name"] == "Bread and Fred [0100AF401B6A4000][v0]"
    assert mods[0]["mod_name"] == "RussianLanguageMod"


def test_searching_a_mod_name_surfaces_its_whole_game_in_the_grouped_view(conn):
    """Regression, found in a live run: search used to prune ENTRIES before
    grouping, so searching a mod's own name pruned away every GAME_PACKAGE
    in its family -- and since families are built from those packages, the
    game vanished from the Games view entirely instead of surfacing. Search
    now matches whole families there: any entry matching brings the card,
    with its updates/DLC/mods intact."""
    _package(conn, "Bread and Fred [0100AF401B6A4000][v0].nsp", "0100AF401B6A4000")
    _package(conn, "Bread and Fred Patch [0100AF401B6A4800][v0].nsp", "0100AF401B6A4800")
    _mod(conn, "Russian Language Mod/atmosphere/contents/0100AF401B6A4000", "0100AF401B6A4000")
    _package(conn, "Unrelated.nsp", GAME_B_BASE)

    view = services.list_library_view(conn, kind="games", search="russian language")
    assert _family_ids(view) == ["0100AF401B6A4000"]
    game = view["games"][0]
    assert game["base"] is not None          # the whole family, not just the mod
    assert len(game["updates"]) == 1
    assert len(game["mods"]) == 1


def test_family_search_keeps_siblings_that_do_not_match_individually(conn):
    """Searching a game's name returns its update/DLC too, even though
    their own filenames/TITLE_IDs do not contain the needle."""
    _package(conn, "Zelda Adventure.nsp", GAME_A_BASE)
    _package(conn, "Patch File.nsp", GAME_A_UPDATE)
    _package(conn, "Mario Kart.nsp", GAME_B_BASE)

    view = services.list_library_view(conn, kind="games", search="zelda")
    assert _family_ids(view) == [GAME_A_BASE]
    assert [e["name"] for e in view["games"][0]["updates"]] == ["Patch File"]


def test_search_matches_title_id(conn):
    _package(conn, "Alpha.nsp", GAME_A_BASE)
    _package(conn, "Beta.nsp", GAME_B_BASE)
    assert _family_ids(services.list_library_view(conn, search=GAME_B_BASE)) == [GAME_B_BASE]


def test_search_matches_base_title_id_of_an_update_or_dlc(conn):
    """Searching a BASE id must also find its update/DLC, whose own
    TITLE_IDs are different strings that never contain the base's hex."""
    _package(conn, "Alpha Patch.nsp", GAME_A_UPDATE)
    _package(conn, "Alpha Bonus.nsp", GAME_A_DLC)
    _package(conn, "Unrelated.nsp", GAME_B_BASE)

    view = services.list_library_view(conn, search=GAME_A_BASE)
    assert _family_ids(view) == [GAME_A_BASE]
    assert len(view["games"][0]["updates"]) == 1
    assert len(view["games"][0]["dlc"]) == 1


def test_search_is_case_insensitive_across_every_field(conn):
    """Each of the five fields _search_haystack() actually matches on --
    display name, filename, an entry's own title_id, its family's
    base_title_id, and a mod's own distribution name -- gets its own,
    genuinely distinct case-varied probe. A single "MiXeD CaSe" filename
    only exercises name/filename together (they render near-identically
    for a plain package), which is why the previous version of this test
    never actually varied case on title_id/base_title_id/mod_name at
    all."""
    _package(conn, "MiXeD CaSe Game.NSP", GAME_A_BASE)
    for needle in ("mixed case", "MIXED CASE"):
        assert _family_ids(services.list_library_view(conn, search=needle)) == [GAME_A_BASE], needle
    # Display name is .stem'd (extension stripped) -- ".NSP" only ever
    # appears in the raw filename field, so this specifically proves
    # filename (not just display name) is searched, case-insensitively.
    for needle in (".nsp", ".NSP"):
        assert _family_ids(services.list_library_view(conn, search=needle)) == [GAME_A_BASE], needle

    # An UPDATE's own title_id differs from its family's base_title_id --
    # searching each, in both cases, proves both fields are matched
    # independently, not just "the one title_id a base item happens to
    # share with its own family".
    _package(conn, "MixedCase Update.nsp", GAME_A_UPDATE)
    for needle in (GAME_A_UPDATE.lower(), GAME_A_UPDATE.upper()):
        assert GAME_A_BASE in _family_ids(services.list_library_view(conn, search=needle)), needle
    for needle in (GAME_A_BASE.lower(), GAME_A_BASE.upper()):
        assert GAME_A_BASE in _family_ids(services.list_library_view(conn, search=needle)), needle

    # A mod's own distribution name (recovered from a path segment above
    # atmosphere/contents/<TITLE_ID> -- never the displayed name, which
    # stays borrowed from its family) is a fifth, independent field.
    _mod(conn, "MixedCaseModName/atmosphere/contents/" + GAME_A_BASE, GAME_A_BASE)
    for needle in ("mixedcasemodname", "MIXEDCASEMODNAME"):
        assert GAME_A_BASE in _family_ids(services.list_library_view(conn, search=needle)), needle


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------

def _seed_filter_corpus(conn):
    """One family of each shape the filter list names by name."""
    _package(conn, "WithEverything.nsp", GAME_A_BASE)
    _package(conn, "WithEverything Patch.nsp", GAME_A_UPDATE)
    _package(conn, "WithEverything Bonus.nsp", GAME_A_DLC)
    _mod(conn, "SomeMod/atmosphere/contents/" + GAME_A_BASE, GAME_A_BASE)
    _package(conn, "WithEverything (copy).nsp", GAME_A_BASE, content_hash="dup")

    _package(conn, "PlainBase.nsp", GAME_B_BASE)

    _package(conn, "BrokenOne.nsp", GAME_C_BASE, status="ERROR", error="extraction failed")

    # A family with NO base game at all -- update-only, exactly the shape
    # test_family_with_no_base_game_still_renders (test_game_detail.py)
    # proves is real. Without this, every family in the corpus has a base
    # item, so "base"'s own filter predicate could be replaced with
    # `lambda g, a: True` and every test below would still pass.
    _package(conn, "OrphanUpdate.nsp", GAME_D_UPDATE)


def test_filter_all_returns_every_family(conn):
    _seed_filter_corpus(conn)
    assert set(_family_ids(services.list_library_view(conn, group_filter="all"))) == {
        GAME_A_BASE, GAME_B_BASE, GAME_C_BASE, GAME_D_BASE,
    }


@pytest.mark.parametrize("group_filter,expected", [
    ("base", {GAME_A_BASE, GAME_B_BASE, GAME_C_BASE}),  # GAME_D has no base item -- correctly excluded
    ("updates", {GAME_A_BASE, GAME_D_BASE}),
    ("dlc", {GAME_A_BASE}),
    ("mods", {GAME_A_BASE}),
    ("duplicates", {GAME_A_BASE}),
])
def test_structural_filters(conn, group_filter, expected):
    _seed_filter_corpus(conn)
    assert set(_family_ids(services.list_library_view(conn, group_filter=group_filter))) == expected


# ---------------------------------------------------------------------------
# sorting -- family-level semantics, locked in
# ---------------------------------------------------------------------------

def test_size_sorts_by_the_sum_of_every_variant_in_the_family(conn):
    """Documented decision: a family's size is the TOTAL of all its
    variants, not just its base package. Family A's base is smaller than
    family B's, but A's update+DLC+mod make the family bigger overall --
    so A must sort first descending."""
    _package(conn, "A.nsp", GAME_A_BASE, size=100)
    _package(conn, "A Patch.nsp", GAME_A_UPDATE, size=400)
    _package(conn, "A Bonus.nsp", GAME_A_DLC, size=300)
    _mod(conn, "AMod/atmosphere/contents/" + GAME_A_BASE, GAME_A_BASE, size=200)
    _package(conn, "B.nsp", GAME_B_BASE, size=500)

    view = services.list_library_view(conn, sort="size", reverse=True)
    assert _family_ids(view) == [GAME_A_BASE, GAME_B_BASE]
    assert view["games"][0]["total_size"] == 1000  # 100 + 400 + 300 + 200
    assert view["games"][1]["total_size"] == 500

    ascending = services.list_library_view(conn, sort="size", reverse=False)
    assert _family_ids(ascending) == [GAME_B_BASE, GAME_A_BASE]


def test_recently_added_sorts_by_the_newest_variant_in_the_family(conn):
    """Documented decision: date_added = MAX(first_seen_at) across the
    family -- downloading a DLC today resurfaces an old game as recently
    added, which is the useful behaviour."""
    _package(conn, "Old.nsp", GAME_A_BASE, first_seen="2020-01-01T00:00:00+00:00")
    _package(conn, "Old Bonus.nsp", GAME_A_DLC, first_seen="2026-06-01T00:00:00+00:00")
    _package(conn, "Newer.nsp", GAME_B_BASE, first_seen="2025-01-01T00:00:00+00:00")

    view = services.list_library_view(conn, sort="date_added", reverse=True)
    assert _family_ids(view) == [GAME_A_BASE, GAME_B_BASE]
    assert view["games"][0]["first_seen_at"] == "2026-06-01T00:00:00+00:00"


def test_date_modified_sorts_by_the_newest_mtime_in_the_family(conn):
    """_FAMILY_SORT_KEYS["date_modified"] had zero coverage before this --
    same MAX-across-the-family pattern as date_added/last_scanned above."""
    _package(conn, "OldContent.nsp", GAME_A_BASE, mtime=1000.0)
    _package(conn, "NewerContent.nsp", GAME_B_BASE, mtime=2000.0)

    view = services.list_library_view(conn, sort="date_modified", reverse=True)
    assert _family_ids(view) == [GAME_B_BASE, GAME_A_BASE]


def test_last_scanned_sorts_by_the_most_recently_scanned_variant(conn):
    """Documented decision: last_scanned = MAX(last_scanned_at) across the
    family. Deliberately distinct from date_added -- proven here by giving
    the two columns opposite orderings."""
    _package(conn, "ScannedRecently.nsp", GAME_A_BASE,
             first_seen="2020-01-01T00:00:00+00:00", last_scanned="2026-09-01T00:00:00+00:00")
    _package(conn, "AddedRecently.nsp", GAME_B_BASE,
             first_seen="2026-08-01T00:00:00+00:00", last_scanned="2021-01-01T00:00:00+00:00")

    by_scan = services.list_library_view(conn, sort="last_scanned", reverse=True)
    assert _family_ids(by_scan) == [GAME_A_BASE, GAME_B_BASE]
    assert by_scan["games"][0]["last_scanned_at"] == "2026-09-01T00:00:00+00:00"

    by_added = services.list_library_view(conn, sort="date_added", reverse=True)
    assert _family_ids(by_added) == [GAME_B_BASE, GAME_A_BASE]


def test_name_sort_is_case_insensitive_and_reversible(conn):
    _package(conn, "banana.nsp", GAME_A_BASE)
    _package(conn, "Apple.nsp", GAME_B_BASE)
    _package(conn, "cherry.nsp", GAME_C_BASE)

    ascending = services.list_library_view(conn, sort="name", reverse=False)
    assert [g["name"] for g in ascending["games"]] == ["Apple", "banana", "cherry"]
    descending = services.list_library_view(conn, sort="name", reverse=True)
    assert [g["name"] for g in descending["games"]] == ["cherry", "banana", "Apple"]


def test_flat_kinds_keep_their_per_entry_sorting(conn):
    """kind=updates/dlc/mods are flat lists with no families -- they keep
    the existing per-entry sort, including the new "last_scanned" key."""
    _package(conn, "A Patch.nsp", GAME_A_UPDATE, last_scanned="2020-01-01T00:00:00+00:00")
    _package(conn, "B Patch.nsp", "0100000000020800", last_scanned="2026-01-01T00:00:00+00:00")

    entries = services.list_library_view(conn, kind="updates", sort="last_scanned", reverse=True)["entries"]
    assert [e["name"] for e in entries] == ["B Patch", "A Patch"]


# ---------------------------------------------------------------------------
# HTTP wiring + on-screen wording
# ---------------------------------------------------------------------------

def test_library_page_exposes_every_new_control(client, web_ctx):
    with db.open_db(web_ctx.db_path) as conn:
        _seed_filter_corpus(conn)

    html = client.get("/").text
    for value in ("base", "updates", "dlc", "mods", "duplicates"):
        assert f'value="{value}"' in html, value
    for label in ("Recently added", "Last scanned", "Size", "Name"):
        assert f">{label}<" in html, label
    # No raw device_id ever reaches this page's HTML.
    assert PARENT not in html


def test_library_page_filters_end_to_end_over_http(client, web_ctx):
    with db.open_db(web_ctx.db_path) as conn:
        _package(conn, "KeepMe.nsp", GAME_A_BASE)
        _package(conn, "KeepMe Bonus.nsp", GAME_A_DLC)
        _package(conn, "DropMe.nsp", GAME_B_BASE)

    html = client.get("/?filter=dlc").text
    assert "KeepMe" in html
    assert "DropMe" not in html

    unfiltered = client.get("/").text
    assert "KeepMe" in unfiltered and "DropMe" in unfiltered
