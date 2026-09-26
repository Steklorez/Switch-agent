"""Frozen content snapshot for a job, plus its delivery progress.

Fixes the TOCTOU gap between "user confirms a job" and "worker actually
sends it" (Stage 4.1): a job's target_device_id was already pinned at
creation time (Stage 4) and never substituted -- WHAT content it sends is
now pinned the same way, as a Manifest built once from the exact
PreviewReport the user was shown, and never rebuilt from a fresh scan.

Stored as small JSON files under work/job-<job_id>/ -- not in SQLite (no
large blobs in the DB) and not just kept in memory (must survive a process
restart, since a job can sit CONFIRMED/DEVICE_UNAVAILABLE for an arbitrary
time before the worker gets to it):

  manifest.json  -- frozen at job creation, never modified afterwards.
  progress.json  -- mutable, updated after each file this job's OWN
                     send_file call durably confirmed delivered.

progress.json, not re-querying the device, is what makes retry idempotent:
a file already recorded there is skipped without needing to prove anything
about its remote content. This is deliberate -- the MtpBackend interface
was NOT extended with a "hash the remote file" method (real MTP can't do
that cheaply, see docs/STAGE4.1.md), so "is this existing destination file
the same content" is answered from OUR OWN record of having put it there,
never by asking the device to prove it.

Two additions (2026-09-23), both still proof rather than trust:
  - a retry inherits its predecessor's progress.json (inherit_progress) --
    the same record, carried along the retry chain;
  - on a real filesystem (SD_CARD), a file already at the destination is
    read back and hashed by the backend; if it holds exactly the manifest's
    bytes, it counts as delivered (TransferResult.already_present). That is
    the bytes themselves, hashed here -- not the device's word for it -- and
    only for files small enough to be worth reading
    (mtp/windows.READ_BACK_MAX_BYTES). Anything else is still a conflict.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config, sd_files
from .model import ContentType
from .preview import PreviewReport
from .scanner import sha256_file


class ManifestError(Exception):
    """Raised when a PreviewReport doesn't have locally-staged content to
    build a manifest from (mirrors transfer.py's "not staged" checks, just
    enforced earlier, at job-creation time instead of send time)."""


@dataclass(frozen=True)
class ManifestFile:
    dest_relative_path: str    # where it goes under the storage's target root
    source_kind: str           # "inbox" | "library" | "frozen" -- see resolve_source_path
    source_relative_path: str  # meaning depends on source_kind
    size: int
    sha256: str
    source_root: Optional[str] = None
    # os.stat().st_mtime_ns of the source at the moment it was hashed. Used
    # by verify_manifest_against_source() as a cheap drift detector; None on
    # manifests frozen before this field existed, which simply re-hash.
    mtime_ns: Optional[int] = None


@dataclass(frozen=True)
class Manifest:
    content_type: str
    title_id: Optional[str]
    target_storage: str
    files: tuple[ManifestFile, ...]
    # Set when this job was created as part of a Web UI batch (see
    # web/services.create_and_confirm_jobs) -- a "frozen" file then lives
    # under batch_work_dir(batch_id), SHARED by every job of that batch,
    # instead of duplicated per-job under job_work_dir(job_id). None for
    # jobs created outside a batch (CLI/inbox pipeline) -- those keep the
    # original per-job frozen location, unchanged.
    batch_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "content_type": self.content_type,
            "title_id": self.title_id,
            "target_storage": self.target_storage,
            "batch_id": self.batch_id,
            "files": [
                {
                    "dest_relative_path": f.dest_relative_path,
                    "source_kind": f.source_kind,
                    "source_relative_path": f.source_relative_path,
                    "size": f.size,
                    "sha256": f.sha256,
                    "source_root": f.source_root,
                    "mtime_ns": f.mtime_ns,
                }
                for f in self.files
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            content_type=d["content_type"],
            title_id=d.get("title_id"),
            target_storage=d["target_storage"],
            batch_id=d.get("batch_id"),
            files=tuple(ManifestFile(**f) for f in d["files"]),
        )


def job_work_dir(job_id: int) -> Path:
    return config.WORK_DIR / f"job-{job_id}"


def batch_work_dir(batch_id: int) -> Path:
    """Shared staging dir for every job created from the SAME Web UI batch
    (see web/services.create_and_confirm_jobs) -- one physical copy of each
    archive-extracted package/mod per batch, never duplicated per job. Only
    ever removed once every job of the batch has reached a fully-successful
    terminal state (see work_cleanup.cleanup_batch_if_all_done)."""
    return config.WORK_DIR / f"batch-{batch_id}"


def manifest_path_for(job_id: int) -> Path:
    return job_work_dir(job_id) / "manifest.json"


def _progress_path_for(job_id: int) -> Path:
    return job_work_dir(job_id) / "progress.json"


def build_manifest_and_stage(
    report: PreviewReport, job_id: int, *, target_storage: str, batch_id: Optional[int] = None,
) -> Manifest:
    """Builds the frozen Manifest for `report` and, for archive-sourced
    content, copies the already-extracted bytes so they survive
    independently of the ephemeral preview extraction dir report.work_dir
    points at (that dir has no lifetime guarantee beyond the preview_path()
    call that created it). Raises ManifestError if the report has nothing
    locally stageable yet (e.g. an un-extracted archive) -- the same
    precondition transfer.py already enforces, just checked once, up
    front, instead of at send time.

    When `batch_id` is given (every Web UI confirm-install call -- see
    web/services.create_and_confirm_jobs), the frozen payload is staged
    under batch_work_dir(batch_id) instead of job_work_dir(job_id): a
    multi-package archive (Base+Update+DLC) fans out into several jobs
    that each need their OWN file, but nothing needs a second physical copy
    per job -- job_work_dir(job_id) then holds only manifest.json/
    progress.json, keeping it small. batch_id=None (jobs created outside a
    batch: CLI/inbox) keeps the original per-job frozen location,
    unchanged."""
    work_dir = job_work_dir(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    payload_dir = batch_work_dir(batch_id) if batch_id is not None else work_dir
    payload_dir.mkdir(parents=True, exist_ok=True)

    if report.content_type is ContentType.GAME_PACKAGE:
        files = _build_package_files(report, payload_dir)
    elif report.content_type is ContentType.ATMOSPHERE_MOD:
        files = _build_mod_files(report, payload_dir)
    elif report.content_type is ContentType.SD_FILES:
        files = _build_sd_files(report, payload_dir)
    else:
        raise ManifestError(f"content type {report.content_type.value} is not eligible for transfer")

    manifest = Manifest(
        content_type=report.content_type.value, title_id=report.title_id,
        target_storage=target_storage, batch_id=batch_id, files=tuple(files),
    )
    manifest_path_for(job_id).write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


def _classify_bare_source_root(source_path: Path) -> tuple[str, Path]:
    """A bare (non-archived) source file/folder is always somewhere under
    one of exactly two scanned roots: config.INBOX_DIR (Stage 2's
    drop-a-file workflow) or config.LIBRARY_DIR (Web UI's index of an
    arbitrary configured directory, see docs/WEB-UI.md). Returns
    (source_kind, root) so callers can both tag the ManifestFile correctly
    and compute source_relative_path against the right root.
    resolve_source_path() below must stay in sync with this."""
    for kind, root in [("inbox", config.INBOX_DIR), *(("library", p) for p in config.library_dirs())]:
        try:
            source_path.relative_to(root)
            return kind, root
        except ValueError:
            continue
    raise ManifestError(
        f"source is not under a known scanned root (inbox or library): {source_path}"
    )


def _build_package_files(report: PreviewReport, payload_dir: Path) -> list[ManifestFile]:
    if report.package_relative_path is None:
        raise ManifestError("no package file resolved on this report")
    source_path = Path(report.source)

    if source_path.suffix.lower() in config.PACKAGE_EXTENSIONS:
        # Bare file directly under a scanned root -- never copied; verified
        # in place, freshly, right before send (see
        # verify_manifest_against_source).
        if not source_path.is_file():
            raise ManifestError(f"expected package file not found on disk: {source_path}")
        source_kind, root = _classify_bare_source_root(source_path)
        rel = source_path.relative_to(root).as_posix()
        stat = source_path.stat()
        return [ManifestFile(
            dest_relative_path=source_path.name, source_kind=source_kind, source_relative_path=rel,
            size=stat.st_size, sha256=sha256_file(source_path),
            source_root=str(root) if source_kind == "library" else None,
            mtime_ns=stat.st_mtime_ns,
        )]

    if report.work_dir is None:
        raise ManifestError(
            "archive source not extracted yet -- call preview_path(path, extract=True) first"
        )
    extracted = report.work_dir / report.package_relative_path
    if not extracted.is_file():
        raise ManifestError(f"expected extracted package file not found: {extracted}")
    # Preserve the common filename layout, but never overwrite another
    # archive's payload with the same basename in this batch.
    relative = Path(extracted.name)
    if (payload_dir / relative).exists():
        import uuid
        relative = Path(uuid.uuid4().hex) / extracted.name
        (payload_dir / relative).parent.mkdir(parents=True, exist_ok=False)
    frozen = payload_dir / relative
    # move, not copy: `extracted` lives under the ephemeral preview
    # extraction dir (report.work_dir), never read again once frozen here --
    # copying would leave the SAME bytes on disk twice for no reason.
    shutil.move(str(extracted), str(frozen))
    frozen_stat = frozen.stat()
    return [ManifestFile(
        dest_relative_path=extracted.name, source_kind="frozen", source_relative_path=relative.as_posix(),
        size=frozen_stat.st_size, sha256=sha256_file(frozen), mtime_ns=frozen_stat.st_mtime_ns,
    )]


def _build_mod_files(report: PreviewReport, payload_dir: Path) -> list[ManifestFile]:
    if report.mod_source_dir is None:
        raise ManifestError(
            "mod content not staged locally yet -- call preview_path(path, extract=True) first"
        )
    if report.title_id is None:
        raise ManifestError("no TITLE_ID -- cannot determine destination path")
    if not report.mod_source_dir.is_dir():
        raise ManifestError(f"mod source directory does not exist: {report.mod_source_dir}")

    base = f"atmosphere/contents/{report.title_id}"

    bare_root: Optional[Path] = None
    if report.work_dir is None:
        # Bare mod-folder directly under a scanned root -- never copied;
        # each file is re-verified in place right before send.
        walk_root = report.mod_source_dir
        source_kind, bare_root = _classify_bare_source_root(walk_root)
    else:
        # Archive-sourced: freeze the mod tree permanently -- report.
        # mod_source_dir lives under the ephemeral preview extraction, not
        # guaranteed to survive until the worker actually gets to this job.
        # Moved, not copied (see _build_package_files' own move for why).
        frozen_root = payload_dir / "mod"
        if frozen_root.exists():
            import uuid
            frozen_root = payload_dir / ("mod-" + uuid.uuid4().hex)
        shutil.move(str(report.mod_source_dir), str(frozen_root))
        walk_root = frozen_root
        source_kind = "frozen"

    files = []
    for f in sorted((p for p in walk_root.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
        rel = f.relative_to(walk_root).as_posix()
        source_rel = f.relative_to(payload_dir).as_posix() if source_kind == "frozen" else f.relative_to(bare_root).as_posix()
        file_stat = f.stat()
        files.append(ManifestFile(
            dest_relative_path=f"{base}/{rel}", source_kind=source_kind, source_relative_path=source_rel,
            size=file_stat.st_size, sha256=sha256_file(f),
            source_root=str(bare_root) if source_kind == "library" else None,
            mtime_ns=file_stat.st_mtime_ns,
        ))
    if not files:
        raise ManifestError(f"mod source has no files: {report.mod_source_dir}")
    return files


def _build_sd_files(report: PreviewReport, payload_dir: Path) -> list[ManifestFile]:
    """Every file of a switch/ folder, each bound for the same path under
    switch/ on the SD card (sd_files.destination_for) -- nowhere else, so an
    SD_FILES job can never write outside the homebrew folder whatever its
    source looked like. Frozen and verified exactly the way a mod is: a bare
    folder under a scanned root is hashed in place and re-verified before
    sending; an archive's extracted folder is moved into the payload dir."""
    if report.sd_source_dir is None:
        raise ManifestError(
            "switch/ folder not staged locally yet -- call preview_path(path, extract=True) first"
        )
    if not report.sd_source_dir.exists():
        raise ManifestError(f"switch/ folder does not exist: {report.sd_source_dir}")

    bare_root: Optional[Path] = None
    if report.work_dir is None:
        # A switch/ folder, or a lone .nro (sd_files.source_files).
        walk_root = report.sd_source_dir
        source_kind, bare_root = _classify_bare_source_root(walk_root)
    else:
        frozen_root = payload_dir / "sd"
        if frozen_root.exists():
            import uuid
            frozen_root = payload_dir / ("sd-" + uuid.uuid4().hex)
        shutil.move(str(report.sd_source_dir), str(frozen_root))
        walk_root = frozen_root
        source_kind = "frozen"

    files = []
    for f, rel in sd_files.source_files(walk_root):
        source_rel = f.relative_to(payload_dir).as_posix() if source_kind == "frozen" else f.relative_to(bare_root).as_posix()
        file_stat = f.stat()
        files.append(ManifestFile(
            dest_relative_path=sd_files.destination_for(rel), source_kind=source_kind,
            source_relative_path=source_rel, size=file_stat.st_size, sha256=sha256_file(f),
            source_root=str(bare_root) if source_kind == "library" else None,
            mtime_ns=file_stat.st_mtime_ns,
        ))
    if not files:
        raise ManifestError(f"switch/ folder has no files: {report.sd_source_dir}")
    return files


