"""Downloading emuiibo's current release from its own GitHub releases.

github_release.py does the fetching and every check that goes with it
(GitHub's hosts only, declared size, published SHA-256, `.part` until
verified). This module adds what is emuiibo's own:
  - the repository, XorTroll/emuiibo, and its asset named exactly
    `emuiibo.zip` (the release also carries emuiigen.jar, a PC app --
    never taken);
  - a numeric version tag;
  - and it has to BE an emuiibo release: emuiibo.plan_release() must find
    the sysmodule in it, or it is refused.
"""

from __future__ import annotations

import io
import re
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import emuiibo, github_release
from .github_release import ALLOWED_HOSTS, DOWNLOADS_FOLDER, DownloadError  # noqa: F401 -- re-exported

REPO = "XorTroll/emuiibo"
LATEST_RELEASE_API = github_release.latest_release_api(REPO)
ASSET_NAME = "emuiibo.zip"
# The real asset is 615 KB (1.1.3); anything past this is not emuiibo.
MAX_ASSET_BYTES = 32 * 1024 * 1024

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")

Opener = github_release.Opener


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    sha256: Optional[str]
    page_url: str
    source: Optional[github_release.Release] = None

    @property
    def file_name(self) -> str:
        return f"emuiibo-v{self.version}.zip"


def latest_release(*, opener: Opener = urllib.request.urlopen) -> Release:
    """emuiibo's newest published release, and its emuiibo.zip."""
    found = github_release.latest_release(REPO, ASSET_NAME, max_bytes=MAX_ASSET_BYTES, what="emuiibo",
                                          opener=opener)
    match = _VERSION_RE.match(found.tag)
    if not match:
        raise DownloadError(f"emuiibo's latest release has an unexpected tag: {found.tag!r}")
    return Release(version=match.group(1), url=found.url, size=found.size, sha256=found.sha256,
                   page_url=found.page_url or emuiibo.RELEASES_URL, source=found)


def download(
    release: Release, dest_dir: Path, *, opener: Opener = urllib.request.urlopen,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Downloads `release` into dest_dir and returns the verified file. A
    file already there with the published checksum is kept, not fetched
    again."""
    source = release.source or github_release.Release(
        repo=REPO, tag=release.version, asset_name=ASSET_NAME, url=release.url, size=release.size,
        sha256=release.sha256, page_url=release.page_url,
    )
    return github_release.download(source, dest_dir, release.file_name, max_bytes=MAX_ASSET_BYTES,
                                   check=_check_is_emuiibo, opener=opener, progress=progress)


def _check_is_emuiibo(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            files = [(i.filename.replace("\\", "/"), i.file_size) for i in zf.infolist() if not i.is_dir()]
    except zipfile.BadZipFile as exc:
        raise DownloadError("the download is not a zip") from exc
    plan = emuiibo.plan_release(files)
    if plan is None or not plan.files:
        raise DownloadError("the download is not an emuiibo release (no emuiibo sysmodule in it)")
