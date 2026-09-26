"""Command-line entry point.

    switch-agent scan              one-off scan of inbox/, prints a report
    switch-agent scan --watch      keep watching inbox/ and rescan on change
    switch-agent preview <path>    read-only analysis of any path (needs not
                                    be inside inbox/); add --extract to
                                    actually unpack an archive into
                                    work/<job-id>/ under safe_extract's guards

(`python scan_cli.py [...]` at the project root is a thin, backwards
compatible shim over this module -- see that file.)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

from . import config, db, extractor, firstrun, preview, scanner, transfer
from .mtp import DeviceNotFoundError, MockMtpBackend, MtpError, RealMtpBackend
from .mtp.windows import device_fingerprint, mask_device_id


def _print_scan_report(conn) -> None:
    rows = db.list_inbox_items(conn)
    if not rows:
        print(f"(inbox/ is empty: {config.INBOX_DIR})")
        return
    print(f"{'STATUS':<15} {'TYPE':<9} {'ACTION':<16} {'TITLE_ID':<17} {'SIZE':>10}  PATH")
    for r in rows:
        size_mb = r["size"] / (1024 * 1024)
        print(
            f"{r['status']:<15} {r['file_type']:<9} {(r['suggested_action'] or '-'):<16} "
            f"{(r['title_id'] or '-'):<17} {size_mb:>8.1f}MB  {r['relative_path']}"
        )
        if r["note"]:
            print(f"    note:  {r['note']}")
        if r["error"]:
            print(f"    error: {r['error']}")


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.watch:
        return _run_watch()
    with db.open_db() as conn:
        summary = scanner.scan_once(conn)
        print(f"Scan complete: {summary}\n")
        _print_scan_report(conn)
    return 0


def _run_watch() -> int:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    debounce_seconds = 5.0
    state = {"last_event": 0.0, "pending": False}

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):  # noqa: ANN001 -- watchdog's own signature
            state["last_event"] = time.time()
            state["pending"] = True

    observer = Observer()
    observer.schedule(Handler(), str(config.INBOX_DIR), recursive=True)
    observer.start()
    print(f"Watching {config.INBOX_DIR} (Ctrl+C to stop)...")

    try:
        with db.open_db() as conn:
            summary = scanner.scan_once(conn)
            print(f"[{time.strftime('%H:%M:%S')}] initial scan: {summary}")
            while True:
                time.sleep(1)
                if state["pending"] and (time.time() - state["last_event"]) >= debounce_seconds:
                    state["pending"] = False
                    summary = scanner.scan_once(conn)
                    print(f"[{time.strftime('%H:%M:%S')}] rescanned: {summary}")
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        report = preview.preview_path(path, extract=args.extract)
    except FileNotFoundError:
        print(f"Path not found: {path}", file=sys.stderr)
        return 1
    except extractor.ArchiveError as exc:
        print(f"Extraction error: {exc}", file=sys.stderr)
        return 1
    print(preview.format_report(report))
    return 0


def _cmd_mtp_mock(args: argparse.Namespace) -> int:
    """Manual, no-real-Switch-needed run of the full pipeline through
    MOCK TRANSFER: preview(extract=True) -> transfer_report() against a
    fresh in-memory MockMtpBackend, printing everything a human would want
    to sanity-check -- the preview, the outcome, what ended up where, and
    the operation log. Nothing here touches a real device; see
    switchagent/mtp/mock.py."""
    path = Path(args.path)
    backend = MockMtpBackend(device_present=not args.no_device)
    backend.add_storage("SD_CARD")
    backend.add_storage("SD_INSTALL")

    try:
        backend.connect()
    except DeviceNotFoundError as exc:
        print(f"CONNECT failed: {exc}", file=sys.stderr)
        return 1

    try:
        report = preview.preview_path(path, extract=True)
    except FileNotFoundError:
        print(f"Path not found: {path}", file=sys.stderr)
        return 1
    except extractor.ArchiveError as exc:
        print(f"Extraction error: {exc}", file=sys.stderr)
        return 1

    print(preview.format_report(report))
    print()

    if args.fail:
        backend.arm_failure(args.fail)
        print(f"(armed fault: next transfer will simulate '{args.fail}')\n")

    outcome = transfer.transfer_report(backend, report, overwrite=args.overwrite)
    print(f"TRANSFER: {'OK' if outcome.ok else 'FAILED'}")
    if outcome.storage:
        print(f"STORAGE: {outcome.storage}")
    print(f"FILES: {outcome.files_sent}/{outcome.files_total}")
    print(f"BYTES: {outcome.bytes_sent}")
    if outcome.error:
        print(f"ERROR: {outcome.error}")

    print("\nDEVICE CONTENTS:")
    for storage_name in ("SD_INSTALL", "SD_CARD"):
        files = backend.storage_tree(storage_name).list_files()
        print(f"  {storage_name}:" + ("" if files else " (empty)"))
        for f in files:
            print(f"    {f}")

    print("\nOPERATION LOG:")
    for entry in backend.operation_log:
        print(f"  {entry.seq:>2} {entry.operation:<22} {entry.details}")

    return 0 if outcome.ok else 1


def _select_real_device_id(device_id_arg: Optional[str]) -> Optional[str]:
    from .mtp.windows import enumerate_devices

    devices = enumerate_devices()
    if not devices:
        print("No MTP devices found under This PC.", file=sys.stderr)
        return None

    if device_id_arg is None:
        if len(devices) == 1:
            chosen = devices[0]
            print(f"Auto-selected the only connected device: fingerprint={device_fingerprint(chosen.device_id)}")
            return chosen.device_id
        print(
            f"{len(devices)} MTP devices connected -- refusing to guess. "
            "Pass --device-id (substring of the device path, or its fingerprint) to pick one:",
            file=sys.stderr,
        )
        for d in devices:
            print(f"  name={d.name!r} fingerprint={device_fingerprint(d.device_id)} path={mask_device_id(d.device_id)}",
                  file=sys.stderr)
        return None

    matches = [d for d in devices if device_id_arg in d.device_id or device_id_arg == device_fingerprint(d.device_id)]
    if len(matches) != 1:
        print(f"--device-id {device_id_arg!r} matched {len(matches)} device(s), expected exactly 1.", file=sys.stderr)
        return None
    return matches[0].device_id


def _cmd_mtp_test(args: argparse.Namespace) -> int:
    """Minimal, deliberately narrow smoke test for RealMtpBackend against
    real hardware. Hardcoded to SD_CARD ONLY -- this command has no way to
    target SD Card install at all, so it cannot accidentally trigger a game
    install (see docs/STAGE5B-REAL-MTP.md, "Why SD_CARD only")."""
    import os
    import time as time_module
    import uuid

    device_id = _select_real_device_id(args.device_id)
    if device_id is None:
        return 1

    backend = RealMtpBackend(device_id=device_id)
    try:
        info = backend.connect()
    except DeviceNotFoundError as exc:
        print(f"CONNECT failed: {exc}", file=sys.stderr)
        return 1
    print(f"Connected: name={info.name!r} fingerprint={device_fingerprint(info.device_id)}")

    storage = "SD_CARD"
    try:
        storage_info = backend.get_storage(storage)
    except MtpError as exc:
        print(f"get_storage({storage!r}) failed: {exc}", file=sys.stderr)
        return 1
    print(f"Storage: {storage} free={storage_info.free_bytes} total={storage_info.total_bytes}")

    test_dir = args.test_dir or f"switchagent_test_{uuid.uuid4().hex[:8]}"
    try:
        backend.ensure_directory(storage, test_dir)
    except MtpError as exc:
        print(f"ensure_directory({test_dir!r}) failed: {exc}", file=sys.stderr)
        return 1
    print(f"Test directory confirmed: {test_dir}")

    # Anchored under WORK_DIR (writable app data), not PROJECT_ROOT/tools --
    # `tools/` is a dev-only diagnostics directory, not part of the
    # installed/portable package, and PROJECT_ROOT may not even be
    # writable in a packaged build (see switchagent/paths.py).
    scratch_dir = config.WORK_DIR / "_mtp_test_artifacts"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    if args.file:
        local_file = Path(args.file)
        if not local_file.is_file():
            print(f"--file {local_file} does not exist", file=sys.stderr)
            return 1
    else:
        local_file = scratch_dir / f"switchagent-mtp-test-{uuid.uuid4().hex[:8]}.bin"
        local_file.write_bytes(os.urandom(4096))
        print(f"Generated fresh local test file: {local_file} (4096 bytes)")

    dest_path = f"{test_dir}/{local_file.name}"
    t0 = time_module.monotonic()
    try:
        result = backend.send_file(storage, dest_path, local_file, overwrite=args.overwrite)
    except MtpError as exc:
        print(f"send_file FAILED (raised): {exc}", file=sys.stderr)
        return 1
    elapsed = time_module.monotonic() - t0

    print(f"\nRESULT: {result.status.value}")
    print(f"BYTES: {result.bytes_sent}/{result.bytes_total}")
    print(f"ELAPSED: {elapsed:.2f}s")
    if result.error:
        print(f"ERROR: {result.error}")
    print(f"Destination left in place at: {storage}/{dest_path} -- not deleted.")

    return 0 if result.status.value == "COMPLETED" else 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    """UI-001: `switch-agent doctor` / `switch-agent doctor --json`. Builds
    the same DiagnosticsReport the Settings page and the export endpoint
    use (see switchagent/diagnostics.py's module docstring) -- never a
    second implementation. Standalone process, no shared WebContext, so
    unlike the Web UI path it may do a one-off read-only device-discovery
    probe (never in --mock mode, since MockMtpBackend has nothing for
    enumerate_devices() to find on a real system anyway). Never writes to
    a Switch."""
    import json as json_mod

    from . import diagnostics

    firstrun.ensure_app_config(config.CONFIG_YAML_PATH)
    config.LIBRARY_DIR = config.load_library_dir()
    library_info = config.library_dir_info(config.CONFIG_YAML_PATH)

    known_device_count = 0
    fingerprints: list[str] = []
    if config.DB_PATH.exists():
        with db.open_db(config.DB_PATH) as conn:
            rows = db.list_devices(conn)
        known_device_count = len(rows)
        fingerprints = [device_fingerprint(row["device_id"]) for row in rows]

    report = diagnostics.build_report(
        version=_cli_version(),
        runtime_mode=config.RUNTIME_MODE,
        resource_root=config.PROJECT_ROOT,
        app_data_root=config.APP_DATA_ROOT,
        config_path=config.CONFIG_YAML_PATH,
        db_path=config.DB_PATH,
        library_dir=config.LIBRARY_DIR,
        library_dir_configured=library_info.configured,
        work_dir=config.WORK_DIR,
        rar_backend_available=extractor.rar_backend_available(),
        watcher_running=None,  # standalone CLI process runs no watcher/worker
        worker_running=None,
        worker_paused=None,
        worker_last_heartbeat_at=None,
        probe_com=not args.mock,
        known_device_count=known_device_count,
        connected_device_count=0,
        device_fingerprints=fingerprints,
    )

    if args.json:
        print(json_mod.dumps(diagnostics.report_to_dict(report), indent=2))
    else:
        print(diagnostics.format_report_text(report))
    return 0 if report.overall_status != "ERROR" else 1


def _cli_version() -> str:
    from . import __version__

    return __version__


def _cmd_web(args: argparse.Namespace) -> int:
    """Starts the local Web UI (see docs/WEB-UI.md). Binds to 127.0.0.1 by
    default -- pass --host 0.0.0.0 to make it reachable from other devices
    on the LAN (phone/tablet), never exposed to the internet automatically.
    --mock (or MOCK_MTP=true, matching the project-wide mock-mode
    convention) runs against pre-registered MockMtpBackend instances
    instead of real hardware -- useful for trying the UI without a Switch
    plugged in."""
    import logging
    import os

    import uvicorn

    from .web.app import create_app
    from .web.context import build_mock_context, build_real_context

    use_mock = args.mock or os.environ.get("MOCK_MTP", "").lower() in ("1", "true", "yes")
    if use_mock:
        # Before anything opens a connection -- see config.use_mock_database().
        config.use_mock_database()
    firstrun.ensure_app_config(config.CONFIG_YAML_PATH)
    config.LIBRARY_DIR = config.load_library_dir()

    # Real-hardware finding (2026-09-12): the background worker's own
    # connect()/send_file()/etc. logging (mtp/windows.py, queue_worker.py,
    # web/context.py -- all logging.getLogger(__name__).info(...), never
    # print()) had nowhere to go here -- only the packaged app
    # (desktop.py/logging_setup.py) ever configured the root logger; this
    # dev/CLI entrypoint left it at Python's default (WARNING, no handler),
    # so every one of those INFO-level lines was silently dropped. Found
    # while trying to inspect the exact timing of a real SD_INSTALL
    # transfer and finding nothing but uvicorn's own separate HTTP access
    # log. A plain console handler here is enough to see them without
    # taking on logging_setup.py's rotating-file-handler machinery (which
    # exists specifically for a packaged, console-less EXE -- not needed
    # here, where a console already exists).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    ctx = build_mock_context(config.DB_PATH, seed_emuiibo=True) if use_mock else build_real_context(config.DB_PATH)
    ctx.start_worker()
    ctx.start_library_watcher()
    ctx.addon_releases.start()
    app = create_app(ctx)
    print(
        f"SwitchAgent Web UI: http://{args.host}:{args.port}  "
        f"(mode: {'mock' if use_mock else 'real'}, db: {config.DB_PATH.name})"
    )
    try:
        from .web.network import home_network_only
        app.middleware("http")(home_network_only)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info", proxy_headers=False)
    finally:
        ctx.stop_library_watcher()
        ctx.stop_worker()
        ctx.addon_releases.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(prog="switch-agent")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="scan inbox/ once (or --watch continuously)")
    p_scan.add_argument("--watch", action="store_true")
    p_scan.set_defaults(func=_cmd_scan)

    p_preview = sub.add_parser("preview", help="read-only analysis of any file/folder")
    p_preview.add_argument("path")
    p_preview.add_argument(
        "--extract", action="store_true",
        help="actually extract the archive into work/<job-id>/ (safe_extract guards apply)",
    )
    p_preview.set_defaults(func=_cmd_preview)

    p_mtp_mock = sub.add_parser(
        "mtp-mock", help="run preview+transfer for a path against an in-memory mock Switch (no real device)",
    )
    p_mtp_mock.add_argument("path")
    p_mtp_mock.add_argument("--overwrite", action="store_true")
    p_mtp_mock.add_argument(
        "--fail", choices=["error", "disconnect", "corrupt", "partial"], default=None,
        help="simulate this failure on the first file transfer, to manually check error handling",
    )
    p_mtp_mock.add_argument(
        "--no-device", action="store_true", help="simulate no Switch being connected at all",
    )
    p_mtp_mock.set_defaults(func=_cmd_mtp_mock)

    p_mtp_test = sub.add_parser(
        "mtp-test",
        help="real-hardware smoke test for RealMtpBackend -- SD_CARD only, never SD Card install",
    )
    p_mtp_test.add_argument(
        "--device-id", default=None,
        help="substring of the target device's path (or its fingerprint) -- required if more than one "
             "MTP device is connected; auto-selected if exactly one is found",
    )
    p_mtp_test.add_argument(
        "--test-dir", default=None,
        help="name of an existing test directory under SD_CARD to reuse (e.g. to test DESTINATION_CONFLICT); "
             "default: create a fresh uniquely-named one",
    )
    p_mtp_test.add_argument(
        "--file", default=None,
        help="local file to send (default: generate a fresh random ~4KB file each run)",
    )
    p_mtp_test.add_argument("--overwrite", action="store_true")
    p_mtp_test.set_defaults(func=_cmd_mtp_test)

    p_doctor = sub.add_parser("doctor", help="run diagnostics: DB, library, work dir, disk space, RAR/COM availability")
    p_doctor.add_argument("--json", action="store_true", help="machine-readable output")
    p_doctor.add_argument(
        "--mock", action="store_true",
        help="skip the read-only device-discovery probe (matches `web --mock`'s no-real-hardware mode)",
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_web = sub.add_parser("web", help="start the local Web UI (see docs/WEB-UI.md)")
    p_web.add_argument("--host", default="0.0.0.0", help="bind address -- default allows LAN access; use 127.0.0.1 for local only")
    p_web.add_argument("--port", type=int, default=8765)
    p_web.add_argument("--mock", action="store_true", help="use MockMtpBackend instead of real hardware")
    p_web.set_defaults(func=_cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Backwards compatibility with the stage-2 CLI shape: `python scan_cli.py`
    # and `python scan_cli.py --watch` with no explicit subcommand both mean
    # "scan". `--help`/`-h`/`--version` are deliberately excluded from this
    # rewrite so they show the real top-level help/version (all subcommands,
    # including newer ones like `preview`/`mtp-mock`) instead of being
    # silently narrowed to just `scan`'s (which has neither).
    if argv and argv[0] in ("-h", "--help", "--version"):
        pass
    elif not argv or argv[0].startswith("-"):
        argv = ["scan", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
