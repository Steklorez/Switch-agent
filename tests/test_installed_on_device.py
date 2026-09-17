"""Tests for the "confirmed on device" cross-reference (services.py's
_library_entry_view/list_library/list_library_view, fed by
WebContext.get_known_installed_title_ids()) -- a live, direct signal from
DBI's own "InstalledApplications.csv" (see mtp/windows.py's
list_installed_title_ids()/parse_installed_applications_csv(), real-
hardware-confirmed 2026-09-15), matched by exact BASE title id, deliberately
independent of the job-history-based INSTALLED/INSTALLED_UNVERIFIED status
tested elsewhere. Pure service-layer tests against a plain isolated_db,
matching tests/test_library_grouping.py's own style.
"""

from __future__ import annotations

from switchagent import db
from switchagent.web import services


def _item(conn, path, *, title_id_value, content_type="GAME_PACKAGE"):
    return db.upsert_library_item(
        conn, absolute_path=path, item_type="FILE", file_type="NSP", size=1000, mtime=0.0,
        content_hash=path, title_id=title_id_value, title_id_source="filename",
        status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        content_type=content_type, package_format="NSP",
    )


def _mod_item(conn, path, *, title_id_value):
    return db.upsert_library_item(
        conn, absolute_path=path, item_type="MOD_FOLDER", file_type="ATMOSPHERE_MOD", size=1000, mtime=0.0,
        content_hash=path, title_id=title_id_value, title_id_source="atmosphere_path",
        title_id_confident=True, status="AVAILABLE", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
        content_type="ATMOSPHERE_MOD",
    )


def test_confirmed_on_device_true_for_a_matching_base_title_id(isolated_db):
    conn, _inbox_dir = isolated_db
    _item(conn, "Celeste.nsp", title_id_value="01002B30028F6000")
    _item(conn, "Cocoon.nsp", title_id_value="01002E700C366000")

    view = services.list_library_view(
        conn, kind="games", installed_on_device_base_ids={"01002B30028F6000"},
    )
    by_name = {g["name"]: g for g in view["games"]}
    assert by_name["Celeste"]["confirmed_on_device"] is True
    assert by_name["Cocoon"]["confirmed_on_device"] is False


def test_confirmed_on_device_matches_a_dlc_entry_through_its_recovered_base_id(isolated_db):
    """Real CSV shape: a DLC row's own id (e.g. ...1001) is what DBI
    reports for an installed DLC; mtp/windows.py's CSV parser already
    reduces it to its BASE id before this set is built. A DLC-only library
    entry (base game not itself in the library) must still match through
    its OWN recovered base_title_id (family_base_title_id) -- both sides
    agree on the same base."""
    conn, _inbox_dir = isolated_db
    _item(conn, "River City Girls 2 DLC 1.nsp", title_id_value="0100026016321001")

    view = services.list_library_view(
        conn, kind="games", installed_on_device_base_ids={"0100026016320000"},
    )
    assert view["games"][0]["confirmed_on_device"] is True


def test_confirmed_on_device_false_by_default_when_no_information_given(isolated_db):
    """installed_on_device_base_ids=None (the default) means "no
    information available" -- must never be mistaken for "nothing is
    installed", but every entry's own confirmed_on_device still comes back
    a plain False, never None/missing, so templates never have to
    special-case it."""
    conn, _inbox_dir = isolated_db
    _item(conn, "Celeste.nsp", title_id_value="01002B30028F6000")

    view = services.list_library_view(conn, kind="games")
    assert view["games"][0]["confirmed_on_device"] is False

    flat = services.list_library(conn)
    assert flat[0]["confirmed_on_device"] is False


def test_confirmed_on_device_never_set_for_a_mod_even_if_its_family_matches(isolated_db):
    """A mod is never NCA-installed via DBI, so its own field must stay
    False even when the family-level aggregate (via the base) is True."""
    conn, _inbox_dir = isolated_db
    _item(conn, "Celeste.nsp", title_id_value="01002B30028F6000")
    _mod_item(conn, "atmosphere/contents/01002B30028F6000", title_id_value="01002B30028F6000")

    installed = {"01002B30028F6000"}
    view = services.list_library_view(conn, kind="games", installed_on_device_base_ids=installed)
    game = view["games"][0]
    assert game["confirmed_on_device"] is True  # family-level: the base matched
    assert game["mods"][0]["confirmed_on_device"] is False  # the mod's own field never does

    mods_view = services.list_library_view(conn, kind="mods", installed_on_device_base_ids=installed)
    assert mods_view["entries"][0]["confirmed_on_device"] is False


def test_confirmed_on_device_promotes_installed_unverified_to_installed(isolated_db):
    """A bare DONE_UNVERIFIED job status must never claim "installed" on
    its own (this module's own docstring rule) -- but once DBI's live CSV
    independently confirms the same base title_id is actually on the
    console, that's at least as strong as UI-003's manual user
    confirmation, which already promotes this exact case (see
    services._library_entry_view). The raw transport outcome is untouched
    -- only the entry's user-facing `status` summary is promoted."""
    base_title_id = "01002B30028F6000"
    conn, _inbox_dir = isolated_db
    item_id = _item(conn, "BreadAndFred.nsp", title_id_value=base_title_id)
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
        target_device_id="mock-switch-parent", library_item_id=item_id,
    )
    db.update_job_status(conn, job_id, "DONE_UNVERIFIED")

    unconfirmed = services.list_library_view(conn, kind="games")
    assert unconfirmed["games"][0]["base"]["status"] == "INSTALLED_UNVERIFIED"

    confirmed = services.list_library_view(
        conn, kind="games", installed_on_device_base_ids={base_title_id},
    )
    entry = confirmed["games"][0]["base"]
    assert entry["confirmed_on_device"] is True
    assert entry["status"] == "INSTALLED"
    # The transport fact itself is never rewritten -- job_view still says
    # what actually happened, this is a display-only promotion.
    assert entry["job"]["status"] == "DONE_UNVERIFIED"


def test_set_device_installed_base_title_ids_replaces_wholesale(isolated_db):
    """Each call is a full replacement of that device's confirmed set, not
    a merge -- a title genuinely no longer reported by a fresh CSV read
    must stop being claimed, never linger from a previous call."""
    conn, _inbox_dir = isolated_db
    db.set_device_installed_base_title_ids(conn, "switch-a", {"AAA", "BBB"})
    assert db.get_all_confirmed_installed_base_title_ids(conn) == {"AAA", "BBB"}

    db.set_device_installed_base_title_ids(conn, "switch-a", {"BBB", "CCC"})
    assert db.get_all_confirmed_installed_base_title_ids(conn) == {"BBB", "CCC"}


def test_get_all_confirmed_installed_base_title_ids_unions_across_devices(isolated_db):
    conn, _inbox_dir = isolated_db
    db.set_device_installed_base_title_ids(conn, "switch-a", {"AAA"})
    db.set_device_installed_base_title_ids(conn, "switch-b", {"BBB"})
    assert db.get_all_confirmed_installed_base_title_ids(conn) == {"AAA", "BBB"}

    # Replacing switch-a's set never touches switch-b's own rows.
    db.set_device_installed_base_title_ids(conn, "switch-a", set())
    assert db.get_all_confirmed_installed_base_title_ids(conn) == {"BBB"}
