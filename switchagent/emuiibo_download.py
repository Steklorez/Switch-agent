"""Downloading emuiibo's current release from its own GitHub releases.

The one thing SwitchAgent downloads to put on a Switch, and only on the
user's explicit click (the Amiibo page). Nothing here runs anything it
fetches: the result is a zip in a Library folder, which then goes through
exactly the same path as one the user put there by hand -- classified by
structure, frozen into a manifest, copied by the queue.

What is fetched, and what is refused:
  - the latest release of XorTroll/emuiibo, from GitHub's public API --
    a plain GET with a User-Agent and nothing else (update_check.py's
    privacy rule, kept);
  - its asset named exactly `emuiibo.zip` (the release also carries
    emuiigen.jar, a PC app -- never taken);
  - from GitHub's own hosts only, redirects included;
  - no bigger than MAX_ASSET_BYTES, and exactly the size the API declared;
  - with the SHA-256 GitHub publishes for the asset, when it publishes one
    (it does for 1.1.3) -- a mismatch is refused, never "probably fine";
  - and it has to BE an emuiibo release: emuiibo.plan_release() must find
    the sysmodule in it, or it is refused.

The file is written as `<name>.part` next to its final place and renamed
only once all of that holds, so a Library scan can never pick up half a
download.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from . import emuiibo

LATEST_RELEASE_API = "https://api.github.com/repos/XorTroll/emuiibo/releases/latest"
ASSET_NAME = "emuiibo.zip"
# The real asset is 615 KB (1.1.3); anything past this is not emuiibo.
MAX_ASSET_BYTES = 32 * 1024 * 1024
# Where GitHub serves release assets from, after its redirects.
ALLOWED_HOSTS = frozenset({
    "api.github.com", "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
USER_AGENT = "SwitchAgent-emuiibo-download"
TIMEOUT_SECONDS = 30.0
# Where downloads land inside the first Library folder.
DOWNLOADS_FOLDER = "SwitchAgent downloads"

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")

Opener = Callable[..., object]


class DownloadError(Exception):
    """Something about the download is not right -- the message says what,
    in words fit for the page."""


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    sha256: Optional[str]
    page_url: str

    @property
    def file_name(self) -> str:
        return f"emuiibo-v{self.version}.zip"


def _check_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise DownloadError(f"refusing to download from {parsed.hostname or url!r} -- not GitHub")


def _open(url: str, opener: Opener):
    _check_host(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        response = opener(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"GitHub answered {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise DownloadError(f"could not reach GitHub: {exc}") from exc
    final = getattr(response, "geturl", lambda: url)()
    _check_host(final)  # a redirect must stay on GitHub too
    return response


def latest_release(*, opener: Opener = urllib.request.urlopen) -> Release:
    """emuiibo's newest published release, and its emuiibo.zip."""
    with _open(LATEST_RELEASE_API, opener) as response:
        try:
            doc = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DownloadError("GitHub's answer about emuiibo's releases could not be read") from exc
    if doc.get("draft") or doc.get("prerelease"):
        raise DownloadError("emuiibo's latest release on GitHub is a draft or a pre-release")
    match = _VERSION_RE.match(str(doc.get("tag_name") or "").strip())
    if not match:
        raise DownloadError(f"emuiibo's latest release has an unexpected tag: {doc.get('tag_name')!r}")
    asset = next((a for a in doc.get("assets") or [] if a.get("name") == ASSET_NAME), None)
    if asset is None:
        raise DownloadError(f"emuiibo's latest release has no {ASSET_NAME}")
    size = asset.get("size")
    if not isinstance(size, int) or not 0 < size <= MAX_ASSET_BYTES:
        raise DownloadError(f"{ASSET_NAME} has an unexpected size: {size!r}")
    digest = asset.get("digest") or ""
    sha256 = digest[len("sha256:"):].lower() if digest.lower().startswith("sha256:") else None
    if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise DownloadError("GitHub's checksum for emuiibo.zip is malformed")
    url = str(asset.get("browser_download_url") or "")
    _check_host(url)
    return Release(version=match.group(1), url=url, size=size, sha256=sha256,
                   page_url=str(doc.get("html_url") or emuiibo.RELEASES_URL))


def download(
    release: Release, dest_dir: Path, *, opener: Opener = urllib.request.urlopen,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Downloads `release` into dest_dir and returns the verified file. A
    file already there with the published checksum is kept, not fetched
    again."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / release.file_name
    if final.is_file() and release.sha256 and _sha256(final.read_bytes()) == release.sha256:
        return final
    part = dest_dir / (release.file_name + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with _open(release.url, opener) as response, part.open("wb") as out:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size or received > MAX_ASSET_BYTES:
                    raise DownloadError("the download is larger than GitHub said it would be")
                digest.update(chunk)
                out.write(chunk)
                if progress is not None:
                    progress(received, release.size)
        if received != release.size:
            raise DownloadError(f"the download stopped at {received} of {release.size} bytes")
        if release.sha256 is not None and digest.hexdigest() != release.sha256:
            raise DownloadError("the download does not match the checksum GitHub published for it")
        _check_is_emuiibo(part.read_bytes())
        part.replace(final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return final


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_is_emuiibo(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            files = [(i.filename.replace("\\", "/"), i.file_size) for i in zf.infolist() if not i.is_dir()]
    except zipfile.BadZipFile as exc:
        raise DownloadError("the download is not a zip") from exc
    plan = emuiibo.plan_release(files)
    if plan is None or not plan.files:
        raise DownloadError("the download is not an emuiibo release (no emuiibo sysmodule in it)")
