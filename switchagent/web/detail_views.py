"""View assembly for the two Wave-3 detail pages -- W3-001 Game Details
(`GET /games/{base_title_id}`) and W3-004 Device Details
(`GET /devices/{fingerprint}`).

Deliberately its own module rather than more weight in the already-large
services.py, but it is a LAYER ON TOP of services.py, never a parallel
implementation of anything:

  - family grouping (base / update / DLC / mod / duplicates) comes from
    services.list_library_view(kind="games") verbatim, which in turn uses
    title_id.classify_title_variant() -- there is no second piece of
    TITLE_ID arithmetic anywhere in this file;
  - display names come from the same queue_worker.find_family_base_name_source()
    lookup the Library/Queue/History already share (with the PERF-001
    `library_items` parameter threaded through, never a per-row refetch);
  - every human-facing device label comes from services.device_label(), and
    every device URL from mtp.windows.device_fingerprint() -- the raw,
    serial-bearing device_id never reaches a template;
  - activity claims come from services.classify_activity(), the single
    place that decides what SwitchAgent is entitled to say about a
    DONE / DONE_UNVERIFIED / user-confirmed row.

Installing from the Game Details page is likewise not a second job
pipeline: the page renders the same selection checkboxes the Library page
does, loads the same static/library.js, and posts to the same
POST /api/jobs -> services.create_and_confirm_jobs().
"""

from __future__ import annotations

from typing import Optional

from .. import db, queue_worker
from .. import title_id as title_id_mod
from ..mtp.windows import device_fingerprint
from . import services
from .context import WebContext

# ---------------------------------------------------------------------------
# W3-001: Game Details
# ---------------------------------------------------------------------------


def normalize_base_title_id(raw: str) -> Optional[str]:
    """The URL segment of `GET /games/{base_title_id}` -> the canonical
    uppercase family id, or None if it isn't a 16-hex-char TITLE_ID at all.
    Shape validation is title_id.is_valid_title_id()'s job (the single
    place that owns that regex, see its module docstring) -- never a second
    regex here."""
    normalized = title_id_mod.normalize_title_id((raw or "").strip())
    if normalized is None:
        return None
    # A family id is by definition a BASE application id; anything else
    # (an update's ...800, a DLC's ...1001) would silently address the same
    # family under a second URL. Canonicalize instead of 404-ing, so a
    # hand-typed update id still lands on the right page.
    return title_id_mod.classify_title_variant(normalized).base_title_id


def _family_history_rows(conn, base_title_id: str) -> list:
    """Every install_history row whose own title_id resolves into this
    family -- base, update, DLC and mod alike (a mod's title_id IS the
    family id, see services.family_base_title_id). Read in one pass, never
    a query per row."""
    return [
        row for row in db.list_all_install_history(conn)
        if services.family_base_title_id(row["title_id"]) == base_title_id
    ]


def _activity_entry(row) -> dict:
    activity = services.classify_activity(row)
    return {
        "history_id": row["id"],
        "job_id": row["job_id"],
        "display_name": row["display_name"],
        "title_id": row["title_id"],
        "target_storage": row["target_storage"],
        "created_at": row["created_at"],
        "error": row["error"],
        "bytes_total": row["bytes_total"],
        "user_verified_at": row["user_verified_at"],
        **activity,
    }


def _summarize_activity(entries: list[dict]) -> dict:
    """Counters kept on the SAME two independent axes the rest of this
    project uses (see services.py's activity section header): transport
    outcome and console confirmation are counted separately and never
    added together into one "installed" number."""
    return {
        "total": len(entries),
        "transport_verified": sum(1 for e in entries if e["kind"] == "TRANSPORT_VERIFIED"),
        "transport_unverified": sum(1 for e in entries if e["kind"] == "TRANSPORT_UNVERIFIED"),
        "console_confirmed_success": sum(1 for e in entries if e["kind"] == "CONSOLE_CONFIRMED_SUCCESS"),
        "console_confirmed_failed": sum(1 for e in entries if e["kind"] == "CONSOLE_CONFIRMED_FAILED"),
        "failed": sum(1 for e in entries if e["kind"] == "FAILED"),
        "interrupted": sum(1 for e in entries if e["kind"] == "INTERRUPTED"),
        "destination_conflict": sum(1 for e in entries if e["kind"] == "DESTINATION_CONFLICT"),
        "source_changed": sum(1 for e in entries if e["kind"] == "SOURCE_CHANGED"),
    }


