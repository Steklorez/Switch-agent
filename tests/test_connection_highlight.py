import time
from switchagent import db
from switchagent.mtp.errors import DeviceNotFoundError
from .test_web_api import web_ctx


def test_connect_and_reconnect_refresh_titles_without_waiting_for_periodic_timer(web_ctx, monkeypatch):
    ctx = web_ctx
    device_id = ctx.registry.known_device_ids()[0]
    backend = ctx.registry.get(device_id)
    connect = backend.connect
    title = "0100A6300150C000"
    monkeypatch.setattr(backend, "list_installed_title_ids", lambda: {title})
    ctx._device_cache = []
    ctx._last_storage_refresh_monotonic = time.monotonic()
    with db.open_db(ctx.db_path) as conn:
        ctx.refresh_devices(conn)
        assert title in ctx.get_known_installed_title_ids()
        def absent():
            raise DeviceNotFoundError("unplugged")
        monkeypatch.setattr(backend, "connect", absent)
        ctx.refresh_devices(conn)
        assert title not in ctx.get_known_installed_title_ids()
        monkeypatch.setattr(backend, "connect", connect)
        ctx.refresh_devices(conn)
        assert title in ctx.get_known_installed_title_ids()


def test_confirmed_titles_survive_disconnect_unlike_the_in_memory_cache(web_ctx, monkeypatch):
    """The real bug report this persisted table exists to fix: a title
    DBI already confirmed installed must not silently become "unknown"
    the moment its Switch disconnects (or SwitchAgent restarts) -- only a
    genuinely fresh, successful rescan of that SAME device may ever change
    what it claims. ctx.get_known_installed_title_ids() (in-memory, used
    only for the "enable DBI's Installed games setting" hint) is EXPECTED
    to drop it on disconnect, per the test above -- but the Library page
    itself reads db.get_all_confirmed_installed_base_title_ids(), which
    must not."""
    ctx = web_ctx
    device_id = ctx.registry.known_device_ids()[0]
    backend = ctx.registry.get(device_id)
    connect = backend.connect
    title = "0100A6300150C000"
    monkeypatch.setattr(backend, "list_installed_title_ids", lambda: {title})
    ctx._device_cache = []
    ctx._last_storage_refresh_monotonic = time.monotonic()
    with db.open_db(ctx.db_path) as conn:
        ctx.refresh_devices(conn)
        assert title in db.get_all_confirmed_installed_base_title_ids(conn)

        def absent():
            raise DeviceNotFoundError("unplugged")
        monkeypatch.setattr(backend, "connect", absent)
        ctx.refresh_devices(conn)
        # The in-memory, connected-only view has indeed lost it (matches
        # the test above) -- the persisted table must not.
        assert title not in ctx.get_known_installed_title_ids()
        assert title in db.get_all_confirmed_installed_base_title_ids(conn)

        # A device restarting SwitchAgent would rebuild ctx from scratch,
        # with an empty in-memory cache -- but the DB survives the process,
        # so the persisted confirmation is still there regardless.
        assert db.get_all_confirmed_installed_base_title_ids(conn) == {title}