def load_manifest(job_id: int) -> Manifest:
    return Manifest.from_dict(json.loads(manifest_path_for(job_id).read_text(encoding="utf-8")))


def resolve_source_path(file: ManifestFile, job_id: int, *, batch_id: Optional[int] = None) -> Path:
    """Resolves a manifest file's declared source to an absolute path,
    strictly scoped to the root its source_kind implies. A manifest is
    normally built only by build_manifest_and_stage() from paths already
    confirmed to live under a known root (see _classify_bare_source_root),
    so in ordinary operation this can never reject anything -- these checks
    exist to fail closed on genuinely corrupted/malformed manifest data
    (FAULT-001: a hand-edited or otherwise damaged manifest.json declaring
    an unrecognized source_kind, an absolute path, or a relative path that
    would resolve outside its intended root), rather than silently
    resolving to -- and letting a caller read from or send -- a path
    outside the intended tree. Raises ManifestError, never guesses.

    `batch_id` should be the manifest's OWN Manifest.batch_id -- callers
    that already have the loaded Manifest object pass it straight through
    (see verify_manifest_against_source below); None reproduces the
    original per-job frozen location."""
    if file.source_kind == "frozen":
        root = batch_work_dir(batch_id) if batch_id is not None else job_work_dir(job_id)
    elif file.source_kind == "library":
        root = Path(file.source_root) if file.source_root else config.LIBRARY_DIR
        if root.resolve() not in {p.resolve() for p in config.library_dirs()}:
            raise ManifestError("source library folder is no longer configured")
    elif file.source_kind == "inbox":
        root = config.INBOX_DIR
    else:
        raise ManifestError(f"unknown source_kind in manifest: {file.source_kind!r}")

    try:
        root_resolved = root.resolve()
        candidate = (root_resolved / file.source_relative_path).resolve()
    except (OSError, ValueError) as exc:
        raise ManifestError(
            f"manifest file has an unresolvable relative path {file.source_relative_path!r}: {exc}"
        ) from exc
    if not candidate.is_relative_to(root_resolved):
        raise ManifestError(
            f"manifest file has an invalid relative path that would escape its "
            f"source root: {file.source_relative_path!r}"
        )
    return candidate


