"""A device's name field saves itself -- there is no Save/Rename button,
on either the Devices list or the Device Details page.

The buttons were removed deliberately (user request, 2026-09-18): one text
field with its own button next to it is a button the user has to notice,
aim at and click for something the field could just do. Storage mappings
deliberately KEEP their explicit Save -- "the paths I set up inside the
device" are a decision; a name is just a label. static/self_saving_name.js
saves on a debounce while typing, immediately on blur, and on Enter.

What is worth pinning down in a test, because it is easy to undo by
accident:

  * the markup contract the shared JS depends on -- no submit button, and
    the status span that replaced it as the field's only feedback (without
    it a save would be completely invisible, which is worse than a button);
  * on Device Details, the two ids the field resyncs after each save --
    that page renders the name in more places than the field, and used to
    rely on the old button's post-save page reload to keep them honest;
  * the endpoints those fields post to still accept and persist a rename,
    including clearing the name back to NULL (an empty field is a real
    action here -- "use whatever the device reports" -- not a no-op).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders
from switchagent.mtp.windows import device_fingerprint
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

PARENT = "mock-switch-parent"
PARENT_FP = device_fingerprint(PARENT)


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


def _rename_form_html(html: str) -> str:
    start = html.index('class="device-rename-form"')
    return html[start:html.index("</form>", start)]


def test_the_rename_field_has_no_save_button(client):
    form = _rename_form_html(client.get("/devices").text)

    assert "<button" not in form
    assert "submit" not in form


def test_the_rename_field_carries_the_status_span_that_replaced_the_button(client):
    """Removing the button without this would make saving completely
    invisible -- devices.js writes "Saved"/"Not saved" here."""
    form = _rename_form_html(client.get("/devices").text)

    assert 'class="device-rename-status"' in form
    assert 'aria-live="polite"' in form


def test_the_endpoint_the_field_posts_to_persists_a_name(client, web_ctx):
    res = client.post(f"/api/devices/{PARENT}/rename", json={"friendly_name": "Гостиная"})

    assert res.status_code == 200
    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_device(conn, PARENT)["friendly_name"] == "Гостиная"


def test_clearing_the_field_clears_the_stored_name(client, web_ctx):
    """An emptied field is a real action -- "go back to whatever the device
    reports itself as" -- and must reach the database as NULL, not as an
    empty string that would then render as a blank name."""
    client.post(f"/api/devices/{PARENT}/rename", json={"friendly_name": "Гостиная"})

    res = client.post(f"/api/devices/{PARENT}/rename", json={"friendly_name": ""})

    assert res.status_code == 200
    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_device(conn, PARENT)["friendly_name"] is None


# ---------------------------------------------------------------------------
# the same field on the Device Details page
# ---------------------------------------------------------------------------

def test_the_detail_pages_name_field_has_no_rename_button_either(client):
    html = client.get(f"/devices/{PARENT_FP}").text
    start = html.index('id="device-rename-form"')
    form = html[start:html.index("</form>", start)]

    assert "<button" not in form
    assert 'class="device-rename-status"' in form


def test_the_detail_page_marks_the_places_the_field_has_to_resync(client):
    """This page renders the name in the heading and in the Device table as
    well as in the field. The old Rename button reloaded the page to keep
    all three in step; an autosaving field cannot reload (it would do so
    mid-typing), so it updates these two by id instead -- if the ids go,
    the page silently starts contradicting the field."""
    html = client.get(f"/devices/{PARENT_FP}").text

    assert 'id="device-display-name"' in html
    assert 'id="device-friendly-name"' in html


def test_both_pages_load_the_shared_self_saving_field_script(client):
    """Neither page's own script implements the behaviour any more -- if
    this tag is dropped, the field silently stops saving entirely."""
    assert "/static/self_saving_name.js" in client.get("/devices").text
    assert "/static/self_saving_name.js" in client.get(f"/devices/{PARENT_FP}").text
