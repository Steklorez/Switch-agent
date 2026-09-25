"""The Add-ons tab (web/addons_views.py, web/addons/catalog.yaml).

The catalog is edited by hand; these tests are what stops an entry that
could not be shown truthfully -- a `requires` naming nothing, a check or an
installer SwitchAgent does not have, a preview that is not there -- from
reaching the page.
"""

from __future__ import annotations

import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders, scanner
from switchagent.web import addons_views
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context


def _doc():
    return yaml.safe_load(addons_views.CATALOG_PATH.read_text(encoding="utf-8"))


def test_the_shipped_catalog_keeps_every_rule():
    addons = addons_views.load_catalog()
    ids = [a.id for a in addons]
    assert ids[:3] == ["emuiibo", "tesla-menu", "nx-ovlloader"]
    emuiibo = addons[0]
    assert emuiibo.install == "emuiibo" and emuiibo.status == "emuiibo"
    assert emuiibo.requires == ("nx-ovlloader", "tesla-menu")
    assert emuiibo.usage and emuiibo.controls and emuiibo.tips
    assert (addons_views.STATIC_DIR / emuiibo.preview).is_file()


@pytest.mark.parametrize("breakage,message", [
    (lambda d: d["addons"][0].update(requires=["nope"]), "does not have"),
    (lambda d: d["addons"][1].update(id="emuiibo"), "used twice"),
    (lambda d: d["addons"][0].update(status="dbi"), "unknown `status`"),
    (lambda d: d["addons"][1].update(install="tesla"), "unknown `install`"),
    (lambda d: d["addons"][0].update(preview="missing.png"), "not a file"),
    (lambda d: d["addons"][0].update(source="http://example.com"), "https"),
    (lambda d: d["addons"][0].pop("usage"), "`usage`"),
    (lambda d: d["addons"][0].update(related={"label": "x", "href": "https://elsewhere"}), "in-app"),
    (lambda d: d["addons"][0].update(id="Emu iibo"), "lowercase"),
])
def test_an_entry_the_tab_could_not_show_truthfully_is_refused(breakage, message):
    doc = copy.deepcopy(_doc())
    breakage(doc)
    with pytest.raises(addons_views.CatalogError, match=message):
        addons_views.parse_catalog(doc)


def test_catalog_text_is_escaped_and_backticks_become_code():
    assert str(addons_views.rich("copy `SdOut` to <root> & restart")) == \
        "copy <code>SdOut</code> to &lt;root&gt; &amp; restart"


def test_status_says_what_the_console_last_showed():
    emuiibo, tesla, _loader = addons_views.load_catalog()[:3]
    assert addons_views.addon_status(emuiibo, None)["state"] == "unknown"
    full = {"components": {"sysmodule": True, "overlay": True, "tesla_menu": False}, "overlay_version": "1.1.3"}
    assert addons_views.addon_status(emuiibo, full) == {"state": "installed", "label": "installed — 1.1.3",
                                                         "version": "1.1.3"}
    old = {**full, "overlay_version": "0.6.3"}
    assert addons_views.addon_status(emuiibo, old)["state"] == "outdated"
    half = {"components": {"sysmodule": True, "overlay": False}}
    assert addons_views.addon_status(emuiibo, half)["label"] == "incomplete — the overlay is missing"
    assert addons_views.addon_status(tesla, full)["state"] == "missing"


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "library").mkdir()
    (tmp_path / "inbox").mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    db_path = tmp_path / "test.db"
    ctx = build_mock_context(db_path, seed_emuiibo=True)
    with db.open_db(db_path) as conn:
        scanner.scan_library_once(conn)
        ctx.refresh_devices(conn)
        while ctx.emuiibo.step(conn, ctx.registry):
            pass
    return TestClient(create_app(ctx))


def _fingerprint(client, name):
    return next(d["device_fingerprint"] for d in client.get("/api/devices").json() if name in d["display_name"])


def test_the_tab_shows_every_entry_with_its_instructions(client):
    html = client.get("/addons").text
    assert 'href="/addons" class="active"' in html
    for anchor in ("addon-emuiibo", "addon-tesla-menu", "addon-nx-ovlloader"):
        assert f'id="{anchor}"' in html
    assert "Using it" in html and "When something is wrong" in html
    assert "<kbd>R3</kbd>" in html
    assert "<code>atmosphere/contents/0100000000000352</code>" in html
    assert "addon-emuiibo-overlay.svg" in html


def test_a_switch_with_old_emuiibo_is_offered_the_update(client):
    parent = _fingerprint(client, "Parent")
    html = client.get(f"/addons?device={parent}").text
    assert "installed — 0.6.3 (outdated, 1.1.3 is current)" in html
    assert f'data-device="{parent}"' in html and ">Update<" in html


def test_a_bare_switch_says_what_is_missing_and_offers_the_install(client):
    child = _fingerprint(client, "Child")
    html = client.get(f"/addons?device={child}").text
    assert html.count("addon-status-missing") == 3
    assert ">Install<" in html
