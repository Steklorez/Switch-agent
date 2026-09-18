"""Server-side half of "Install button stayed disabled after connecting
the Switch mid-session, without a page reload" -- the actual live-update
behavior is client-side (library.js's refreshSelectionTarget(), polling
/api/devices every 5s and rebuilding only #selection-target-area), which
has no Python-side runtime to unit test here. Verified directly in a
browser against the real dev server instead (patched window.fetch to
simulate 2 -> 1 -> 0 connected devices with no reload; Install correctly
enabled/disabled and the target <select> repopulated each time, exactly
matching what these tests assert about the underlying template contract
that JS logic depends on.

What IS testable here: the #selection-target-area wrapper the JS
rebuilds must actually exist, in the same place, on every page that
loads library.js -- both Library and Game Details, since
_install_selection.html is shared between them (see its own docstring)."""
from pathlib import Path

from .test_web_api import web_ctx, client
from .test_game_detail import BASE_TITLE_ID, _add_item


def test_selection_target_area_wraps_all_three_connection_states(client, web_ctx):
    """library.js's refreshSelectionTarget() rebuilds #selection-target-area
    wholesale -- if the server-rendered markup doesn't wrap ALL THREE
    branches (no device / multiple devices / exactly one) in that same
    id, the very first live poll tick would duplicate or orphan content
    instead of cleanly replacing it."""
    html = client.get("/").text
    assert 'id="selection-target-area"' in html
    start = html.index('id="selection-target-area"')
    # The wrapper's closing </div> must come AFTER install-selected-btn's
    # opening tag is NOT inside it -- i.e. the button is a sibling, not a
    # child (rebuilding the area must never touch the button itself).
    button_pos = html.index('id="install-selected-btn"')
    assert start < button_pos


def test_selection_target_area_present_on_game_detail_page_too(client, web_ctx):
    """_install_selection.html is shared verbatim between Library and
    Game Details specifically so one selection/install implementation
    (and one live-reconnect fix) covers both -- never a second, drifting
    copy. A regression here would silently only fix the bug on one of
    the two pages."""
    _add_item(web_ctx, f"ZzzQuest [{BASE_TITLE_ID}][v0].nsp", BASE_TITLE_ID)
    library_html = client.get("/").text
    detail_html = client.get(f"/games/{BASE_TITLE_ID}").text
    assert 'id="selection-target-area"' in library_html
    assert 'id="selection-target-area"' in detail_html


def test_target_select_uses_let_binding_so_it_can_be_reassigned():
    """The old bug: targetSelect was `const`, captured once at page load
    -- reconnecting mid-session had nowhere to put the freshly-rebuilt
    <select> the poll creates. A regression back to `const` here would
    silently reintroduce the exact bug (a TypeError on reassignment,
    caught nowhere, that just quietly stops the poll from ever working
    again) without any other test here catching it."""
    js = (Path(__file__).parent.parent
          / "switchagent" / "web" / "static" / "library.js").read_text(encoding="utf-8")
    assert "let targetSelect" in js
    assert "const targetSelect" not in js
