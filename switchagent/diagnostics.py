"""Single, reusable diagnostics engine (UI-001 / UI-008).

Consumed by three call sites that must never diverge into separate
implementations:
  - the Settings page's Diagnostics section (web/services.py's
    get_diagnostics_report(), HTTP-safe: builds its inputs only from
    already-cached WebContext state, the DB, and the filesystem -- never
    a live COM call on the request thread);
  - `switch-agent doctor` / `switch-agent doctor --json` (cli.py's
    _cmd_doctor -- a standalone process with no shared WebContext, so it
    is allowed to do a one-off read-only device-discovery probe; see
    `probe_com` below);
  - UI-008's `GET /api/diagnostics/export`, which serializes exactly the
    same DiagnosticsReport this module produces for the other two --
    never a second diagnostics implementation.

Every string this module puts into a DiagnosticsReport is already safe to
paste into a public bug report: no raw device_id/USB serial (only
mtp.windows.device_fingerprint()'s sha256[:16] stand-ins, supplied by the
caller), and every filesystem path has the current user's home directory
prefix replaced with the literal `%USERPROFILE%` (see _sanitize_path).

This module never performs a Windows COM write or an MTP file transfer.
The only optional COM interaction is a read-only enumerate_devices() call,
gated behind `probe_com=True`, which only the standalone CLI path may set.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CheckStatus = str  # one of "OK" | "WARNING" | "ERROR" | "N/A"

_STATUS_RANK = {"OK": 0, "N/A": 0, "WARNING": 1, "ERROR": 2}

# Core tables the foundation migration (and everything before it) is
# expected to have created. Used only as a cheap, honest proxy for "this
# DB's schema is current" -- not a full schema diff.
_EXPECTED_TABLES = (
    "inbox_items", "library_items", "jobs", "job_log", "devices",
    "install_history", "installation_batches", "device_storage_mappings",
)


@dataclass(frozen=True)
class DiagnosticsCheck:
    name: str
    status: CheckStatus
    detail: str

    def line(self) -> str:
        if self.detail:
            return f"{self.name:<18} {self.status} · {self.detail}"
        return f"{self.name:<18} {self.status}"


@dataclass(frozen=True)
class DiagnosticsReport:
    generated_at: str
    version: str
    runtime_mode: str
    resource_root: str          # already sanitized, see _sanitize_path
    app_data_root: str
    config_path: str
    db_path: str
    library_dir: str
    work_dir: str
    checks: list[DiagnosticsCheck] = field(default_factory=list)
    known_device_count: int = 0
    connected_device_count: int = 0
    device_fingerprints: list[str] = field(default_factory=list)

    @property
    def overall_status(self) -> CheckStatus:
        if not self.checks:
            return "OK"
        return max((c.status for c in self.checks), key=lambda s: _STATUS_RANK.get(s, 0))


def _sanitize_path(path) -> str:
    """Replaces the current user's home-directory prefix with the literal
    `%USERPROFILE%` -- required so a Copy diagnostics/export payload never
    leaks a full user-specific home path (UI-001/UI-008 both require
    this, not just the export)."""
    if path is None:
        return "—"
    text = str(path)
    home = os.environ.get("USERPROFILE") or str(Path.home())
    if home and text.lower().startswith(home.lower()):
        text = "%USERPROFILE%" + text[len(home):]
    return text


def _format_size(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "unknown"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


# ---------------------------------------------------------------------------
# Individual checks -- each one is filesystem/DB-only (no COM), safe to run
# on any thread, including an HTTP request thread.
# ---------------------------------------------------------------------------

def _check_database(db_path: Path) -> DiagnosticsCheck:
    if not db_path.exists():
        return DiagnosticsCheck("Database", "WARNING", "not created yet")
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in _EXPECTED_TABLES if t not in tables]
            if missing:
                return DiagnosticsCheck(
                    "Database", "ERROR", f"schema out of date -- missing table(s): {', '.join(missing)}",
                )
            conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return DiagnosticsCheck("Database", "ERROR", f"cannot open: {exc}")
    return DiagnosticsCheck("Database", "OK", "schema up to date")


def _check_library(library_dir: Path, *, configured: bool) -> DiagnosticsCheck:
    if not library_dir.exists():
        detail = "configured path does not exist" if configured else "no folder configured yet"
        return DiagnosticsCheck("Library", "WARNING", detail)
    if not os.access(library_dir, os.R_OK):
        return DiagnosticsCheck("Library", "ERROR", "not readable")
    return DiagnosticsCheck("Library", "OK", "")


def _check_work_dir(work_dir: Path) -> DiagnosticsCheck:
    if not work_dir.exists():
        return DiagnosticsCheck("Work directory", "OK", "not created yet")
    if not os.access(work_dir, os.W_OK):
        return DiagnosticsCheck("Work directory", "ERROR", f"not writable · {_format_size(_dir_size_bytes(work_dir))} used")
    return DiagnosticsCheck("Work directory", "OK", f"{_format_size(_dir_size_bytes(work_dir))} used")


def _check_app_data_writable(app_data_root: Path) -> DiagnosticsCheck:
    marker = app_data_root / ".diagnostics_write_test"
    try:
        app_data_root.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
    except OSError as exc:
        return DiagnosticsCheck("App data", "ERROR", f"not writable: {exc}")
    return DiagnosticsCheck("App data", "OK", "")


def _check_free_disk(path: Path) -> DiagnosticsCheck:
    import shutil

    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return DiagnosticsCheck("Free disk", "WARNING", f"could not determine: {exc}")
    status = "WARNING" if usage.free < 2_000_000_000 else "OK"
    detail = f"{_format_size(usage.free)} free" if status == "OK" else f"only {_format_size(usage.free)} free"
    return DiagnosticsCheck("Free disk", status, detail)


def _check_rar(rar_available: bool) -> DiagnosticsCheck:
    if rar_available:
        return DiagnosticsCheck("RAR extraction", "OK", "")
    return DiagnosticsCheck("RAR extraction", "WARNING", "external unrar/7z/bsdtar backend not found on PATH")


def _check_com(*, probe: bool) -> DiagnosticsCheck:
    import importlib.util

    if importlib.util.find_spec("win32com") is None:
        return DiagnosticsCheck("MTP/COM", "ERROR", "pywin32 not installed")
    if not probe:
        return DiagnosticsCheck("MTP/COM", "OK", "pywin32 available")
    try:
        from .mtp.windows import enumerate_devices

        found = enumerate_devices()
    except Exception as exc:  # noqa: BLE001 -- a probe failure is a WARNING, not a crash
        return DiagnosticsCheck("MTP/COM", "WARNING", f"discovery probe failed: {exc}")
    return DiagnosticsCheck("MTP/COM", "OK", f"pywin32 available, {len(found)} device(s) discoverable")


def _tri_state_check(name: str, value: Optional[bool], *, ok_detail: str = "", off_detail: str = "") -> DiagnosticsCheck:
    if value is None:
        return DiagnosticsCheck(name, "N/A", "not running under `switch-agent web`")
    if value:
        return DiagnosticsCheck(name, "OK", ok_detail)
    return DiagnosticsCheck(name, "WARNING", off_detail or "not running")


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(
    *,
    version: str,
    runtime_mode: str,
    resource_root: Path,
    app_data_root: Path,
    config_path: Path,
    db_path: Path,
    library_dir: Path,
    library_dir_configured: bool,
    work_dir: Path,
    rar_backend_available: bool,
    watcher_running: Optional[bool] = None,
    worker_running: Optional[bool] = None,
    worker_paused: Optional[bool] = None,
    worker_last_heartbeat_at: Optional[str] = None,
    probe_com: bool = False,
    known_device_count: int = 0,
    connected_device_count: int = 0,
    device_fingerprints: Optional[list[str]] = None,
) -> DiagnosticsReport:
    from datetime import datetime, timezone

    checks: list[DiagnosticsCheck] = [
        _check_database(db_path),
        _check_app_data_writable(app_data_root),
        _check_library(library_dir, configured=library_dir_configured),
        _check_work_dir(work_dir),
        _check_free_disk(app_data_root),
        _tri_state_check("Watcher", watcher_running, ok_detail="watching library folder"),
        _tri_state_check(
            "Worker",
            None if worker_running is None else (worker_running and not bool(worker_paused)),
            ok_detail=f"last heartbeat {worker_last_heartbeat_at}" if worker_last_heartbeat_at else "",
            off_detail="paused" if worker_paused else "not running",
        ),
        _check_rar(rar_backend_available),
        _check_com(probe=probe_com),
        DiagnosticsCheck(
            "Devices", "OK",
            f"{connected_device_count} connected, {known_device_count} known" if known_device_count or connected_device_count
            else "none known yet",
        ),
    ]

    return DiagnosticsReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        version=version,
        runtime_mode=runtime_mode,
        resource_root=_sanitize_path(resource_root),
        app_data_root=_sanitize_path(app_data_root),
        config_path=_sanitize_path(config_path),
        db_path=_sanitize_path(db_path),
        library_dir=_sanitize_path(library_dir),
        work_dir=_sanitize_path(work_dir),
        checks=checks,
        known_device_count=known_device_count,
        connected_device_count=connected_device_count,
        device_fingerprints=list(device_fingerprints or []),
    )


def _render_core_text_lines(payload: dict) -> list[str]:
    """Shared between format_report_text() and export_dict_to_text() (UI-008)
    so the two never drift into two different renderings of the same data."""
    lines = [
        f"Version {payload['version']} ({payload['runtime_mode']})",
        "",
        f"Resource root      {payload['resource_root']}",
        f"App data           {payload['app_data_root']}",
        f"Config             {payload['config_path']}",
        f"Database           {payload['db_path']}",
        f"Library            {payload['library_dir']}",
        f"Work directory     {payload['work_dir']}",
        "",
    ]
    for c in payload["checks"]:
        line = f"{c['name']:<18} {c['status']}"
        if c["detail"]:
            line += f" · {c['detail']}"
        lines.append(line)
    if payload.get("device_fingerprints"):
        lines.append("")
        lines.append("Known device fingerprints: " + ", ".join(payload["device_fingerprints"]))
    return lines


def format_report_text(report: DiagnosticsReport) -> str:
    payload = report_to_dict(report)
    lines = [f"SwitchAgent diagnostics · {payload['generated_at']}", *_render_core_text_lines(payload)]
    return "\n".join(lines)


def report_to_dict(report: DiagnosticsReport) -> dict:
    return {
        "generated_at": report.generated_at,
        "version": report.version,
        "runtime_mode": report.runtime_mode,
        "resource_root": report.resource_root,
        "app_data_root": report.app_data_root,
        "config_path": report.config_path,
        "db_path": report.db_path,
        "library_dir": report.library_dir,
        "work_dir": report.work_dir,
        "overall_status": report.overall_status,
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in report.checks],
        "known_device_count": report.known_device_count,
        "connected_device_count": report.connected_device_count,
        "device_fingerprints": report.device_fingerprints,
    }


# ---------------------------------------------------------------------------
# UI-008: export -- strictly additive on top of the same DiagnosticsReport
# (never a second engine). Adds recent job errors (safe: job ids/display
# names/status/error text -- none of that is device-identifying) and an
# optional sanitized application-log tail.
# ---------------------------------------------------------------------------

def build_export_dict(
    report: DiagnosticsReport, *, recent_job_errors: Optional[list[dict]] = None, log_tail: Optional[str] = None,
) -> dict:
    """HW-005 finding (real-hardware diagnostics/privacy validation session):
    a job's `error` text is free text written by whatever code path failed
    it (e.g. manifest.ManifestError's own message, which can legitimately
    embed a full local filesystem path -- confirmed live: a manifest-build
    failure's error included `C:\\Users\\<real name>\\...`). Every OTHER
    field this module emits is already sanitized (_sanitize_path for the
    known root/db/library paths, sanitize_free_text for the log tail) --
    `recent_job_errors` was the one field copied through verbatim. Now runs
    the same sanitize_free_text() (home-directory + raw-device_id redaction)
    over each entry's `error` and `display_name` (display_name is normally
    just a filename, but is still free text ultimately derived from a
    filesystem path -- sanitizing it too costs nothing and closes the same
    class of gap defensively)."""
    payload = report_to_dict(report)
    payload["recent_job_errors"] = [
        {
            **e,
            "error": sanitize_free_text(e["error"]) if e.get("error") else e.get("error"),
            "display_name": sanitize_free_text(e["display_name"]) if e.get("display_name") else e.get("display_name"),
        }
        for e in (recent_job_errors or [])
    ]
    payload["log_tail"] = log_tail
    return payload


def export_dict_to_text(payload: dict) -> str:
    lines = [f"SwitchAgent diagnostics export · {payload['generated_at']}", *_render_core_text_lines(payload)]
    if payload.get("recent_job_errors"):
        lines.append("")
        lines.append("Recent job errors:")
        for e in payload["recent_job_errors"]:
            lines.append(f"  #{e['id']} {e['display_name']} [{e['status']}]: {e['error']}")
    if payload.get("log_tail"):
        lines.append("")
        lines.append("Log tail (sanitized):")
        lines.append(payload["log_tail"])
    return "\n".join(lines)


def sanitize_free_text(text: str) -> str:
    """Redacts any embedded raw device_id (USB-serial-shaped segment) and
    the current user's home directory from arbitrary free text -- used for
    the application log tail, which (unlike every other field in this
    module) is not a single known-safe value but real log lines that could
    in principle contain anything a future log call writes -- and, since
    HW-005, for each recent job error's own `error`/`display_name` text
    (see build_export_dict's own docstring for the real-hardware finding
    that motivated this).

    Two separate passes, deliberately in this order: first the full home
    directory PATH prefix (`C:\\Users\\<name>\\...` -> `%USERPROFILE%\\...`,
    the common case, keeps the rest of the path readable); then, as a
    second, broader safety net, the bare username by itself anywhere else
    it appears -- confirmed necessary live: a real error message embedded
    the username a SECOND time inside an unrelated hyphenated directory
    name (a leftover scratchpad path from an unrelated tool, not a
    `C:\\Users\\...`-shaped prefix at all), which the first pass alone does
    not and cannot catch."""
    import re as _re

    from .mtp.windows import mask_device_id

    text = mask_device_id(text)
    home = os.environ.get("USERPROFILE") or str(Path.home())
    if home:
        text = _re.sub(_re.escape(home), "%USERPROFILE%", text, flags=_re.IGNORECASE)
    username = os.environ.get("USERNAME")
    if username and len(username) >= 3:  # avoid mangling short, common substrings
        text = _re.sub(_re.escape(username), "%USERNAME%", text, flags=_re.IGNORECASE)
    return text


def read_sanitized_log_tail(log_path: Path, *, max_bytes: int = 8_000) -> Optional[str]:
    """Best-effort: returns None (never raises) if the log file doesn't
    exist or can't be read -- this is optional when it's safe to include.
    Only switchagent/desktop.py (the packaged app) currently
    configures file logging (see logging_setup.py); a plain `switch-agent
    web` dev/CLI run has no log file at all, so this routinely returns
    None there -- that is expected, not an error."""
    if not log_path.is_file():
        return None
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]  # drop a line likely truncated mid-way by the seek above
    return sanitize_free_text(text)