def build_game_detail_view(conn, ctx: WebContext, raw_base_title_id: str) -> Optional[dict]:
    """W3-001's whole page, as one dict. Returns None when the URL isn't a
    valid TITLE_ID, or when nothing in the library belongs to that family
    (the route turns that into a 404 -- never an empty page pretending the
    game exists).

    The family itself is taken straight out of
    services.list_library_view(kind="games"): identical grouping, identical
    duplicate handling, identical name resolution as the Library page's own
    card for this game. A family whose base game is not in the library
    (update/DLC/mod only) is a normal, fully-rendered case -- `base` is
    simply None, exactly as the Library card already handles it."""
    base_title_id = normalize_base_title_id(raw_base_title_id)
    if base_title_id is None:
        return None

    # installed_on_device_base_ids: same connected-only cache the Library
    # page reads (ctx.get_known_installed_title_ids()) -- omitting it here
    # left every Game Details page's "On Switch" badge permanently false,
    # regardless of what the Library page showed for the exact same family.
    view = services.list_library_view(
        conn, kind="games", installed_on_device_base_ids=ctx.get_known_installed_title_ids(),
    )
    family = next((g for g in view["games"] if g["base_title_id"] == base_title_id), None)
    if family is None:
        return None

    by_device: dict[str, list[dict]] = {}
    for row in _family_history_rows(conn, base_title_id):
        by_device.setdefault(row["target_device_id"], []).append(_activity_entry(row))
    for entries in by_device.values():
        entries.sort(key=lambda e: e["created_at"], reverse=True)

    # Every KNOWN Switch gets a section, including ones this family has
    # never been sent to ("no recorded activity" is itself the honest
    # answer, and is very different from "not installed"). Any device_id
    # that only exists in history (e.g. its devices row was never written)
    # is appended rather than dropped.
    devices = services.list_devices(conn, ctx)
    device_sections = []
    seen_ids = set()
    for device in devices:
        seen_ids.add(device["device_id"])
        entries = by_device.get(device["device_id"], [])
        device_sections.append({
            "device_fingerprint": device["device_fingerprint"],
            "display_name": device["display_name"],
            "connected": device["connected"],
            "entries": entries,
            "summary": _summarize_activity(entries),
        })
    for device_id, entries in by_device.items():
        if device_id in seen_ids:
            continue
        device_sections.append({
            "device_fingerprint": device_fingerprint(device_id),
            "display_name": services.device_label(conn, device_id),
            "connected": False,
            "entries": entries,
            "summary": _summarize_activity(entries),
        })

    all_entries = [e for section in device_sections for e in section["entries"]]
    return {
        "base_title_id": base_title_id,
        "name": family["name"],
        "family": family,
        "base": family["base"],
        "updates": family["updates"],
        "dlc": family["dlc"],
        "mods": family["mods"],
        "sd_files": family["sd_files"],
        "launch_checks": family["launch_checks"],
        "duplicates": family["duplicates"],
        # Entries the scanner could not classify or that failed extraction.
        # Kept as their own section so they are never quietly mixed in with
        # healthy, installable content.
        "needs_review": [
            e for e in services._family_entries(family) if e["library_status"] in ("NEEDS_REVIEW", "ERROR")
        ],
        "base_count": (1 if family["base"] else 0) + len(family["duplicates"]),
        "update_count": len(family["updates"]),
        "dlc_count": len(family["dlc"]),
        "mod_count": len(family["mods"]),
        "sd_count": len(family["sd_files"]),
        "total_size": family["total_size"],
        "first_seen_at": family["first_seen_at"],
        "last_scanned_at": family["last_scanned_at"],
        "device_sections": device_sections,
        "activity_summary": _summarize_activity(all_entries),
        "has_any_activity": bool(all_entries),
    }


# ---------------------------------------------------------------------------
# W3-004: Device Details
# ---------------------------------------------------------------------------


