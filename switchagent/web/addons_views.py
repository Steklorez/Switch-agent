"""The Add-ons tab: a hand-curated catalog of open-source Switch utilities
(web/addons/catalog.yaml) -- what each one is, how it works, how it gets
onto a console, how to use it -- and, for the ones SwitchAgent can check,
whether the chosen Switch has it.

A read of the catalog file and of the database only (what the worker last
read off a console -- web/emuiibo_service.py): no COM, no network, safe from
any request thread. The one action on the page, installing emuiibo, is the
Amiibo tab's own GitHub download (POST /api/amiibo/emuiibo/download).
"""

from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from markupsafe import Markup

from .. import db, emuiibo

CATALOG_PATH = Path(__file__).resolve().parent / "addons" / "catalog.yaml"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# What a catalog entry may name in `status` / `install`: the checks and
# installers SwitchAgent actually has.
STATUS_CHECKS = ("emuiibo", "tesla_menu", "ovlloader")
INSTALLERS = ("emuiibo",)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CODE_RE = re.compile(r"`([^`]+)`")


class CatalogError(ValueError):
    """The catalog file breaks one of its own rules -- the message says which."""


@dataclass(frozen=True)
class Tip:
    problem: Optional[str]
    answer: str


@dataclass(frozen=True)
class Addon:
    id: str
    name: str
    tagline: str
    category: str
    author: str
    license: str
    source: str
    releases: str
    about: tuple[str, ...]
    how_it_works: tuple[str, ...]
    install_steps: tuple[str, ...]
    usage: tuple[str, ...]
    controls: tuple[tuple[str, str], ...] = ()
    tips: tuple[Tip, ...] = ()
    requires: tuple[str, ...] = ()
    preview: Optional[str] = None
    preview_caption: Optional[str] = None
    status: Optional[str] = None
    install: Optional[str] = None
    related: Optional[tuple[str, str]] = None


def _text(entry: dict, key: str, where: str, *, required: bool = True) -> Optional[str]:
    value = entry.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where}: `{key}` must be a non-empty text")
    return value.strip()


def _lines(entry: dict, key: str, where: str, *, required: bool = True) -> tuple[str, ...]:
    value = entry.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise CatalogError(f"{where}: `{key}` must be a list of texts")
    return tuple(v.strip() for v in value)


def _https(url: str, where: str, key: str) -> str:
    if not url.startswith("https://"):
        raise CatalogError(f"{where}: `{key}` must be an https:// link")
    return url


def parse_catalog(doc) -> list[Addon]:
    """The catalog's YAML document -> its entries, in order. Raises
    CatalogError for anything the tab could not show truthfully."""
    if not isinstance(doc, dict) or not isinstance(doc.get("addons"), list):
        raise CatalogError("the catalog must have an `addons:` list")
    out: list[Addon] = []
    for index, entry in enumerate(doc["addons"]):
        where = f"entry {index + 1}"
        if not isinstance(entry, dict):
            raise CatalogError(f"{where}: must be a mapping")
        addon_id = _text(entry, "id", where)
        if not _ID_RE.match(addon_id):
            raise CatalogError(f"{where}: `id` must be lowercase letters, digits and dashes")
        where = f"`{addon_id}`"
        status = _text(entry, "status", where, required=False)
        if status is not None and status not in STATUS_CHECKS:
            raise CatalogError(f"{where}: unknown `status` check {status!r} (known: {', '.join(STATUS_CHECKS)})")
        install = _text(entry, "install", where, required=False)
        if install is not None and install not in INSTALLERS:
            raise CatalogError(f"{where}: unknown `install` {install!r} (known: {', '.join(INSTALLERS)})")
        preview = _text(entry, "preview", where, required=False)
        if preview is not None and (not (STATIC_DIR / preview).is_file() or "/" in preview or "\\" in preview):
            raise CatalogError(f"{where}: `preview` {preview!r} is not a file in web/static/")
        requires = entry.get("requires") or []
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise CatalogError(f"{where}: `requires` must be a list of ids")
        controls = []
        for control in entry.get("controls") or []:
            if not isinstance(control, dict) or not control.get("keys") or not control.get("action"):
                raise CatalogError(f"{where}: every control needs `keys` and `action`")
            controls.append((str(control["keys"]), str(control["action"])))
        tips = []
        for tip in entry.get("tips") or []:
            if isinstance(tip, str) and tip.strip():
                tips.append(Tip(problem=None, answer=tip.strip()))
            elif isinstance(tip, dict) and tip.get("problem") and tip.get("answer"):
                tips.append(Tip(problem=str(tip["problem"]).strip(), answer=str(tip["answer"]).strip()))
            else:
                raise CatalogError(f"{where}: a tip is a sentence or a {{problem, answer}} pair")
        related = entry.get("related")
        if related is not None:
            if not isinstance(related, dict) or not related.get("label") or not str(related.get("href", "")).startswith("/"):
                raise CatalogError(f"{where}: `related` needs a `label` and an in-app `href` starting with /")
            related = (str(related["label"]), str(related["href"]))
        about = _text(entry, "about", where)
        out.append(Addon(
            id=addon_id, name=_text(entry, "name", where), tagline=_text(entry, "tagline", where),
            category=_text(entry, "category", where), author=_text(entry, "author", where),
            license=_text(entry, "license", where),
            source=_https(_text(entry, "source", where), where, "source"),
            releases=_https(_text(entry, "releases", where), where, "releases"),
            about=tuple(p.strip() for p in re.split(r"\n\s*\n", about) if p.strip()),
            how_it_works=_lines(entry, "how_it_works", where),
            install_steps=_lines(entry, "install_steps", where),
            usage=_lines(entry, "usage", where),
            controls=tuple(controls), tips=tuple(tips), requires=tuple(requires),
            preview=preview, preview_caption=_text(entry, "preview_caption", where, required=False),
            status=status, install=install, related=related,
        ))
    ids = [a.id for a in out]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise CatalogError(f"ids used twice: {', '.join(duplicates)}")
    for addon in out:
        unknown = [r for r in addon.requires if r not in ids]
        if unknown:
            raise CatalogError(f"`{addon.id}` requires {', '.join(unknown)}, which the catalog does not have")
    return out


