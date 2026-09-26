"""The Amiibo page: emuiibo and virtual amiibo, on each console and in the
Library, side by side.

Everything here is a read -- of the database (what the worker last read off
a console, see web/emuiibo_service.py) and of Library files on this PC. No
COM, no device access: safe from any request thread.

What the page owes its reader, in order:
  1. Can this console use virtual amiibo? Which of emuiibo's parts are
     there, which are missing, and where each missing one comes from -- the
     Library, when it holds one, otherwise the project's own release page.
  2. Which amiibo are on it (with what emuiibo wrote into them: save data,
     favorites, hidden ones).
  3. Which amiibo of the Library are not on it yet.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import db, emuiibo, extractor, scanner
from ..model import ContentType


# ---------------------------------------------------------------------------
# Library collections, amiibo by amiibo
# ---------------------------------------------------------------------------

_CACHE_LIMIT = 16
_cache: "OrderedDict[tuple, list[dict]]" = OrderedDict()
_cache_lock = threading.Lock()
# Reading every amiibo.json of a collection: the real pack's 766 are 390 KB.
_MAX_JSON_BYTES = 32 * 1024 * 1024


def collection_amiibo(row) -> list[dict]:
    """Every amiibo of one AMIIBO Library item: where it goes, and -- read
    from its own amiibo.json -- its name and figure id. Cached per file
    state, so a page polling every few seconds lists an archive once."""
    key = (row["absolute_path"], row["size"], row["mtime"], row["content_hash"])
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    path = Path(row["absolute_path"])
    collection = None
    jsons: dict[str, bytes] = {}
    try:
        if path.is_dir():
            collection = scanner.folder_collection(path)
            for a in collection.amiibo if collection else ():
                if a.kind == "virtual":
                    try:
                        jsons[a.source] = (path / a.source / emuiibo.AMIIBO_JSON).read_bytes() if a.source \
                            else (path / emuiibo.AMIIBO_JSON).read_bytes()
                    except OSError:
                        pass
        elif path.is_file():
            entries = extractor.list_archive_entries(path)
            collection = emuiibo.find_collection(extractor.file_list(entries), wrapper_name=path.stem)
            if collection is not None:
                by_name = {e.name.lower(): e.name for e in entries if not e.is_dir}
                wanted = {}
                for a in collection.amiibo:
                    if a.kind != "virtual":
                        continue
                    actual = by_name.get(f"{a.source}/{emuiibo.AMIIBO_JSON}".lower())
                    if actual:
                        wanted[actual] = a.source
                read = extractor.read_archive_members(path, list(wanted), max_total_bytes=_MAX_JSON_BYTES)
                jsons = {wanted[name]: data for name, data in read.items()}
    except (OSError, extractor.ArchiveError):
        collection = None
    out = []
    for a in collection.amiibo if collection else ():
        info = emuiibo.parse_amiibo_json(jsons[a.source]) if a.source in jsons else None
        out.append({
            "dest": a.dest, "group": a.group, "label": a.label, "kind": a.kind, "enabled": a.enabled,
            "name": info.name if info else None,
            "amiibo_id": info.id.hex if info else None,
            "files": len(a.files),
        })
    with _cache_lock:
        _cache[key] = out
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return out


def _on_console(entry: dict, device_by_path: dict[str, dict]) -> str:
    """"on_console" / "different" (a different amiibo holds that folder --
    an install leaves it alone) / "missing". A raw dump is never "on the
    console" by path: emuiibo converts it into a folder named after the
    amiibo at the next boot and the .bin is gone from where it was put."""
    present = device_by_path.get(entry["dest"].lower())
    if present is None:
        return "missing"
    if entry["amiibo_id"] and present.get("amiibo_id") and entry["amiibo_id"] != present["amiibo_id"]:
        return "different"
    return "on_console"


def collection_view(row, device_amiibo: Optional[list[dict]]) -> dict:
    amiibo = collection_amiibo(row)
    by_path = {a["path"].lower(): a for a in device_amiibo or []}
    groups: "OrderedDict[str, dict]" = OrderedDict()
    counts = {"on_console": 0, "different": 0, "missing": 0}
    for entry in sorted(amiibo, key=lambda a: (a["group"].lower(), a["label"].lower())):
        status = _on_console(entry, by_path) if device_amiibo is not None else None
        if status:
            counts[status] += 1
        group = groups.setdefault(entry["group"], {"name": entry["group"], "amiibo": []})
        group["amiibo"].append({**entry, "status": status})
    return {
        "id": row["id"], "name": Path(row["absolute_path"]).name, "path": row["absolute_path"],
        "count": len(amiibo), "disabled": sum(1 for a in amiibo if not a["enabled"]),
        # Top-level folders, as the Library card counts them (a nested
        # "Animal Crossing/Spork" is still part of "Animal Crossing").
        "folders": len({a["dest"].split("/", 1)[0] for a in amiibo if "/" in a["dest"]}),
        "dumps": sum(1 for a in amiibo if a["kind"] == "dump"),
        "groups": list(groups.values()), "counts": counts if device_amiibo is not None else None,
        "can_install": row["status"] == "AVAILABLE",
    }


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def _local_label(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return iso
    today = datetime.now().astimezone().date()
    return moment.strftime("%H:%M") if moment.date() == today else moment.strftime("%Y-%m-%d %H:%M")


def library_releases(conn) -> list[dict]:
    """emuiibo releases in the Library, newest version first."""
    out = []
    for row in db.list_library_items(conn):
        if row["content_type"] != ContentType.EMUIIBO.value or row["status"] == db.LIBRARY_ITEM_RETIRED:
            continue
        from . import services
        fields = services._emuiibo_fields(row)
        out.append({
            "id": row["id"], "name": Path(row["absolute_path"]).name,
            "version": fields["emuiibo_version"], "outdated": fields["emuiibo_outdated"],
            "can_install": row["status"] == "AVAILABLE",
        })
    out.sort(key=lambda r: emuiibo.version_tuple(r["version"]) or (), reverse=True)
    return out


def library_pc_tools(conn) -> list[dict]:
    """emuiibo's PC apps in the Library (emutool, emuiigen): not for a
    Switch, and not games -- listed here, with the rest of emuiibo."""
    from ..scanner import NOT_FOR_SWITCH

    return [{"id": row["id"], "name": Path(row["absolute_path"]).name, "note": row["note"]}
            for row in db.list_library_items(conn) if row["status"] == NOT_FOR_SWITCH]


def library_collections(conn) -> list:
    return [row for row in db.list_library_items(conn)
            if row["content_type"] == ContentType.AMIIBO.value and row["status"] != db.LIBRARY_ITEM_RETIRED]


def _console_view(conn, device_id: str) -> Optional[dict]:
    stored = db.get_device_emuiibo(conn, device_id)
    if stored is None:
        return None
    state_dict, amiibo, read_at = stored
    state = emuiibo.DeviceState.from_dict(state_dict, [])
    favorites = {f.lower() for f in state.favorites}
    items = []
    for a in amiibo:
        items.append({**a, "favorite": a["path"].lower() in favorites,
                      "group": a["path"].rpartition("/")[0], "label": a["path"].rpartition("/")[2]})
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for item in sorted(items, key=lambda a: (a["group"].lower(), a["label"].lower())):
        groups.setdefault(item["group"], {"name": item["group"], "amiibo": []})["amiibo"].append(item)
    return {
        "components": [
            {"key": c.key, "name": c.name, "purpose": c.purpose, "source": c.source,
             "present": bool(state.components.get(c.key)), "in_release": c.in_emuiibo_release,
             "paths": list(c.paths)}
            for c in emuiibo.COMPONENTS
        ],
        "installed": state.installed,
        "overlay_version": state.overlay_version,
        "overlay_outdated": emuiibo.is_outdated(state.overlay_version),
        "emulation_on": state.emulation_on,
        "library_exists": state.library_exists,
        "amiibo": items,
        "groups": list(groups.values()),
        "count": len(items),
        "with_save_data": sum(1 for a in items if a.get("save_data")),
        "hidden": sum(1 for a in items if not a.get("enabled") and a.get("kind") != "dump"),
        "invalid": sum(1 for a in items if a.get("kind") != "dump" and not a.get("name")),
        "dumps": sum(1 for a in items if a.get("kind") == "dump"),
        "favorites": len(favorites),
        "read_at": read_at,
        "read_at_label": _local_label(read_at),
    }


def _advice(console: Optional[dict], releases: list[dict], connected: bool) -> list[dict]:
    """What to do next, most important first -- each with where it comes
    from. emuiibo itself is installed from the Library when the Library
    holds a current release, and otherwise downloaded from GitHub and
    installed in the same click ("download_release"); an outdated release
    in the Library is never what the page recommends."""
    if console is None:
        return []
    out = []
    best = next((r for r in releases if r["can_install"]), None)
    current = best if best is not None and not best["outdated"] else None
    present = {c["key"]: c["present"] for c in console["components"]}
    missing_own = [c for c in console["components"] if c["in_release"] and not c["present"]]
    on_console = console["overlay_version"]

    def offer(text_install: str, text_download: str) -> None:
        if current is not None:
            out.append({"kind": "install_release", "item_id": current["id"], "text": text_install,
                        "outdated": False})
        else:
            older = f" (your Library has only {best['version']}, which is outdated)" if best and best["version"] else ""
            out.append({"kind": "download_release", "text": text_download + older})

    if missing_own:
        what = ("emuiibo is not on this Switch" if len(missing_own) == 2
                else f"The {missing_own[0]['name']} is missing on this Switch")
        version = f" {current['version']}" if current and current["version"] else ""
        offer(f"{what}. Install emuiibo{version} from your Library.",
              f"{what}. Download the current emuiibo from GitHub and install it")
    elif on_console and current is not None and current["version"] and (
            (emuiibo.version_tuple(current["version"]) or ()) > (emuiibo.version_tuple(on_console) or ())):
        offer(f"emuiibo {on_console} is on this Switch; your Library has {current['version']}. Installing it "
              "replaces emuiibo's own files only -- amiibo and their save data stay.", "")
    elif console["overlay_outdated"]:
        offer("", f"emuiibo {on_console} is on this Switch; {emuiibo.LATEST_KNOWN_VERSION} is current. Download it "
                  "from GitHub and update -- emuiibo's own files are replaced, amiibo and their save data stay")
    missing_menu = [emuiibo.COMPONENTS_BY_KEY[k].name for k in ("tesla_menu", "ovlloader") if not present.get(k)]
    if missing_menu:
        out.append({"kind": "install_addon", "addon": "ultrahand",
                    "text": f"The overlay menu is missing ({' and '.join(missing_menu)}) -- without it the "
                            "emuiibo overlay cannot be opened to choose an amiibo. Ultrahand Overlay brings "
                            "both; restart the Switch after installing it."})
    return out


# ---------------------------------------------------------------------------
# emuiibo on a console -- and so whether there is an Amiibo tab at all
# ---------------------------------------------------------------------------

_DELIVERED = ("DONE", "DONE_UNVERIFIED")
_WAITING = tuple(s for s in db.ACTIVE_OR_DONE_JOB_STATUSES if s not in _DELIVERED)


def _emuiibo_jobs(conn) -> list:
    return conn.execute(
        "SELECT j.target_device_id AS device_id, j.status, j.finished_at FROM jobs j "
        "JOIN library_items li ON li.id = j.library_item_id WHERE li.content_type = ?",
        (ContentType.EMUIIBO.value,),
    ).fetchall()


def emuiibo_on_device(conn, device_id: str, *, jobs: Optional[list] = None) -> str:
    """emuiibo on one console: "installed", "partial" (the module or the
    overlay only), "missing", "queued" (an install of it is in the Queue) or
    "unknown" (never read). What its last read found -- unless SwitchAgent
    has delivered emuiibo there since, which the next read will confirm."""
    stored = db.get_device_emuiibo(conn, device_id)
    read_at = stored[2] if stored else None
    mine = [j for j in (jobs if jobs is not None else _emuiibo_jobs(conn)) if j["device_id"] == device_id]
    if any(j["status"] in _DELIVERED and (read_at is None or (j["finished_at"] or "") > read_at) for j in mine):
        return "installed"
    components = (stored[0].get("components") or {}) if stored else {}
    parts = [bool(components.get("sysmodule")), bool(components.get("overlay"))]
    if all(parts):
        return "installed"
    if any(j["status"] in _WAITING for j in mine):
        return "queued"
    if stored is None:
        return "unknown"
    return "partial" if any(parts) else "missing"


def amiibo_tab_visible(conn) -> bool:
    """The Amiibo tab exists once emuiibo does: on some console as of its
    last read, or delivered there by SwitchAgent. Before that there is
    nothing on it to manage -- emuiibo is installed from Add-ons, or offered
    when a game's amiibo are installed."""
    jobs = _emuiibo_jobs(conn)
    devices = set(db.list_devices_with_emuiibo(conn)) | {j["device_id"] for j in jobs if j["status"] in _DELIVERED}
    return any(emuiibo_on_device(conn, d, jobs=jobs) in ("installed", "partial") for d in devices)