def resolve_device_id_by_fingerprint(conn, fingerprint: str) -> Optional[str]:
    """fingerprint -> the raw device_id it stands for, or None.

    `device_fingerprint` is a sha256 prefix, not a stored column, so this
    recomputes it for each known device and compares. That is deliberate
    and cheap: the `devices` table holds the handful of Switches this one
    machine has ever seen (this is a personal USB tool, not a fleet
    manager), so a linear scan needs no new column, no new index, and no
    migration. Comparison is case-insensitive because the fingerprint
    arrives from a URL a human may have retyped."""
    if not fingerprint:
        return None
    needle = fingerprint.strip().lower()
    for row in db.list_devices(conn):
        if device_fingerprint(row["device_id"]).lower() == needle:
            return row["device_id"]
    return None


def _device_diagnostics_summary(conn, ctx: WebContext, device_id: str, storages: list[dict]) -> dict:
    """A per-device read of state this app already tracks -- never a live
    COM probe (this runs on an HTTP request thread; see WebContext's
    class-level note and services.get_diagnostics_report's probe_com=False
    for exactly why that rule exists)."""
    jobs = [j for j in db.list_jobs(conn) if j["target_device_id"] == device_id]
    unfinished = [j for j in jobs if j["status"] not in ("DONE", "DONE_UNVERIFIED", "FAILED", "INTERRUPTED")]
    mappings = db.list_device_storage_mappings(conn, device_id)
    writable = [s for s in storages if s["writable"]]
    return {
        "storage_count": len(storages),
        "writable_storage_count": len(writable),
        "has_sd_card": any(s["effective_logical_name"] == "SD_CARD" for s in storages),
        "has_sd_install": any(s["effective_logical_name"] == "SD_INSTALL" for s in storages),
        "manual_mapping_count": len(mappings),
        "job_count": len(jobs),
        "unfinished_job_count": len(unfinished),
        "error_job_count": sum(1 for j in jobs if j["error"]),
        # Same HTTP-safe, COM-free report the Settings page renders; shown
        # here only as overall app health context next to the device's own
        # numbers, never as a claim about the device itself.
        "app_overall_status": services.get_diagnostics_report(conn, ctx).overall_status,
    }


def build_device_detail_view(conn, ctx: WebContext, fingerprint: str) -> Optional[dict]:
    """W3-004's whole page. Returns None if no known device has this
    fingerprint (the route turns that into a 404).

    Every field here is safe to render as visible text: the friendly/
    display name, the fingerprint itself, and StorageInfo.raw_name (DBI's
    own storage label, e.g. "1: SD Card" -- established non-identifying
    text, see mtp/base.py's StorageInfo.raw_name and the existing Devices
    page which already renders it). The raw device_id is resolved and used
    server-side only, and is deliberately NOT part of this dict."""
    device_id = resolve_device_id_by_fingerprint(conn, fingerprint)
    if device_id is None:
        return None

    device = next(
        (d for d in services.list_devices(conn, ctx) if d["device_id"] == device_id), None
    )
    if device is None:  # pragma: no cover -- resolve_* already proved the row exists
        return None

    storages = services.list_device_storages(conn, ctx, device_id)
    library_items = db.list_library_items(conn)

    history_rows = [r for r in db.list_all_install_history(conn) if r["target_device_id"] == device_id]
    jobs_by_id = {j["id"]: j for j in db.list_jobs(conn)}

    entries = []
    for row in history_rows:
        entry = _activity_entry(row)
        job = jobs_by_id.get(row["job_id"])
        entry["batch_id"] = job["batch_id"] if job is not None else None
        entry["family_base_title_id"] = services.family_base_title_id(row["title_id"])
        entries.append(entry)
    entries.sort(key=lambda e: e["created_at"], reverse=True)

    # Batches, newest first. A pre-UI-002 row with batch_id=NULL becomes its
    # own single-entry group, exactly like list_history_grouped() does.
    batches: list[dict] = []
    by_batch: dict[Optional[int], list[dict]] = {}
    for entry in entries:
        by_batch.setdefault(entry["batch_id"], []).append(entry)
    for batch_id, group in by_batch.items():
        batches.append({
            "batch_id": batch_id,
            "entries": group,
            "created_at": max(e["created_at"] for e in group),
            "summary": _summarize_activity(group),
        })
    batches.sort(key=lambda b: b["created_at"], reverse=True)

    # Game families this device has been involved with, each linking to its
    # own W3-001 page. Named via the SAME family-name lookup the Library
    # and Queue/History share (PERF-001's library_items threaded through),
    # falling back to the recorded display_name and finally the bare
    # TITLE_ID -- never an invented name.
    families: dict[str, dict] = {}
    for entry in entries:
        family_id = entry["family_base_title_id"]
        if family_id is None:
            continue
        family = families.get(family_id)
        if family is None:
            source = queue_worker.find_family_base_name_source(conn, family_id, library_items=library_items)
            name = (
                queue_worker.resolve_library_item_display_name(conn, source, library_items=library_items)
                if source is not None else (entry["display_name"] or family_id)
            )
            family = families[family_id] = {
                "base_title_id": family_id, "name": name, "entries": [], "last_activity_at": entry["created_at"],
            }
        family["entries"].append(entry)
        family["last_activity_at"] = max(family["last_activity_at"], entry["created_at"])
    family_list = sorted(families.values(), key=lambda f: f["last_activity_at"], reverse=True)
    for family in family_list:
        family["summary"] = _summarize_activity(family["entries"])

    return {
        "device_fingerprint": device["device_fingerprint"],
        "display_name": device["display_name"],
        "friendly_name": device["friendly_name"],
        "last_known_display_name": device["last_known_display_name"],
        "connected": device["connected"],
        "first_seen_at": device["first_seen_at"],
        "last_seen_at": device["last_seen_at"],
        "storages": storages,
        "diagnostics": _device_diagnostics_summary(conn, ctx, device_id, storages),
        "entries": entries,
        "batches": batches,
        "families": family_list,
        "summary": _summarize_activity(entries),
    }


