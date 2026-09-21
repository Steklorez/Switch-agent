import time
from switchagent import db, queue_worker
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


def _sd_install_item_and_job(conn, device_id, base_title_id):
    item_id = db.upsert_library_item(
        conn, absolute_path=f"{base_title_id}.nsp", item_type="FILE", file_type="NSP", size=1000, mtime=0.0,
        content_hash=base_title_id, title_id=base_title_id, title_id_source="filename",
        status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        content_type="GAME_PACKAGE", package_format="NSP",
    )
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
        target_device_id=device_id, library_item_id=item_id,
    )
    return item_id, job_id


def test_a_freshly_completed_sd_install_job_counts_as_on_switch_immediately(web_ctx):
    """Field report: DBI does not rewrite InstalledApplications.csv
    mid-session -- a title this app itself just sent via SD_INSTALL never
    shows up in the CSV-derived cache until the NEXT MTP session, no matter
    how many more times that same stale file is re-read (see
    WebContext._locally_confirmed_installs's own comment). A job's own
    successful outcome, not a fresh CSV read, is what must make it count as
    "On Switch" before the device disconnects. No monkeypatching of
    list_installed_title_ids() here at all -- the mock backend reports no
    CSV data whatsoever (returns None, same as a real console with DBI's
    "Installed games" setting off), proving this signal is independent of
    that one."""
    ctx = web_ctx
    device_id = ctx.registry.known_device_ids()[0]
    base_title_id = "0100A6300150C000"
    with db.open_db(ctx.db_path) as conn:
        _item_id, job_id = _sd_install_item_and_job(conn, device_id, base_title_id)
        assert base_title_id not in ctx.get_known_installed_title_ids()

        outcome = queue_worker.JobRunOutcome(job_id=job_id, status="DONE_UNVERIFIED")
        ctx.note_install_job_outcome(conn, outcome)
        assert base_title_id in ctx.get_known_installed_title_ids()


def test_a_locally_confirmed_install_clears_on_disconnect_and_is_not_relit_without_a_fresh_csv_read(web_ctx, monkeypatch):
    """The other half of the story above: this is a SESSION-scoped stand-in
    for DBI's own CSV, not a permanent record -- it must go dark on
    disconnect exactly like the CSV-derived cache does (see
    refresh_devices()), and reconnecting alone must never relight it on its
    own. Only a genuinely fresh CSV read confirming the same title again
    may -- covered by test_connect_and_reconnect_refresh_titles_without_
    waiting_for_periodic_timer above; list_installed_title_ids() still
    reports nothing here, so this title staying dark after reconnect is the
    correct outcome, not a gap."""
    ctx = web_ctx
    device_id = ctx.registry.known_device_ids()[0]
    backend = ctx.registry.get(device_id)
    connect = backend.connect
    base_title_id = "0100A6300150C000"
    with db.open_db(ctx.db_path) as conn:
        _item_id, job_id = _sd_install_item_and_job(conn, device_id, base_title_id)
        ctx.note_install_job_outcome(conn, queue_worker.JobRunOutcome(job_id=job_id, status="DONE_UNVERIFIED"))
        assert base_title_id in ctx.get_known_installed_title_ids()

        def absent():
            raise DeviceNotFoundError("unplugged")
        monkeypatch.setattr(backend, "connect", absent)
        ctx.refresh_devices(conn)
        assert base_title_id not in ctx.get_known_installed_title_ids()

        monkeypatch.setattr(backend, "connect", connect)
        ctx.refresh_devices(conn)
        assert base_title_id not in ctx.get_known_installed_title_ids()