@dataclass
class ManifestMismatch:
    dest_relative_path: str
    kind: str  # "missing" | "changed"


def verify_manifest_against_source(manifest: Manifest, job_id: int) -> Optional[ManifestMismatch]:
    """Re-checks every file the manifest expects against what is actually
    there right now, before any device is touched. This is the TOCTOU guard:
    content that drifted since job creation is refused, never silently
    substituted.

    Drift is detected by (size, mtime_ns) -- the pair the filesystem itself
    updates on any write -- and only a file whose stamp does NOT match what
    was recorded when it was hashed gets a full SHA-256 recomputation. The
    hash in the manifest is still the one this job pinned; nothing here
    trusts a hash it did not compute itself.

    Why this matters (measured 2026-09-18 on the real library): the source
    was ALREADY hashed minutes earlier, by build_manifest_and_stage(), and
    re-hashing it here put a second full read of the same bytes directly on
    the wall-clock path between "user clicked Install" and "first byte
    leaves the PC". At the 265 MB/s this machine's library drive sustains
    that is 2 x 53s for a 13.8 GB game -- the jobs table shows exactly that
    shape, created->started of 21s for 2.49 GB, 13s for 1.42 GB, 4s for
    0.29 GB, i.e. twice the hash plus the worker's poll interval, every
    single install.

    A file rewritten in place with byte-identical size AND an unchanged
    mtime would now pass without a hash. That requires deliberately
    restoring the timestamp after modifying the file; ordinary editing,
    re-downloading, copying or extracting all move mtime forward. Set
    SWITCHAGENT_ALWAYS_REHASH=1 to force the full hash for every file
    regardless -- the pre-2026-09-18 behaviour, kept for anyone who wants
    that trade the other way round."""
    always_rehash = os.environ.get("SWITCHAGENT_ALWAYS_REHASH") == "1"
    for file in manifest.files:
        path = resolve_source_path(file, job_id, batch_id=manifest.batch_id)
        if not path.is_file():
            return ManifestMismatch(file.dest_relative_path, "missing")
        stat = path.stat()
        if stat.st_size != file.size:
            return ManifestMismatch(file.dest_relative_path, "changed")
        unchanged_stamp = file.mtime_ns is not None and stat.st_mtime_ns == file.mtime_ns
        if unchanged_stamp and not always_rehash:
            continue
        if sha256_file(path) != file.sha256:
            return ManifestMismatch(file.dest_relative_path, "changed")
    return None