_catalog_lock = threading.Lock()
_catalog_cache: Optional[tuple[float, list[Addon]]] = None


def load_catalog(path: Path = CATALOG_PATH) -> list[Addon]:
    """The catalog, re-read when the file changes (so an edited entry shows
    up without restarting)."""
    global _catalog_cache
    import yaml

    mtime = path.stat().st_mtime
    with _catalog_lock:
        if _catalog_cache is not None and _catalog_cache[0] == mtime and path == CATALOG_PATH:
            return _catalog_cache[1]
    addons = parse_catalog(yaml.safe_load(path.read_text(encoding="utf-8")))
    if path == CATALOG_PATH:
        with _catalog_lock:
            _catalog_cache = (mtime, addons)
    return addons


def rich(text: str) -> Markup:
    """Catalog text for the page: escaped, with `backticks` as <code>."""
    return Markup(_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", html.escape(text, quote=False)))


# ---------------------------------------------------------------------------
# The chosen Switch
# ---------------------------------------------------------------------------

def addon_status(addon: Addon, state: Optional[dict]) -> Optional[dict]:
    """{state, label, version} for an entry with a `status` check, as of the
    console's last read; None for an entry SwitchAgent cannot check.
    state: "installed" / "partial" / "missing" / "unknown" (never read)."""
    if addon.status is None:
        return None
    if state is None:
        return {"state": "unknown", "label": "not read yet", "version": None}
    components = state.get("components") or {}
    if addon.status == "emuiibo":
        have = [bool(components.get("sysmodule")), bool(components.get("overlay"))]
        version = state.get("overlay_version")
        if all(have):
            outdated = emuiibo.is_outdated(version)
            label = f"installed{' — ' + version if version else ''}"
            if outdated:
                label += f" (outdated, {emuiibo.LATEST_KNOWN_VERSION} is current)"
            return {"state": "outdated" if outdated else "installed", "label": label, "version": version}
        if any(have):
            missing = "the overlay" if have[0] else "the module"
            return {"state": "partial", "label": f"incomplete — {missing} is missing", "version": version}
        return {"state": "missing", "label": "not installed", "version": None}
    present = bool(components.get(addon.status))
    return {"state": "installed" if present else "missing",
            "label": "installed" if present else "not installed", "version": None}


def page(conn, ctx, fingerprint: Optional[str] = None) -> dict:
    from . import services

    devices = services.list_devices(conn, ctx)
    chosen = next((d for d in devices if d["device_fingerprint"] == fingerprint), None) if fingerprint else None
    chosen = chosen or next((d for d in devices if d["connected"]), None) or (devices[0] if devices else None)
    stored = db.get_device_emuiibo(conn, chosen["device_id"]) if chosen else None
    state = stored[0] if stored else None
    catalog = load_catalog()
    by_id = {a.id: a for a in catalog}
    entries = []
    for addon in catalog:
        status = addon_status(addon, state)
        entries.append({
            "addon": addon,
            "status": status,
            "requires": [by_id[r] for r in addon.requires],
            "can_install": bool(addon.install and chosen and chosen["connected"]
                                and (status is None or status["state"] != "installed")),
        })
    return {
        "entries": entries,
        "devices": [{"fingerprint": d["device_fingerprint"], "name": d["display_name"], "connected": d["connected"]}
                    for d in devices],
        "device": ({"fingerprint": chosen["device_fingerprint"], "name": chosen["display_name"],
                    "connected": chosen["connected"]} if chosen else None),
        "read_at": stored[2] if stored else None,
        "download": ctx.emuiibo_downloads.snapshot(),
    }
