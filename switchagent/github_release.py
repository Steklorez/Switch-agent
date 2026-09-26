"""Downloading one file of a project's latest GitHub release.

What SwitchAgent downloads to put on a Switch -- emuiibo, and the Add-ons
catalog's utilities -- and only on the user's explicit click. Nothing here
runs anything it fetches: the result is a file in a Library folder, which
then goes through exactly the same path as one the user put there by hand
-- classified by structure, frozen into a manifest, copied by the queue.

What is fetched, and what is refused:
  - the latest release of one named repository, from GitHub's public API --
    a plain GET with a User-Agent and nothing else (update_check.py's
    privacy rule, kept); drafts and pre-releases never;
  - its one asset matching the name the caller asked for (a release often
    carries more: a PC tool, a debug build -- never taken);
  - from GitHub's own hosts only, redirects included;
  - no bigger than the caller's limit, and exactly the size the API declared;
  - with the SHA-256 GitHub publishes for the asset, when it publishes one --
    a mismatch is refused, never "probably fine";
  - and it has to be what the caller expects (`check`, run on the bytes
    before the file is kept).

The file is written as `<name>.part` next to its final place and renamed
only once all of that holds, so a Library scan can never pick up half a
download.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

API_ROOT = "https://api.github.com/repos"
# Where GitHub serves release assets from, after its redirects.
ALLOWED_HOSTS = frozenset({
    "api.github.com", "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
USER_AGENT = "SwitchAgent-download"
TIMEOUT_SECONDS = 30.0
# Where downloads land inside the first Library folder.
DOWNLOADS_FOLDER = "SwitchAgent downloads"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

Opener = Callable[..., object]


class DownloadError(Exception):
    """Something about the download is not right -- the message says what,
    in words fit for the page."""


@dataclass(frozen=True)
class Release:
    repo: str
    tag: str
    asset_name: str
    url: str
    size: int
    sha256: Optional[str]
    page_url: str

    @property
    def version(self) -> str:
        """The tag as a person reads it: "v2.5.3" -> "2.5.3"."""
        return self.tag[1:] if re.match(r"^[vV]\d", self.tag) else self.tag


def latest_release_api(repo: str) -> str:
    if not _REPO_RE.match(repo):
        raise DownloadError(f"not a GitHub repository name: {repo!r}")
    return f"{API_ROOT}/{repo}/releases/latest"


def check_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise DownloadError(f"refusing to download from {parsed.hostname or url!r} -- not GitHub")


def _open(url: str, opener: Opener):
    check_host(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        response = opener(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"GitHub answered {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise DownloadError(f"could not reach GitHub: {exc}") from exc
    final = getattr(response, "geturl", lambda: url)()
    check_host(final)  # a redirect must stay on GitHub too
    return response


def latest_release(
    repo: str, asset: str, *, max_bytes: int, what: Optional[str] = None,
    opener: Opener = urllib.request.urlopen,
) -> Release:
    """`repo`'s newest published release and its asset named `asset` (an
    exact name, or a pattern like "Fizeau-*.zip" that must match exactly
    one). `what` names the project in messages."""
    what = what or repo.split("/")[1]
    with _open(latest_release_api(repo), opener) as response:
        try:
            doc = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DownloadError(f"GitHub's answer about {what}'s releases could not be read") from exc
    if doc.get("draft") or doc.get("prerelease"):
        raise DownloadError(f"{what}'s latest release on GitHub is a draft or a pre-release")
    tag = str(doc.get("tag_name") or "").strip()
    if not tag:
        raise DownloadError(f"{what}'s latest release has no tag")
    matching = [a for a in doc.get("assets") or [] if fnmatch.fnmatchcase(str(a.get("name") or ""), asset)]
    if not matching:
        raise DownloadError(f"{what}'s latest release has no {asset}")
    if len(matching) > 1:
        raise DownloadError(f"{what}'s latest release has more than one {asset}")
    found = matching[0]
    size = found.get("size")
    if not isinstance(size, int) or not 0 < size <= max_bytes:
        raise DownloadError(f"{found['name']} has an unexpected size: {size!r}")
    digest = found.get("digest") or ""
    sha256 = digest[len("sha256:"):].lower() if digest.lower().startswith("sha256:") else None
    if sha256 is not None and not _SHA256_RE.fullmatch(sha256):
        raise DownloadError(f"GitHub's checksum for {found['name']} is malformed")
    url = str(found.get("browser_download_url") or "")
    check_host(url)
    return Release(repo=repo, tag=tag, asset_name=str(found["name"]), url=url, size=size, sha256=sha256,
                   page_url=str(doc.get("html_url") or f"https://github.com/{repo}/releases/latest"))


def repo_stars(repo: str, *, opener: Opener = urllib.request.urlopen) -> int:
    """How many people starred `repo` on GitHub -- how the Add-ons tab
    sorts by popularity. The same plain GET, nothing about the user in it."""
    if not _REPO_RE.match(repo):
        raise DownloadError(f"not a GitHub repository name: {repo!r}")
    with _open(f"{API_ROOT}/{repo}", opener) as response:
        try:
            doc = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DownloadError(f"GitHub's answer about {repo} could not be read") from exc
    stars = doc.get("stargazers_count")
    if not isinstance(stars, int) or stars < 0:
        raise DownloadError(f"GitHub gave no star count for {repo}")
    return stars


def download(
    release: Release, dest_dir: Path, file_name: str, *, max_bytes: int,
    check: Callable[[bytes], None], opener: Opener = urllib.request.urlopen,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Downloads `release` into dest_dir/file_name and returns the verified
    file. `check` raises DownloadError when the bytes are not what was
    expected. A file already there with the published checksum is kept,
    not fetched again."""
    if "/" in file_name or "\\" in file_name or file_name in ("", ".", ".."):
        raise DownloadError(f"refusing an unsafe file name: {file_name!r}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / file_name
    if final.is_file() and release.sha256 and _sha256(final.read_bytes()) == release.sha256:
        return final
    part = dest_dir / (file_name + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with _open(release.url, opener) as response, part.open("wb") as out:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size or received > max_bytes:
                    raise DownloadError("the download is larger than GitHub said it would be")
                digest.update(chunk)
                out.write(chunk)
                if progress is not None:
                    progress(received, release.size)
        if received != release.size:
            raise DownloadError(f"the download stopped at {received} of {release.size} bytes")
        if release.sha256 is not None and digest.hexdigest() != release.sha256:
            raise DownloadError("the download does not match the checksum GitHub published for it")
        check(part.read_bytes())
        part.replace(final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return final


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
