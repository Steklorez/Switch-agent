"""W3-001: the Game Details page (`GET /games/{base_title_id}`).

Same MockMtpBackend-based web fixture style as tests/test_web_api.py (no
real hardware, worker never started -- outcomes are driven explicitly).
These tests are specifically about the page's two hard contracts:

  1. it renders the real family structure (Base / Update / DLC / Mod /
     duplicates / needs-review) out of the SAME grouping the Library page
     uses, including the family-with-no-base case; and
  2. it never states installation as a fact that SwitchAgent cannot back
     up -- the DONE vs DONE_UNVERIFIED transport distinction and the
     separate user console confirmation are both rendered, and a bare
     DONE_UNVERIFIED row implies nothing.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders
from switchagent.mtp.windows import device_fingerprint
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

BASE_TITLE_ID = "0100000000010000"
UPDATE_TITLE_ID = "0100000000010800"
DLC_TITLE_ID = "0100000000011001"
OTHER_TITLE_ID = "0100000000090000"


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
        ctx.refresh_devices(conn)  # stands in for the worker's first tick
    yield ctx


@pytest.fixture
def client(web_ctx):
    return TestClient(create_app(web_ctx))


def _add_item(web_ctx, name, title_id, *, content_type="GAME_PACKAGE", item_type="FILE",
              size=1000, status="AVAILABLE", content_hash=None, error=None):
    path = config.LIBRARY_DIR / name
    if item_type == "MOD_FOLDER":
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.write_bytes(b"x" * size)
    with db.open_db(web_ctx.db_path) as conn:
        return db.upsert_library_item(
            conn, absolute_path=str(path), item_type=item_type,
            file_type="NSP" if item_type == "FILE" else "ATMOSPHERE_MOD",
            size=size, mtime=0.0, content_hash=content_hash or name, title_id=title_id,
            title_id_source="filename", status=status,
            suggested_action="INSTALL_VIA_DBI" if content_type == "GAME_PACKAGE" else "COPY_MERGE",
            suggested_target="SD_INSTALL" if content_type == "GAME_PACKAGE" else "SD_CARD",
            content_type=content_type, package_format="NSP" if item_type == "FILE" else None,
            error=error,
        )


def _seed_full_family(web_ctx) -> dict:
    # Filenames carry their own bracketed TITLE_ID, matching how real
    # scene-group releases are named -- preview.preview_path() resolves a
    # package's TITLE_ID from the filename, so an install started from this
    # page only works end-to-end with realistic names.
    return {
        "base": _add_item(web_ctx, f"ZzzQuest [{BASE_TITLE_ID}][v0].nsp", BASE_TITLE_ID),
        "update": _add_item(web_ctx, f"ZzzQuest Patch [{UPDATE_TITLE_ID}][v65536].nsp", UPDATE_TITLE_ID),
        "dlc": _add_item(web_ctx, f"ZzzQuest Bonus Chapter [{DLC_TITLE_ID}][v0].nsp", DLC_TITLE_ID),
        "mod": _add_item(
            web_ctx, "RussianVoicePack/atmosphere/contents/" + BASE_TITLE_ID, BASE_TITLE_ID,
            content_type="ATMOSPHERE_MOD", item_type="MOD_FOLDER",
        ),
    }


def _record_history(web_ctx, *, title_id, device_id, outcome, display_name="ZzzQuest",
                    user_verified=None, storage="SD_INSTALL", error=None):
    """A real jobs row + its install_history row, via the real db helpers --
    never a hand-written INSERT, so the schema/FK contract is exercised."""
    with db.open_db(web_ctx.db_path) as conn:
        job_id = db.create_job(
            conn, action="INSTALL_VIA_DBI", target_storage=storage, target_device_id=device_id,
            inbox_item_id=_ensure_inbox_item(conn),
        )
        db.update_job_status(conn, job_id, outcome)
        history_id = db.record_install_history(
            conn, job_id=job_id, title_id=title_id, display_name=display_name,
            target_device_id=device_id, target_storage=storage, outcome=outcome,
            error=error, bytes_total=1000,
        )
        if user_verified is not None:
            db.set_user_verified_outcome(conn, history_id, user_verified)
        return history_id


def _ensure_inbox_item(conn) -> int:
    """jobs requires exactly one of library_item_id/inbox_item_id; history
    rows in these tests are about outcomes, not about a specific source, so
    a single shared inbox row keeps them independent of the library items
    the page itself renders."""
    existing = conn.execute("SELECT id FROM inbox_items LIMIT 1").fetchone()
    if existing is not None:
        return existing["id"]
    return db.upsert_inbox_item(
        conn, relative_path="history-source.nsp", item_type="FILE", file_type="NSP",
        size=1000, mtime=0.0, content_hash="history-source", title_id=None,
        title_id_source=None, status="AVAILABLE", suggested_action="INSTALL_VIA_DBI",
        suggested_target="SD_INSTALL",
    )


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

def test_full_family_renders_every_section(client, web_ctx):
    """Base + Update + DLC + Mod all present -> every section renders, with
    the right entry under each one. Grouping comes from the same
    services.list_library_view(kind="games") the Library page uses, so a
    regression in either shows up here."""
    _seed_full_family(web_ctx)

    res = client.get(f"/games/{BASE_TITLE_ID}")
    assert res.status_code == 200
    html = res.text

    for heading in ("Base Game", "Updates", "DLC", "Mods", "SwitchAgent activity"):
        assert f"<h3>{heading}" in html or f">{heading}<" in html, heading

    # Counters in the header reflect reality.
    assert "Updates: <strong>1</strong>" in html
    assert "DLC: <strong>1</strong>" in html
    assert "Mods: <strong>1</strong>" in html
    assert BASE_TITLE_ID in html

    # Each entry shows its own filename, content type, TITLE_ID and size.
    assert f"ZzzQuest [{BASE_TITLE_ID}][v0].nsp" in html
    assert f"ZzzQuest Patch [{UPDATE_TITLE_ID}][v65536].nsp" in html
    assert f"ZzzQuest Bonus Chapter [{DLC_TITLE_ID}][v0].nsp" in html
    assert UPDATE_TITLE_ID in html
    assert DLC_TITLE_ID in html
    # The mod's own distribution folder name is shown as technical info
    # here (the Library page deliberately shows the borrowed family name
    # instead -- see tests/test_library_grouping.py).
    assert "RussianVoicePack" in html


def test_family_with_no_base_game_still_renders(client, web_ctx):
    """Update + DLC only, base never downloaded. The page must render
    normally (not 404, not a broken/blank page) and say so explicitly."""
    _add_item(web_ctx, "Orphan Patch.nsp", UPDATE_TITLE_ID)
    _add_item(web_ctx, "Orphan Bonus.nsp", DLC_TITLE_ID)

    res = client.get(f"/games/{BASE_TITLE_ID}")
    assert res.status_code == 200
    html = res.text
    assert 'id="no-base-note"' in html
    assert "not in library" in html
    assert "Orphan Patch.nsp" in html
    assert "Orphan Bonus.nsp" in html
    assert "Updates: <strong>1</strong>" in html
    assert "DLC: <strong>1</strong>" in html


def test_duplicates_and_needs_review_entries_get_their_own_sections(client, web_ctx):
    _add_item(web_ctx, "ZzzQuest.nsp", BASE_TITLE_ID, content_hash="same")
    _add_item(web_ctx, "ZzzQuest (1).nsp", BASE_TITLE_ID, content_hash="same")
    _add_item(web_ctx, "ZzzQuest Broken.nsp", UPDATE_TITLE_ID, status="ERROR", error="extraction failed")

    html = client.get(f"/games/{BASE_TITLE_ID}").text
    assert "Other copies" in html
    assert 'id="needs-review-section"' in html
    assert "extraction failed" in html
    # Not just that the sections exist -- the actual entries are listed in
    # them (a regression rendering an empty section header would otherwise
    # still pass every assertion above).
    assert "ZzzQuest (1).nsp" in html
    assert "ZzzQuest Broken.nsp" in html


def test_unknown_or_malformed_family_is_a_404(client, web_ctx):
    _seed_full_family(web_ctx)
    assert client.get("/games/not-a-title-id").status_code == 404
    assert client.get("/games/FFFFFFFFFFFFFFFF").status_code == 404
    # An UPDATE id canonicalizes onto its own family's page -- not just a
    # 200, but genuinely the SAME family's content (the actual base game
    # entry), rather than, say, a 200 for an accidentally-empty page.
    update_res = client.get(f"/games/{UPDATE_TITLE_ID}").text
    assert "ZzzQuest" in update_res
    assert "Base Game" in update_res
    # ...and lowercase input resolves to the same real family too.
    lower_res = client.get(f"/games/{BASE_TITLE_ID.lower()}").text
    assert "ZzzQuest" in lower_res


# ---------------------------------------------------------------------------
# activity: transport outcome vs console confirmation
# ---------------------------------------------------------------------------

def test_activity_distinguishes_every_outcome_across_multiple_devices(client, web_ctx):
    """The four cases the mandate names, split across the two mock
    Switches: DONE, DONE_UNVERIFIED (unconfirmed), DONE_UNVERIFIED +
    user-confirmed SUCCESS, DONE_UNVERIFIED + user-confirmed FAILED. Each
    must render with its own distinct claim, under the right device, and
    the raw transport outcome must stay visible and unrewritten."""
    _seed_full_family(web_ctx)
    parent, child = "mock-switch-parent", "mock-switch-child"

    _record_history(web_ctx, title_id=BASE_TITLE_ID, device_id=parent, outcome="DONE",
                    storage="SD_CARD", display_name="ZzzQuest base")
    _record_history(web_ctx, title_id=UPDATE_TITLE_ID, device_id=parent, outcome="DONE_UNVERIFIED",
                    display_name="ZzzQuest patch")
    _record_history(web_ctx, title_id=DLC_TITLE_ID, device_id=child, outcome="DONE_UNVERIFIED",
                    user_verified="SUCCESS", display_name="ZzzQuest bonus")
    _record_history(web_ctx, title_id=BASE_TITLE_ID, device_id=child, outcome="DONE_UNVERIFIED",
                    user_verified="FAILED", display_name="ZzzQuest retry")

    html = client.get(f"/games/{BASE_TITLE_ID}").text

    # Per-row claims, all four distinct.
    assert "Installed on Switch — transfer verified by SwitchAgent" in html
    assert "Installed on Switch — you confirmed this on the console" in html
    assert "Transfer completed, but you reported it did not work on the console" in html
    assert "Transfer accepted by the Switch — console result not confirmed" in html

    # The raw transport outcomes stay visible and are never collapsed into
    # one another (DONE and DONE UNVERIFIED are different facts).
    assert "Transport outcome: <strong>DONE</strong>" in html
    assert "Transport outcome: <strong>DONE UNVERIFIED</strong>" in html

    # Console confirmation is a separate, always-stated axis.
    assert "confirmed successful by you" in html
    assert "confirmed failed by you" in html
    assert "not confirmed" in html

    # Each device section carries only its own rows.
    parent_fp, child_fp = device_fingerprint(parent), device_fingerprint(child)
    parent_block = html.split(f'data-device-fingerprint="{parent_fp}"', 1)[1].split("data-device-fingerprint", 1)[0]
    assert "ZzzQuest base" in parent_block
    assert "ZzzQuest patch" in parent_block
    assert "ZzzQuest bonus" not in parent_block
    child_block = html.split(f'data-device-fingerprint="{child_fp}"', 1)[1].split("data-device-fingerprint", 1)[0]
    assert "ZzzQuest bonus" in child_block
    assert "ZzzQuest base" not in child_block


def test_bare_done_unverified_never_claims_the_game_is_installed(client, web_ctx):
    """The central honesty rule: a DONE_UNVERIFIED transport outcome with
    NO user confirmation must never produce text asserting installation.
    Seeds exactly that one row and nothing else, then asserts the whole
    rendered page contains no "installed" claim at all."""
    _seed_full_family(web_ctx)
    _record_history(web_ctx, title_id=BASE_TITLE_ID, device_id="mock-switch-parent",
                    outcome="DONE_UNVERIFIED")

    html = client.get(f"/games/{BASE_TITLE_ID}").text

    assert "Installed on Switch" not in html
    # Nothing anywhere on this page may use the word as a statement of
    # fact for this row -- the honest wording is used instead.
    assert not re.search(r"\binstalled\b", html, re.IGNORECASE), \
        "a bare DONE_UNVERIFIED row must never render an 'installed' claim"
    assert "Transfer accepted by the Switch — console result not confirmed" in html
    assert "Transport outcome: <strong>DONE UNVERIFIED</strong>" in html


def test_known_device_with_no_activity_says_so_rather_than_implying_absence(client, web_ctx):
    _seed_full_family(web_ctx)
    html = client.get(f"/games/{BASE_TITLE_ID}").text
    assert "No transfer for this game has ever been attempted to this Switch by SwitchAgent." in html
    # Both known mock devices get a section, even with zero history.
    for device_id in ("mock-switch-parent", "mock-switch-child"):
        assert f'data-device-fingerprint="{device_fingerprint(device_id)}"' in html


def test_failed_and_interrupted_activity_render_honestly(client, web_ctx):
    _seed_full_family(web_ctx)
    _record_history(web_ctx, title_id=BASE_TITLE_ID, device_id="mock-switch-parent",
                    outcome="FAILED", error="MTP write refused")
    _record_history(web_ctx, title_id=UPDATE_TITLE_ID, device_id="mock-switch-parent",
                    outcome="INTERRUPTED")
    _record_history(web_ctx, title_id=DLC_TITLE_ID, device_id="mock-switch-parent",
                    outcome="DESTINATION_CONFLICT")

    html = client.get(f"/games/{BASE_TITLE_ID}").text
    assert "Transfer failed" in html
    assert "Transfer interrupted" in html
    assert "Destination conflict — nothing was overwritten" in html
    assert "MTP write refused" in html
    assert not re.search(r"\binstalled\b", html, re.IGNORECASE)


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------

def test_game_detail_page_never_renders_raw_device_id(client, web_ctx):
    """Mirrors test_web_api.py's test_queue_and_history_pages_never_render_raw_device_id:
    device_id embeds the device's real USB serial on real hardware (see
    mtp/windows.py's mask_device_id), so it must not appear anywhere in
    this page's HTML -- not as text, not as an attribute value. Only the
    fingerprint and the friendly/display name may."""
    real_looking_id = r"::{GUID}\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    _seed_full_family(web_ctx)
    with db.open_db(web_ctx.db_path) as conn:
        db.upsert_device_seen(conn, real_looking_id, "Switch")
    _record_history(web_ctx, title_id=BASE_TITLE_ID, device_id=real_looking_id, outcome="DONE_UNVERIFIED")
    _record_history(web_ctx, title_id=BASE_TITLE_ID, device_id="mock-switch-parent", outcome="DONE")

    html = client.get(f"/games/{BASE_TITLE_ID}").text
    assert "xtj10229424075" not in html
    assert real_looking_id not in html
    assert "mock-switch-parent" not in html
    # ...but the safe fingerprint stand-in IS there, as the device's link.
    assert device_fingerprint(real_looking_id) in html
    assert device_fingerprint("mock-switch-parent") in html


# ---------------------------------------------------------------------------
# install from this page -- the SAME pipeline as the Library page
# ---------------------------------------------------------------------------

def test_install_selection_markup_matches_the_library_pages_contract(web_ctx):
    """The page reuses /static/library.js verbatim, which binds by these
    exact ids/attributes -- without them the selection + confirm flow
    silently does nothing. Needs exactly one connected device: with the
    default two-mock-device fixture, the install-selection UI
    deliberately renders its "disconnect one" warning instead of a
    picker (see test_install_selection_warns_when_multiple_devices_connected)."""
    ids = _seed_full_family(web_ctx)
    single_device_ctx = build_mock_context(web_ctx.db_path, device_ids=["mock-switch-parent"])
    with db.open_db(web_ctx.db_path) as conn:
        single_device_ctx.refresh_devices(conn)
    single_client = TestClient(create_app(single_device_ctx))
    html = single_client.get(f"/games/{BASE_TITLE_ID}").text

    assert '<script src="/static/library.js"></script>' in html
    for element_id in ("selection-bar", "install-selected-btn", "confirm-modal",
                       "confirm-install-btn", "confirm-list", "target-device"):
        assert f'id="{element_id}"' in html, element_id
    # every family member is selectable, and the base carries the
    # "selecting the game selects the whole family" cascade role
    for item_id in ids.values():
        assert f'class="select-box" value="{item_id}"' in html
    assert f'data-family="{BASE_TITLE_ID}"' in html
    assert 'data-role="base"' in html


def test_install_selection_warns_when_multiple_devices_connected(client, web_ctx):
    """Only one console is ever expected to be connected at a time. The
    default fixture has two connected mock devices (a realistic "which
    one do you mean" scenario) -- installing to "whichever one" can't be
    guessed safely, so the picker is replaced by an explicit warning and
    the Install button stays disabled rather than letting the user
    silently pick between two real, physically-connected consoles."""
    _seed_full_family(web_ctx)
    html = client.get(f"/games/{BASE_TITLE_ID}").text
    assert "Multiple Switches connected" in html
    assert 'id="target-device"' not in html


def test_install_from_game_detail_creates_a_real_job_batch(client, web_ctx):
    """Asserted at the DB level, not just on an HTTP 200: selecting this
    family's items on the detail page and confirming must create CONFIRMED
    jobs sharing ONE installation batch, exactly as the Library page's own
    POST /api/jobs call does (services.create_and_confirm_jobs -- there is
    no second job-creation path). Also proves the page ACTUALLY OFFERS
    these three items for selection in the first place -- a checkbox with
    the right `value` for each -- since posting straight to /api/jobs
    would otherwise prove nothing about this page's own markup, only
    about an endpoint every other page already covers elsewhere."""
    ids = _seed_full_family(web_ctx)
    selected = [ids["base"], ids["update"], ids["dlc"]]

    html = client.get(f"/games/{BASE_TITLE_ID}").text
    for item_id in selected:
        assert f'class="select-box" value="{item_id}"' in html

    res = client.post("/api/jobs", json={
        "library_item_ids": selected, "target_device_id": "mock-switch-parent",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["errors"] == []
    batch_id = body["batch_id"]
    assert batch_id is not None

    with db.open_db(web_ctx.db_path) as conn:
        jobs = db.list_jobs_by_batch(conn, batch_id)
        assert len(jobs) == 3
        assert {j["status"] for j in jobs} == {"CONFIRMED"}
        assert {j["target_device_id"] for j in jobs} == {"mock-switch-parent"}
        assert sorted(j["library_item_id"] for j in jobs) == sorted(selected)
        # one batch row, created by the shared service -- not a second one
        assert db.get_installation_batch(conn, batch_id)["target_device_id"] == "mock-switch-parent"