def _read_progress(job_id: int) -> dict:
    path = _progress_path_for(job_id)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_progress(job_id: int, data: dict) -> None:
    job_work_dir(job_id).mkdir(parents=True, exist_ok=True)
    _progress_path_for(job_id).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_progress(job_id: int) -> set[str]:
    return set(_read_progress(job_id).get("delivered", []))


def load_replaceable(job_id: int) -> set[str]:
    """Destination files this job may overwrite although it did not deliver
    them itself: the one a previous attempt of the SAME transfer was writing
    when it stopped (see inherit_progress). Anything else already on the
    device is still somebody else's, and still a conflict."""
    return set(_read_progress(job_id).get("replace", []))


def mark_replaceable(job_id: int, dest_relative_path: str) -> None:
    """The file this job was writing when its connection dropped: it may
    be half written, and it is the only one its own resumed attempt may
    replace (same rule as inherit_progress's `in_flight`)."""
    data = _read_progress(job_id)
    replace = set(data.get("replace", []))
    if dest_relative_path in set(data.get("delivered", [])):
        return
    replace.add(dest_relative_path)
    data["replace"] = sorted(replace)
    _write_progress(job_id, data)


def mark_delivered(job_id: int, dest_relative_path: str) -> None:
    data = _read_progress(job_id)
    delivered = set(data.get("delivered", []))
    delivered.add(dest_relative_path)
    data["delivered"] = sorted(delivered)
    _write_progress(job_id, data)