def build_device_diagnostics_export(conn, ctx: WebContext, device_id: str, fingerprint: str) -> dict:
    """W3-004's "Export diagnostics for this device" -- the EXISTING
    services.get_diagnostics_export() payload (same single diagnostics
    engine, see diagnostics.py's module docstring), narrowed to one device
    rather than a second, parallel export:

      - `recent_job_errors` is re-read scoped to this device (the same
        allowlisted, raw-device_id-free shape FIX-001 established);
      - `device_fingerprints` is narrowed to just this one;
      - an additive `device` block carries this device's own safe facts.

    Nothing device-identifying beyond the fingerprint is added: no raw
    device_id, no serial, exactly like the whole-app export."""
    payload = services.get_diagnostics_export(conn, ctx)
    payload["recent_job_errors"] = services.recent_job_errors(conn, target_device_id=device_id)
    payload["device_fingerprints"] = [fingerprint]
    payload["scoped_to_device_fingerprint"] = fingerprint

    view = build_device_detail_view(conn, ctx, fingerprint)
    if view is not None:
        payload["device"] = {
            "device_fingerprint": view["device_fingerprint"],
            "display_name": view["display_name"],
            "connected": view["connected"],
            "first_seen_at": view["first_seen_at"],
            "last_seen_at": view["last_seen_at"],
            "storages": view["storages"],
            "diagnostics": view["diagnostics"],
            "activity_summary": view["summary"],
        }
    return payload


def device_diagnostics_export_to_text(payload: dict) -> str:
    """Reuses diagnostics.export_dict_to_text() unchanged and appends the
    device block this module added -- rather than editing that shared
    formatter for one page's extra section."""
    from .. import diagnostics

    lines = [diagnostics.export_dict_to_text(payload)]
    device = payload.get("device")
    if device:
        lines.append("")
        lines.append(f"Device {device['device_fingerprint']} ({device['display_name']}):")
        lines.append(f"  connected: {device['connected']}")
        lines.append(f"  first seen: {device['first_seen_at']}")
        lines.append(f"  last seen: {device['last_seen_at']}")
        for storage in device["storages"]:
            lines.append(
                f"  storage {storage['raw_name']} -> {storage['effective_logical_name']} "
                f"({storage['mapping_source']})"
            )
        summary = device["activity_summary"]
        lines.append(
            "  SwitchAgent activity: "
            + ", ".join(f"{key}={value}" for key, value in sorted(summary.items()))
        )
    return "\n".join(lines)
