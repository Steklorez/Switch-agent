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