def emuiibo_offer(conn, ctx, device_id: str) -> dict:
    """For the install confirmation of a selection with amiibo: does this
    console need emuiibo too? `offer` when it is missing, incomplete or not
    read yet (`state` says which); never while a download of it is running
    or an install of it is queued."""
    state = emuiibo_on_device(conn, device_id)
    download = ctx.emuiibo_downloads.snapshot()
    from . import addons_views

    # What comes with it: its overlay menu, when the console lacks one.
    also = [a.name for a in addons_views.plan_install(conn, device_id, "emuiibo") if a.id != "emuiibo"]
    busy = download is not None and download["state"] not in ("done", "failed")
    return {
        "state": state,
        "offer": state in ("missing", "partial", "unknown") and not busy,
        "busy": busy,
        "also": also,
        "latest_version": emuiibo.LATEST_KNOWN_VERSION,
    }


def page(conn, ctx, fingerprint: Optional[str] = None) -> dict:
    from . import services
    from ..mtp.windows import device_fingerprint

    devices = services.list_devices(conn, ctx)
    known = set(db.list_devices_with_emuiibo(conn))
    for d in devices:
        d["has_state"] = d["device_id"] in known
    chosen = next((d for d in devices if d["device_fingerprint"] == fingerprint), None) if fingerprint else None
    if chosen is None:
        # A connected Switch that has emuiibo is what this page is about;
        # then any connected one; then whichever was read last time.
        with_emuiibo = set()
        for d in devices:
            stored = db.get_device_emuiibo(conn, d["device_id"]) if d["has_state"] else None
            if stored and stored[0].get("components", {}).get("sysmodule"):
                with_emuiibo.add(d["device_id"])
        chosen = (next((d for d in devices if d["connected"] and d["device_id"] in with_emuiibo), None)
                  or next((d for d in devices if d["connected"]), None)
                  or next((d for d in devices if d["has_state"]), None))

    releases = library_releases(conn)
    console = _console_view(conn, chosen["device_id"]) if chosen else None
    activity = ctx.emuiibo.snapshot(chosen["device_id"]) if chosen else None
    collections = [collection_view(row, console["amiibo"] if console else None) for row in library_collections(conn)]
    return {
        "devices": [{"fingerprint": d["device_fingerprint"], "name": d["display_name"],
                     "connected": d["connected"], "has_state": d["has_state"]} for d in devices],
        "device": ({"fingerprint": device_fingerprint(chosen["device_id"]), "name": chosen["display_name"],
                    "connected": chosen["connected"]} if chosen else None),
        "console": console,
        "activity": ({**activity, "download": ctx.emuiibo_downloads.snapshot()} if activity is not None
                     else {"download": ctx.emuiibo_downloads.snapshot()}),
        "advice": _advice(console, releases, bool(chosen and chosen["connected"])),
        "releases": releases,
        "collections": collections,
        "pc_tools": library_pc_tools(conn),
        "latest_version": emuiibo.LATEST_KNOWN_VERSION,
        "releases_url": emuiibo.RELEASES_URL,
    }
