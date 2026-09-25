"""Wires the existing pipeline (scanner/extractor/preview) to an abstract
MtpBackend -- the final step of INBOX -> SCAN -> CLASSIFY -> EXTRACT ->
VALIDATE -> PREVIEW -> MTP TRANSFER.

This stage's only real MtpBackend is MockMtpBackend (switchagent/mtp/mock.py)
-- nothing in this module is mock-specific, though. A future RealMtpBackend
plugs in unchanged, because everything here talks only to the MtpBackend
ABC (switchagent/mtp/base.py), never to a concrete class.

Deliberately NOT this module's job:
  - Extracting archives. transfer_report() requires a PreviewReport that
    already has its content staged locally (preview_path(path,
    extract=True) for archive sources, or a bare inbox file/mod-folder that
    never needed extraction). If it isn't staged, this raises a clear error
    instead of silently extracting as a side effect -- keeps "what touched
    the filesystem and when" easy to reason about.
  - Deciding whether to overwrite a CONFLICT. Conflict detection already
    happened at the preview stage (extractor.compute_conflicts_*); this
    module just passes `overwrite` through to the backend as given by the
    caller. No automatic overwriting happens unless the caller explicitly
    asks for it.
  - Anything about queues, retries, or persistence -- that is the next
    stage's job (see STATE.md). This is a single, synchronous "send this
    one already-confirmed thing now" operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, sd_files
from .model import ContentType
from .mtp.base import MtpBackend, TransferResult, TransferStatus
from .mtp.errors import MtpError
from .preview import PreviewReport

STORAGE_SD_CARD = "SD_CARD"
STORAGE_SD_INSTALL = "SD_INSTALL"


@dataclass
class TransferOutcome:
    ok: bool
    storage: Optional[str] = None
    files_sent: int = 0
    files_total: int = 0
    bytes_sent: int = 0
    error: Optional[str] = None
    per_file: list[TransferResult] = field(default_factory=list)


def transfer_report(
    backend: MtpBackend, report: PreviewReport, *, overwrite: bool = False,
) -> TransferOutcome:
    """Sends whatever `report` describes through `backend`. Does not decide
    WHETHER something should be sent (that's the caller's job -- this stage
    has no automatic-install-on-scan behavior, matching every prior stage's
    "explicit confirmation only" rule); it only knows HOW, given a report
    that already says what and where."""
    if report.content_type is ContentType.GAME_PACKAGE:
        return _transfer_game_package(backend, report, overwrite=overwrite)
    if report.content_type is ContentType.ATMOSPHERE_MOD:
        return _transfer_atmosphere_mod(backend, report, overwrite=overwrite)
    if report.content_type is ContentType.SD_FILES:
        return _transfer_sd_files(backend, report, overwrite=overwrite)
    if report.content_type in (ContentType.EMUIIBO, ContentType.AMIIBO):
        return _transfer_copy_plan(backend, report, overwrite=overwrite)
    return TransferOutcome(
        ok=False,
        error=f"content type {report.content_type.value} is not eligible for transfer (NEEDS_REVIEW)",
    )


def _resolve_package_source(report: PreviewReport) -> Optional[Path]:
    """report.source is NOT always the file to send: for a bare package
    file in inbox/ it is, but for a package found inside an archive it's
    the archive itself -- the actual file only exists once extracted, under
    report.work_dir. Telling these apart by report.source's own extension
    (not by whether work_dir happens to be set) is what makes an
    un-extracted archive correctly resolve to "nothing to send" instead of
    silently resolving to the archive file itself."""
    if report.package_relative_path is None:
        return None
    source_path = Path(report.source)
    if source_path.suffix.lower() in config.PACKAGE_EXTENSIONS:
        return source_path
    if report.work_dir is not None:
        return report.work_dir / report.package_relative_path
    return None


def _transfer_game_package(
    backend: MtpBackend, report: PreviewReport, *, overwrite: bool,
) -> TransferOutcome:
    source_path = _resolve_package_source(report)
    if source_path is None:
        return TransferOutcome(ok=False, error="no package file resolved on this report")
    if not source_path.is_file():
        return TransferOutcome(
            ok=False, error=(
                f"expected package file not found on disk: {source_path} "
                "(archive source not extracted yet? call preview_path(path, extract=True))"
            ),
        )

    dest_path = Path(report.package_relative_path).name  # flat: "SD install" is an install
    # target, not a real directory tree (see docs/RESEARCH-STAGE1.md) --
    # subfolders from inside an archive are not meaningful here.
    try:
        backend.ensure_directory(STORAGE_SD_INSTALL, "")
        result = backend.send_file(STORAGE_SD_INSTALL, dest_path, source_path, overwrite=overwrite)
    except MtpError as exc:
        return TransferOutcome(ok=False, storage=STORAGE_SD_INSTALL, error=str(exc))

    ok = result.status is TransferStatus.COMPLETED
    return TransferOutcome(
        ok=ok, storage=STORAGE_SD_INSTALL,
        files_sent=1 if ok else 0, files_total=1,
        bytes_sent=result.bytes_sent, error=None if ok else (result.error or result.status.value),
        per_file=[result],
    )


def _transfer_sd_files(
    backend: MtpBackend, report: PreviewReport, *, overwrite: bool,
) -> TransferOutcome:
    """A switch/ folder, file by file, to the same paths under switch/ on
    the SD card -- the CLI counterpart of manifest._build_sd_files, with the
    same one-file-at-a-time, stop-at-the-first-failure rule as mods."""
    if report.sd_source_dir is None or not report.sd_source_dir.is_dir():
        return TransferOutcome(
            ok=False, error=(
                "switch/ folder not staged locally yet "
                "(archive source not extracted? call preview_path(path, extract=True))"
            ),
        )
    files = sorted((p for p in report.sd_source_dir.rglob("*") if p.is_file()), key=lambda p: p.as_posix())
    per_file: list[TransferResult] = []
    sent = 0
    total_bytes = 0
    for f in files:
        dest_path = sd_files.destination_for(f.relative_to(report.sd_source_dir).as_posix())
        parent = "/".join(dest_path.split("/")[:-1])
        try:
            backend.ensure_directory(STORAGE_SD_CARD, parent)
            result = backend.send_file(STORAGE_SD_CARD, dest_path, f, overwrite=overwrite)
        except MtpError as exc:
            return TransferOutcome(
                ok=False, storage=STORAGE_SD_CARD, files_sent=sent, files_total=len(files),
                bytes_sent=total_bytes, error=str(exc), per_file=per_file,
            )
        per_file.append(result)
        if result.status is not TransferStatus.COMPLETED:
            return TransferOutcome(
                ok=False, storage=STORAGE_SD_CARD, files_sent=sent, files_total=len(files),
                bytes_sent=total_bytes, error=result.error or f"transfer of '{dest_path}' did not complete",
                per_file=per_file,
            )
        sent += 1
        total_bytes += result.bytes_sent
    return TransferOutcome(
        ok=bool(files), storage=STORAGE_SD_CARD, files_sent=sent, files_total=len(files),
        bytes_sent=total_bytes, per_file=per_file, error=None if files else "switch/ folder has no files",
    )


def _transfer_copy_plan(
    backend: MtpBackend, report: PreviewReport, *, overwrite: bool,
) -> TransferOutcome:
    """EMUIIBO / AMIIBO: exactly report.copy_plan (see emuiibo.py), one file
    at a time, stopping at the first failure -- the CLI counterpart of
    manifest._build_copy_plan_files, with the same destination checks."""
    from .manifest import ManifestError, _check_copy_destination

    if report.copy_root is None or not report.copy_plan:
        return TransferOutcome(ok=False, error="nothing staged locally to copy (call preview_path(path, extract=True))")
    try:
        for _src, dest in report.copy_plan:
            _check_copy_destination(report.content_type, dest)
    except ManifestError as exc:
        return TransferOutcome(ok=False, error=str(exc))
    per_file: list[TransferResult] = []
    sent = 0
    total_bytes = 0
    for src, dest in sorted(report.copy_plan, key=lambda pair: pair[1]):
        parent = "/".join(dest.split("/")[:-1])
        try:
            backend.ensure_directory(STORAGE_SD_CARD, parent)
            result = backend.send_file(STORAGE_SD_CARD, dest, report.copy_root / src,
                                       overwrite=overwrite or report.content_type is ContentType.EMUIIBO)
        except MtpError as exc:
            return TransferOutcome(ok=False, storage=STORAGE_SD_CARD, files_sent=sent,
                                   files_total=len(report.copy_plan), bytes_sent=total_bytes,
                                   error=str(exc), per_file=per_file)
        per_file.append(result)
        if result.status is not TransferStatus.COMPLETED:
            return TransferOutcome(ok=False, storage=STORAGE_SD_CARD, files_sent=sent,
                                   files_total=len(report.copy_plan), bytes_sent=total_bytes,
                                   error=result.error or f"transfer of '{dest}' did not complete", per_file=per_file)
        sent += 1
        total_bytes += result.bytes_sent
    return TransferOutcome(ok=True, storage=STORAGE_SD_CARD, files_sent=sent, files_total=len(report.copy_plan),
                           bytes_sent=total_bytes, per_file=per_file)


def _transfer_atmosphere_mod(
    backend: MtpBackend, report: PreviewReport, *, overwrite: bool,
) -> TransferOutcome:
    if report.mod_source_dir is None:
        return TransferOutcome(
            ok=False, error=(
                "mod content not staged locally yet "
                "(archive source not extracted? call preview_path(path, extract=True))"
            ),
        )
    if report.title_id is None:
        return TransferOutcome(ok=False, error="no TITLE_ID -- cannot determine destination path")
    if not report.mod_source_dir.is_dir():
        return TransferOutcome(ok=False, error=f"mod source directory does not exist: {report.mod_source_dir}")

    files = sorted(
        (p for p in report.mod_source_dir.rglob("*") if p.is_file()),
        key=lambda p: p.as_posix(),
    )
    base = f"atmosphere/contents/{report.title_id}"
    per_file: list[TransferResult] = []
    sent = 0
    total_bytes = 0

    try:
        backend.ensure_directory(STORAGE_SD_CARD, base)
    except MtpError as exc:
        return TransferOutcome(ok=False, storage=STORAGE_SD_CARD, files_total=len(files), error=str(exc))

    # One file at a time, in order, stopping at the first failure --
    # docs/RESEARCH-STAGE1.md §2-3 found real MTP to a Switch tolerates
    # exactly one transfer at a time and has no reliable mid-batch recovery,
    # so this deliberately does not attempt the remaining files once one has
    # failed (matches the "job stops, doesn't pretend to be done" rule
    # carried through every prior stage of this project).
    for f in files:
        rel = f.relative_to(report.mod_source_dir).as_posix()
        dest_path = f"{base}/{rel}"
        parent = "/".join(dest_path.split("/")[:-1])
        try:
            backend.ensure_directory(STORAGE_SD_CARD, parent)
            result = backend.send_file(STORAGE_SD_CARD, dest_path, f, overwrite=overwrite)
        except MtpError as exc:
            return TransferOutcome(
                ok=False, storage=STORAGE_SD_CARD, files_sent=sent, files_total=len(files),
                bytes_sent=total_bytes, error=str(exc), per_file=per_file,
            )

        per_file.append(result)
        if result.status is not TransferStatus.COMPLETED:
            return TransferOutcome(
                ok=False, storage=STORAGE_SD_CARD, files_sent=sent, files_total=len(files),
                bytes_sent=total_bytes, error=result.error or f"transfer of '{rel}' did not complete",
                per_file=per_file,
            )
        sent += 1
        total_bytes += result.bytes_sent

    return TransferOutcome(
        ok=True, storage=STORAGE_SD_CARD, files_sent=sent, files_total=len(files),
        bytes_sent=total_bytes, per_file=per_file,
    )