def inherit_progress(old_job_id: int, new_job_id: int, *, in_flight: Optional[str] = None) -> int:
    """A retry continues its transfer instead of starting it again.

    Before this, Retry created a job that knew nothing of what the attempt
    it replaces had already delivered -- and every such file then looked
    like somebody else's to it. Real case (2026-09-23): Mega Man X
    Regenesis' 4,625-file switch/ folder stopped after 613 files when the
    console stalled; its retry would have met file #1 already on the SD card
    and, under the default "skip" policy, stopped there, every time.

    What carries over is exactly what the previous attempt recorded in its
    own progress.json -- still this project's "our own record of having put
    it there" rule, never "the device says a file exists" -- and only where
    both attempts' manifests name the same file with the same SHA-256.
    `in_flight` is the file the previous attempt was sending when it
    stopped (jobs.current_file); that one may be half written, and is the
    only file this retry is allowed to replace. Returns how many delivered
    files were carried over."""
    old = load_manifest(old_job_id)
    new = load_manifest(new_job_id)
    old_hashes = {f.dest_relative_path: f.sha256 for f in old.files}
    new_hashes = {f.dest_relative_path: f.sha256 for f in new.files}
    old_progress = _read_progress(old_job_id)
    delivered = {
        path for path in old_progress.get("delivered", [])
        if path in new_hashes and old_hashes.get(path) == new_hashes[path]
    }
    replace = {path for path in old_progress.get("replace", []) if path in new_hashes}
    if in_flight and in_flight in new_hashes:
        replace.add(in_flight)
    replace -= delivered
    if delivered or replace:
        _write_progress(new_job_id, {"delivered": sorted(delivered), "replace": sorted(replace)})
    return len(delivered)
