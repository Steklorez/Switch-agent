"""Installing Add-ons catalog utilities -- and emuiibo -- from GitHub.

An Install click (Add-ons, or the emuiibo offer in Library's install
confirmation) names one add-on; the page adds whatever it requires that the
console does not have yet (addons.install_order). This runs that list in
order, on its own thread, network only -- never COM, never the device:

  for each add-on: the latest release from its own GitHub repository
  (github_release.py -- GitHub's hosts only, declared size, published
  SHA-256), checked to be exactly that add-on (addons.match_archive /
  match_program, or emuiibo.plan_release), kept in a Library folder
  ("SwitchAgent downloads"), indexed like any Library file;

and only once every one of them is in the Library, all of them go to the
ordinary preparation queue together, for one console. One that fails
stops the rest and nothing is queued: half a chain (FPSLocker without the
SaltyNX it runs on) is worse than none, and the page says what failed.

A file already in the Library with the published checksum is used as it
is, not downloaded again. One install at a time.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from .. import addons, db, github_release, nro

log = logging.getLogger("switchagent.web.addons")

_STEP_FIELDS = ("id", "name", "state", "version", "received", "total", "error", "item_id", "file",
                "reused", "verified")


class DownloadRefused(Exception):
    """An install the installer will not start -- the message says why."""


def _safe_file_part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._+-]+", "-", text).strip(" .-") or "release"


class AddonInstaller:
    def __init__(self, db_path, preparations, *, opener=None):
        self.db_path = Path(db_path)
        self.preparations = preparations
        self._opener = opener
        self._lock = threading.Lock()
        self._state: Optional[dict] = None

    # -- HTTP threads ------------------------------------------------------

    def start(self, target_device_id: Optional[str], addon_ids=("emuiibo",)) -> dict:
        """Starts downloading `addon_ids` (in that order -- requirements
        first is the caller's job, see addons.install_order) and, given a
        console, queueing them for it."""
        from .. import config

        known = {a.id: a for a in addons.load_catalog()}
        steps = []
        for addon_id in addon_ids:
            addon = known.get(addon_id)
            if addon is None or not addon.installable:
                raise DownloadRefused(f"{addon_id!r} is not something SwitchAgent can install")
            steps.append({"id": addon.id, "name": addon.name, "state": "waiting", "version": None,
                          "received": 0, "total": None, "error": None, "item_id": None, "file": None,
                          "reused": False, "verified": False})
        if not steps:
            raise DownloadRefused("nothing to install")
        with self._lock:
            if self._state is not None and self._state["state"] not in ("done", "failed"):
                raise DownloadRefused("an install is already being downloaded")
            if not config.library_dirs():
                raise DownloadRefused("choose a Library folder in Settings first -- the download goes there")
            self._state = {"id": uuid.uuid4().hex[:12], "state": "checking", "steps": steps, "current": 0,
                           "queued": False, "error": None, "device_id": target_device_id,
                           "started": time.time(), "finished": None}
            snapshot = _public(self._state)
        threading.Thread(target=self._run, name="switchagent-addon-install", daemon=True).start()
        return snapshot

    def snapshot(self) -> Optional[dict]:
        with self._lock:
            return _public(self._state) if self._state is not None else None

    def busy(self) -> bool:
        with self._lock:
            return self._state is not None and self._state["state"] not in ("done", "failed")

    # -- its own thread ----------------------------------------------------

    def _step(self, index: int, **fields) -> None:
        with self._lock:
            if self._state is not None:
                self._state["steps"][index].update(fields)
                if "state" in fields and fields["state"] in ("checking", "downloading", "adding"):
                    self._state["state"] = fields["state"]
                    self._state["current"] = index

    def _run(self) -> None:
        with self._lock:
            steps = [dict(s) for s in self._state["steps"]]
            device_id = self._state["device_id"]
        item_ids = []
        failed = None
        for index, step in enumerate(steps):
            self._step(index, state="checking")
            try:
                if step["id"] == "emuiibo":
                    item_id = self._fetch_emuiibo(index)
                else:
                    item_id = self._fetch_addon(index, addons.by_id(step["id"]))
            except github_release.DownloadError as exc:
                log.warning("%s download failed: %s", step["name"], exc)
                failed = f"{step['name']}: {exc}"
            except Exception as exc:  # noqa: BLE001 -- the page must learn it failed, whatever it was
                log.exception("%s download failed unexpectedly", step["name"])
                failed = f"{step['name']}: unexpected error: {type(exc).__name__}: {exc}"
            if failed:
                self._step(index, state="failed", error=failed.split(": ", 1)[1])
                for later in range(index + 1, len(steps)):
                    self._step(later, state="skipped")
                break
            self._step(index, state="ready", item_id=item_id)
            item_ids.append(item_id)
        with self._lock:
            if failed:
                self._state.update(state="failed", error=failed, finished=time.time())
                return
        if device_id and item_ids:
            self.preparations.submit(item_ids, device_id)
        queued = bool(device_id and item_ids)
        with self._lock:
            self._state.update(state="done", queued=queued, finished=time.time())
            self._state["current"] = len(steps) - 1
            for step in self._state["steps"]:
                step["state"] = "queued" if queued else "downloaded"
        log.info("add-ons from GitHub: %s, items %s%s", ", ".join(s["id"] for s in steps), item_ids,
                 ", queued for install" if device_id else "")

    def _library_target(self) -> Path:
        from .. import config

        return config.library_dirs()[0] / github_release.DOWNLOADS_FOLDER

    def _index(self, conn, path: Path, content_type: str, what: str):
        from .. import scanner

        row = scanner.index_library_file(conn, path)
        if row is None or row["content_type"] != content_type or row["status"] != "AVAILABLE":
            raise github_release.DownloadError(f"downloaded, but the Library does not take it for {what}")
        return row

    def _fetch_emuiibo(self, index: int) -> int:
        from .. import emuiibo_download, sd_files
        from ..model import ContentType

        kwargs = {"opener": self._opener} if self._opener is not None else {}
        release = emuiibo_download.latest_release(**kwargs)
        self._step(index, version=release.version, total=release.size)
        with db.open_db(self.db_path) as conn:
            for row in db.list_library_items(conn):
                details = sd_files.row_details(row).get("emuiibo") or {}
                if (row["content_type"] == ContentType.EMUIIBO.value and row["status"] == "AVAILABLE"
                        and details.get("version") == release.version):
                    self._step(index, reused=True, file=row["absolute_path"])
                    return row["id"]
            self._step(index, state="downloading")
            path = emuiibo_download.download(release, self._library_target(),
                                             progress=lambda got, _total: self._step(index, received=got),
                                             **kwargs)
            self._step(index, state="adding", received=release.size, file=str(path),
                       verified=release.sha256 is not None)
            return self._index(conn, path, ContentType.EMUIIBO.value, "an emuiibo release")["id"]

    def _fetch_addon(self, index: int, addon: addons.Addon) -> int:
        from ..model import ContentType

        spec = addon.spec
        kwargs = {"opener": self._opener} if self._opener is not None else {}
        release = github_release.latest_release(spec.repo, spec.asset, max_bytes=addons.MAX_ASSET_BYTES,
                                                what=addon.name, **kwargs)
        self._step(index, version=release.version, total=release.size)
        suffix = PurePosixPath(release.asset_name).suffix.lower() or ".zip"
        file_name = f"{_safe_file_part(addon.name)} {_safe_file_part(release.version)}{suffix}"
        target = self._library_target()
        existing = target / file_name
        reused = existing.is_file() and release.sha256 is not None \
            and github_release._sha256(existing.read_bytes()) == release.sha256
        if not reused:
            self._step(index, state="downloading")
        path = github_release.download(
            release, target, file_name, max_bytes=addons.MAX_ASSET_BYTES,
            check=lambda data: _check_is(addon, release.asset_name, data),
            progress=lambda got, _total: self._step(index, received=got), **kwargs,
        )
        self._step(index, state="adding", received=release.size, file=str(path), reused=reused,
                   verified=release.sha256 is not None)
        with db.open_db(self.db_path) as conn:
            return self._index(conn, path, ContentType.ADDON.value, addon.name)["id"]


def _check_is(addon: addons.Addon, asset_name: str, data: bytes) -> None:
    """The downloaded bytes are exactly this add-on's release -- or the
    download is refused."""
    spec = addon.spec
    if spec.single:
        info = nro.nro_info_from_bytes(data)
        match = addons.match_program(asset_name, info, len(data), catalog=[addon])
        if match is None:
            raise github_release.DownloadError(
                f"the download is not {addon.name} (its NACP says {info.name if info else 'nothing'!r})")
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            files = [(i.filename.replace("\\", "/"), i.file_size) for i in zf.infolist() if not i.is_dir()]
    except zipfile.BadZipFile as exc:
        raise github_release.DownloadError("the download is not a zip") from exc
    if addons.match_archive(files, catalog=[addon]) is None:
        raise github_release.DownloadError(
            f"the download is not {addon.name}'s release as the catalog knows it "
            "(a file of it is missing, or it has files outside its places)")


def _public(state: dict) -> dict:
    """What a page may see: every step, and -- for pages written for one
    download at a time -- the current step's fields at the top level."""
    steps = [{k: s.get(k) for k in _STEP_FIELDS} for s in state["steps"]]
    current = steps[min(state.get("current", 0), len(steps) - 1)]
    top = {k: current[k] for k in ("name", "version", "received", "total", "item_id", "file", "reused", "verified")}
    return {**top, "id": state["id"], "state": state["state"], "error": state["error"] or current.get("error"),
            "queued": state["queued"], "started": state["started"], "finished": state["finished"],
            "steps": steps, "addons": [s["id"] for s in steps]}


# ---------------------------------------------------------------------------
# Which release is current: asked of GitHub at start and once a day
# ---------------------------------------------------------------------------

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
# A start soon after the last check does not ask again: restarting the app a
# few times must not spend GitHub's 60 requests an hour for an
# unauthenticated client (the app's own update check shares them).
START_RECHECK_AFTER_SECONDS = 60 * 60
# Stars change slowly, and each is one more request against GitHub's 60 an
# hour for an unauthenticated client: asked once a week.
STARS_INTERVAL_SECONDS = 7 * 24 * 60 * 60


def _rate_limited(exc: Exception) -> bool:
    """GitHub refusing more requests for now: the rest of this round would
    be refused too -- stop asking, keep what is known, try the next time."""
    text = str(exc)
    return "answered 403" in text or "answered 429" in text


class ReleaseChecker:
    """The latest release of every installable catalog entry, as GitHub
    publishes it -- so the Add-ons tab can offer "Update to X" where a
    console holds an older one.

    Asked once when the app starts and then once a day, on its own thread:
    one plain GET per add-on against GitHub's public releases API (nothing
    about the user, the library or any console travels in it -- the same
    rule as update_check.py). An add-on GitHub does not answer for keeps
    its last known release. The answers are kept in a small JSON file, so
    the page has them right after a restart, before the next check."""

    def __init__(self, cache_path: Optional[Path] = None, *, opener=None,
                 interval_seconds: float = CHECK_INTERVAL_SECONDS, enabled=None):
        self.cache_path = Path(cache_path) if cache_path else None
        self._opener = opener
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._releases: dict[str, dict] = {}
        self._stars: dict[str, dict] = {}          # addon id -> {"stars", "at"}
        self._checked_at: Optional[float] = None
        self._checking = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Whether to ask GitHub at all: the Add-ons tab is a beta feature,
        # and with it off nothing here goes to the network.
        self._enabled = enabled
        self._load()

    # -- any thread --------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {"releases": {k: dict(v) for k, v in self._releases.items()},
                    "stars": {k: v["stars"] for k, v in self._stars.items()},
                    "checked_at": self._checked_at, "checking": self._checking}

    def latest(self, addon_id: str) -> Optional[dict]:
        with self._lock:
            found = self._releases.get(addon_id)
            return dict(found) if found else None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="switchagent-addon-releases", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    # -- its own thread ----------------------------------------------------

    def enabled(self) -> bool:
        if self._enabled is not None:
            return bool(self._enabled())
        from .. import preferences

        return preferences.beta_enabled()

    def check_soon(self) -> None:
        """Check now, on a thread of its own (the beta features were just
        turned on) -- unless a check is running or has just been made."""
        with self._lock:
            recent = self._checked_at is not None and time.time() - self._checked_at < START_RECHECK_AFTER_SECONDS
            if self._checking or recent:
                return
        threading.Thread(target=self.check_now, name="switchagent-addon-releases-now", daemon=True).start()

    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            with self._lock:
                recent = (self._checked_at is not None
                          and time.time() - self._checked_at < START_RECHECK_AFTER_SECONDS)
            if self.enabled() and not (first and recent):
                self.check_now()
            first = False
            if self._stop.wait(self.interval_seconds):
                return

    def check_now(self) -> dict:
        from .. import emuiibo_download

        with self._lock:
            self._checking = True
        kwargs = {"opener": self._opener} if self._opener is not None else {}
        found: dict[str, dict] = {}
        try:
            catalog = [a for a in addons.load_catalog() if a.installable]
        except (addons.CatalogError, OSError, ValueError) as exc:
            log.warning("add-on releases not checked: the catalog does not load: %s", exc)
            catalog = []
        for addon in catalog:
            if self._stop.is_set():
                break
            try:
                if addon.install == "emuiibo":
                    release = emuiibo_download.latest_release(**kwargs)
                    found[addon.id] = {"version": release.version, "page_url": release.page_url}
                else:
                    release = github_release.latest_release(addon.spec.repo, addon.spec.asset,
                                                            max_bytes=addons.MAX_ASSET_BYTES, what=addon.name,
                                                            **kwargs)
                    found[addon.id] = {"version": release.version, "page_url": release.page_url}
            except github_release.DownloadError as exc:
                log.info("latest %s release not known: %s", addon.name, exc)
                if _rate_limited(exc):
                    break
            except Exception:  # noqa: BLE001 -- a check must never take the app down
                log.exception("checking %s's latest release failed unexpectedly", addon.name)
        stars = self._check_stars(catalog, kwargs)
        with self._lock:
            self._stars.update(stars)
            self._releases.update(found)
            if found:
                self._checked_at = time.time()
            self._checking = False
        self._save()
        log.info("add-on releases checked: %s", ", ".join(f"{k} {v['version']}" for k, v in found.items()) or "none")
        return self.snapshot()

    def _check_stars(self, catalog, kwargs) -> dict[str, dict]:
        from .. import emuiibo_download

        now = time.time()
        found: dict[str, dict] = {}
        for addon in catalog:
            if self._stop.is_set():
                break
            with self._lock:
                known = self._stars.get(addon.id)
            if known and now - known.get("at", 0) < STARS_INTERVAL_SECONDS:
                continue
            repo = emuiibo_download.REPO if addon.install == "emuiibo" else addon.spec.repo
            try:
                found[addon.id] = {"stars": github_release.repo_stars(repo, **kwargs), "at": now}
            except github_release.DownloadError as exc:
                log.info("%s's stars not known: %s", addon.name, exc)
                if _rate_limited(exc):
                    break
            except Exception:  # noqa: BLE001 -- a check must never take the app down
                log.exception("checking %s's stars failed unexpectedly", addon.name)
        return found

    def _load(self) -> None:
        import json

        if self.cache_path is None or not self.cache_path.is_file():
            return
        try:
            doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._releases = {str(k): {"version": str(v["version"]), "page_url": v.get("page_url")}
                              for k, v in (doc.get("releases") or {}).items() if v.get("version")}
            self._checked_at = doc.get("checked_at")
            self._stars = {str(k): {"stars": int(v["stars"]), "at": float(v.get("at") or 0)}
                           for k, v in (doc.get("stars") or {}).items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            log.warning("add-on releases cache unreadable, ignored: %s", self.cache_path)

    def _save(self) -> None:
        import json

        if self.cache_path is None:
            return
        try:
            with self._lock:
                doc = {"releases": self._releases, "stars": self._stars, "checked_at": self._checked_at}
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            log.warning("add-on releases cache not written: %s", self.cache_path, exc_info=True)
