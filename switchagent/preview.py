"""Standalone, read-only analysis of any given path -- powers the
`preview` CLI command.

Unlike scanner.py this does NOT require the path to live inside inbox/ and
does NOT touch the database -- it's a "what would happen if I dropped this
in inbox?" tool. Extraction into work/<job-id>/ only happens when the
caller explicitly asks for it (--extract); by default this only peeks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, emuiibo, extractor, nro, scanner, sd_files, title_id
from .model import ConflictEntry, ConflictState, ContentType


@dataclass
class PackageEntryPreview:
    """One installable package entry inside a GAME_PACKAGE archive that
    contains more than one (e.g. Base+Update+DLC in one .zip) -- see
    PreviewReport.package_entries below. relative_path is relative to
    `work_dir` once extracted, exactly like PreviewReport.package_relative_path
    is for the (single, first) entry those singular fields still describe."""
    relative_path: str
    package_format: str
    title_id: Optional[str]
    title_id_confident: bool
    variant: Optional[str]  # "BASE" | "UPDATE" | "DLC" | None (title_id unknown/unclassifiable)
    size: int


@dataclass
class PreviewReport:
    content_type: ContentType
    source: str
    title_id: Optional[str] = None
    title_id_confident: bool = False
    package_format: Optional[str] = None
    size: int = 0
    file_count: Optional[int] = None
    destination: Optional[str] = None
    mode: Optional[str] = None
    note: Optional[str] = None
    conflicts: list[ConflictEntry] = field(default_factory=list)
    job_id: Optional[str] = None
    work_dir: Optional[Path] = None

    # Populated when the report has an actual local file/folder ready to
    # hand to transfer.py -- see that module's docstring for why it is
    # deliberately NOT responsible for extracting archives itself.
    package_relative_path: Optional[str] = None  # GAME_PACKAGE: path of the package file, relative to `source` (bare file) or `work_dir` (extracted archive)
    mod_source_dir: Optional[Path] = None         # ATMOSPHERE_MOD: local folder containing the mod's romfs/exefs tree, ready to walk
    # The local switch/ folder whose contents go to switch/ on the SD card
    # (see sd_files.py): the whole payload of an SD_FILES report, and, once
    # extracted, the part of a package or mod archive that ships one too.
    sd_source_dir: Optional[Path] = None
    sd_summary: Optional[sd_files.SdSummary] = None

    # GAME_PACKAGE archives only: every installable entry found (Base,
    # Update, any number of DLC), in listing order. The singular fields
    # above (title_id/package_format/package_relative_path/...) always
    # describe this list's first element -- unchanged for every existing
    # single-package caller. Empty for a bare package file and for an
    # archive with exactly one installable entry (nothing for a caller
    # that only reads the singular fields to gain by also consulting this).
    package_entries: list[PackageEntryPreview] = field(default_factory=list)

    # EMUIIBO / AMIIBO: what to copy, as (path relative to copy_root, SD-card
    # destination) pairs -- decided by emuiibo.py from the release's or the
    # collection's own structure, never by copying a tree wholesale.
    copy_root: Optional[Path] = None
    copy_plan: list[tuple[str, str]] = field(default_factory=list)
    emuiibo_version: Optional[str] = None
    # AMIIBO: the whole collection, so a selection can narrow copy_plan to
    # some of its amiibo (see web/services.create_and_confirm_jobs).
    amiibo_collection: Optional[emuiibo.Collection] = None


def amiibo_copy_plan(collection: emuiibo.Collection, selected=None) -> list[tuple[str, str]]:
    return [(src, dest) for a in (selected if selected is not None else collection.amiibo)
            for src, dest, _size in a.destinations()]


def _atmosphere_destination_dir(title_id_value: str) -> Path:
    return config.MOCK_SD_CARD_DIR / "atmosphere" / "contents" / title_id_value


def _summarize_conflicts(conflicts: list[ConflictEntry]) -> str:
    new = sum(1 for c in conflicts if c.state is ConflictState.NEW)
    same = sum(1 for c in conflicts if c.state is ConflictState.SAME)
    conflict = sum(1 for c in conflicts if c.state is ConflictState.CONFLICT)
    return f"NEW={new} SAME={same} CONFLICT={conflict}"


def _preview_package_file(path: Path) -> PreviewReport:
    guess = title_id.from_filename(path.name)
    fmt = path.suffix.lstrip(".").upper()
    size = path.stat().st_size
    if guess.title_id is None:
        return PreviewReport(
            content_type=ContentType.GAME_PACKAGE, source=str(path),
            package_format=fmt, size=size, title_id=None, title_id_confident=False,
            note="could not unambiguously determine TITLE_ID from the filename -- NEEDS_REVIEW",
        )
    return PreviewReport(
        content_type=ContentType.GAME_PACKAGE, source=str(path),
        package_format=fmt, size=size,
        title_id=guess.title_id, title_id_confident=guess.confident,
        destination="SD install", mode="INSTALL",
        package_relative_path=path.name,
    )


def _preview_archive(path: Path, *, extract: bool) -> PreviewReport:
    archive_format = path.suffix.lstrip(".").upper()
    try:
        entries = extractor.list_archive_entries(path)
    except extractor.CorruptArchiveError as exc:
        return PreviewReport(
            content_type=ContentType.UNKNOWN, source=str(path),
            note=f"could not open {archive_format}: {exc}",
        )

    cls = extractor.classify_entries(entries, archive_name=path.name)
    size = sum(e.size for e in entries if not e.is_dir)
    file_count = sum(1 for e in entries if not e.is_dir)

    if cls.content_type is ContentType.MIXED:
        return PreviewReport(
            content_type=ContentType.MIXED, source=str(path),
            package_format=cls.package_format, size=size, file_count=file_count,
            note="archive contains both an installable package and an atmosphere mod -- NEEDS_REVIEW, no action suggested",
        )

    if cls.content_type is ContentType.GAME_PACKAGE:
        guess = cls.package_title_id_guess
        package_entries = []
        for e in cls.package_entries:
            variant = None
            if e.title_id_guess.title_id:
                variant = title_id.classify_title_variant(e.title_id_guess.title_id).variant
            package_entries.append(PackageEntryPreview(
                relative_path=e.name, package_format=e.format,
                title_id=e.title_id_guess.title_id, title_id_confident=e.title_id_guess.confident,
                variant=variant, size=e.size,
            ))
        report = PreviewReport(
            content_type=ContentType.GAME_PACKAGE, source=str(path),
            package_format=cls.package_format, size=size, file_count=file_count,
            title_id=guess.title_id, title_id_confident=guess.confident,
            destination="SD install" if guess.title_id else None,
            mode="INSTALL" if guess.title_id else None,
            note=None if guess.title_id else "could not unambiguously determine TITLE_ID -- NEEDS_REVIEW",
            package_relative_path=cls.package_entry_name,
            package_entries=package_entries,
        )
        if extract:
            job_id = extractor.new_job_id()
            result = extractor.safe_extract(path, job_id)
            report.job_id = job_id
            report.work_dir = result.dest_root
            report.file_count = result.file_count
            report.size = result.total_size
            # Declared archive sizes above are only ever a listing claim
            # (see extractor.py's own "don't trust declared sizes" rule) --
            # once real bytes are on disk, reflect each entry's real,
            # verified size, same as report.size already does.
            for pe in report.package_entries:
                extracted_file = result.dest_root / pe.relative_path
                if extracted_file.is_file():
                    pe.size = extracted_file.stat().st_size
            _attach_sd_part(report, cls, result.dest_root)
        report.sd_summary = cls.sd_summary
        return report

    if cls.content_type is ContentType.ATMOSPHERE_MOD:
        guess = cls.atmosphere_title_id_guess
        dest_dir = _atmosphere_destination_dir(guess.title_id)
        conflicts = extractor.compute_conflicts_from_archive_entries(
            entries, cls.atmosphere_root, dest_dir
        )
        report = PreviewReport(
            content_type=ContentType.ATMOSPHERE_MOD, source=str(path),
            size=size, file_count=file_count,
            title_id=guess.title_id, title_id_confident=guess.confident,
            destination="SD Card", mode="MERGE", conflicts=conflicts,
        )
        if extract:
            job_id = extractor.new_job_id()
            result = extractor.safe_extract(path, job_id)
            report.job_id = job_id
            report.work_dir = result.dest_root
            report.file_count = result.file_count
            report.size = result.total_size
            mod_source = result.dest_root / cls.atmosphere_root.rstrip("/")
            if mod_source.is_dir():
                report.conflicts = extractor.compute_conflicts_from_folder(mod_source, dest_dir)
                report.mod_source_dir = mod_source
            _attach_sd_part(report, cls, result.dest_root)
        report.sd_summary = cls.sd_summary
        return report

    if cls.content_type is ContentType.EMUIIBO:
        plan = cls.emuiibo_release
        report = PreviewReport(
            content_type=ContentType.EMUIIBO, source=str(path),
            size=sum(size for _s, _d, size in plan.files), file_count=len(plan.files),
            destination="SD Card (emuiibo)", mode="MERGE",
            emuiibo_version=emuiibo.version_from_name(path.name),
        )
        if extract:
            job_id = extractor.new_job_id()
            result = extractor.safe_extract(path, job_id)
            report.job_id = job_id
            report.work_dir = result.dest_root
            report.copy_root = result.dest_root
            report.copy_plan = [(src, dest) for src, dest, _size in plan.files]
            if plan.overlay_source:
                overlay = result.dest_root / plan.overlay_source
                if overlay.is_file():
                    report.emuiibo_version = emuiibo.overlay_version(overlay.read_bytes()) or report.emuiibo_version
        return report

    if cls.content_type is ContentType.AMIIBO:
        summary = cls.amiibo.summary()
        report = PreviewReport(
            content_type=ContentType.AMIIBO, source=str(path),
            size=summary["size"], file_count=summary["files"],
            destination="SD Card (emuiibo/amiibo)", mode="MERGE",
            amiibo_collection=cls.amiibo, sd_summary=cls.sd_summary,
        )
        if extract:
            job_id = extractor.new_job_id()
            result = extractor.safe_extract(path, job_id)
            report.job_id = job_id
            report.work_dir = result.dest_root
            report.copy_root = result.dest_root
            report.copy_plan = amiibo_copy_plan(cls.amiibo)
            _attach_sd_part(report, cls, result.dest_root)
        return report

    if cls.content_type is ContentType.SD_FILES:
        guess = title_id.from_filename(path.name)
        report = PreviewReport(
            content_type=ContentType.SD_FILES, source=str(path),
            size=cls.sd_summary.size, file_count=cls.sd_summary.files,
            title_id=guess.title_id, title_id_confident=False,
            destination="SD Card (switch/)", mode="MERGE", sd_summary=cls.sd_summary,
        )
        if extract:
            job_id = extractor.new_job_id()
            result = extractor.safe_extract(path, job_id)
            report.job_id = job_id
            report.work_dir = result.dest_root
            _attach_sd_part(report, cls, result.dest_root)
            if report.sd_source_dir is not None:
                report.sd_summary = sd_files.folder_summary(report.sd_source_dir) or report.sd_summary
                report.size = report.sd_summary.size
                report.file_count = report.sd_summary.files
        return report

    return PreviewReport(
        content_type=ContentType.UNKNOWN, source=str(path),
        size=size, file_count=file_count,
        note="archive contains neither an nsp/nsz/xci/xcz package, an atmosphere structure nor a switch/ folder",
    )


def _attach_sd_part(report: PreviewReport, cls, dest_root: Path) -> None:
    """Points the report at the extracted switch/ folder, if the archive had
    one. Resolved against the real extraction, never the listing alone."""
    if cls.loose_nro:
        _stage_loose_nro(report, cls.loose_nro, dest_root)
        return
    if cls.sd_root is None:
        return
    extracted = dest_root.joinpath(*cls.sd_root)
    if extracted.is_dir():
        report.sd_source_dir = extracted


def _stage_loose_nro(report: PreviewReport, names: list[str], dest_root: Path) -> None:
    """Lays an archive's loose .nro files out as the switch/ folder they
    become (<name>/<name>.nro each), inside the same extraction, so the
    rest of the pipeline sees an ordinary switch/ folder."""
    import uuid

    staged = dest_root / f".sd-{uuid.uuid4().hex}"
    for name in names:
        extracted = extractor.safe_relative_path(name, dest_root)
        if not extracted.is_file():
            return
        target = staged / sd_files.loose_nro_relative(extracted.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        extracted.replace(target)
    report.sd_source_dir = staged


def _preview_sd_folder(path: Path, summary: sd_files.SdSummary) -> PreviewReport:
    return PreviewReport(
        content_type=ContentType.SD_FILES, source=str(path),
        size=summary.size, file_count=summary.files,
        destination="SD Card (switch/)", mode="MERGE",
        sd_source_dir=path, sd_summary=summary,
    )


def _preview_directory(path: Path) -> PreviewReport:
    if path.name.lower() == sd_files.SD_ROOT_DIR:
        summary = sd_files.folder_summary(path)
        if summary is not None:
            return _preview_sd_folder(path, summary)

    if scanner.TITLE_ID_DIR_RE.fullmatch(path.name):
        title_id_value = path.name.upper()
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        file_count = sum(1 for f in path.rglob("*") if f.is_file())
        dest_dir = _atmosphere_destination_dir(title_id_value)
        conflicts = extractor.compute_conflicts_from_folder(path, dest_dir)
        return PreviewReport(
            content_type=ContentType.ATMOSPHERE_MOD, source=str(path),
            size=size, file_count=file_count,
            title_id=title_id_value, title_id_confident=True,
            destination="SD Card", mode="MERGE", conflicts=conflicts,
            mod_source_dir=path,
        )

    release = _preview_release_folder(path)
    if release is not None:
        return release

    mod_folders = scanner.find_mod_folders(path)
    if not mod_folders:
        collection = scanner.folder_collection(path)
        if collection is not None:
            summary = collection.summary()
            return PreviewReport(
                content_type=ContentType.AMIIBO, source=str(path),
                size=summary["size"], file_count=summary["files"],
                destination="SD Card (emuiibo/amiibo)", mode="MERGE",
                amiibo_collection=collection, copy_root=path, copy_plan=amiibo_copy_plan(collection),
            )
    if len(mod_folders) == 1:
        return _preview_directory(mod_folders[0])
    if len(mod_folders) > 1:
        return PreviewReport(
            content_type=ContentType.MIXED, source=str(path),
            note=f"found {len(mod_folders)} different atmosphere/contents/<TITLE_ID> folders inside -- specify a more precise path",
        )

    sd_folders = sd_files.find_sd_folders(
        (p for p in path.rglob("*") if p.name.lower() == sd_files.SD_ROOT_DIR and p.is_dir()),
    )
    if len(sd_folders) == 1:
        return _preview_directory(sd_folders[0])
    if len(sd_folders) > 1:
        return PreviewReport(
            content_type=ContentType.MIXED, source=str(path),
            note=f"found {len(sd_folders)} different switch/ folders inside -- specify a more precise path",
        )

    return PreviewReport(
        content_type=ContentType.UNKNOWN, source=str(path),
        note="folder contains neither a package, an atmosphere/contents/<TITLE_ID> structure nor a switch/ folder",
    )


def _preview_release_folder(path: Path) -> Optional[PreviewReport]:
    """An unpacked emuiibo release (the folder atmosphere/ sits in)."""
    program = scanner._child_ci(path, "atmosphere")
    program = scanner._child_ci(program, "contents") if program else None
    program = scanner._child_ci(program, emuiibo.PROGRAM_ID) if program else None
    if program is None or not (program / "exefs.nsp").is_file():
        return None
    plan = emuiibo.plan_release(scanner.release_folder_files(path))
    if plan is None or not plan.files:
        return None
    version = None
    if plan.overlay_source:
        version = emuiibo.overlay_version((path / plan.overlay_source).read_bytes())
    return PreviewReport(
        content_type=ContentType.EMUIIBO, source=str(path),
        size=sum(size for _s, _d, size in plan.files), file_count=len(plan.files),
        destination="SD Card (emuiibo)", mode="MERGE",
        copy_root=path, copy_plan=[(src, dest) for src, dest, _size in plan.files],
        emuiibo_version=version or emuiibo.version_from_name(path.name),
    )


def preview_path(path: Path, *, extract: bool = False) -> PreviewReport:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))

    if path.is_dir():
        return _preview_directory(path)

    ext = path.suffix.lower()
    if ext in config.PACKAGE_EXTENSIONS:
        return _preview_package_file(path)
    if ext in config.ARCHIVE_EXTENSIONS:
        return _preview_archive(path, extract=extract)
    if ext == sd_files.NRO_EXTENSION and nro.read_nro_info(path) is not None:
        size = path.stat().st_size
        return _preview_sd_folder(path, sd_files.summarize([(sd_files.loose_nro_relative(path.name), size)]))

    return PreviewReport(
        content_type=ContentType.UNKNOWN, source=str(path),
        size=path.stat().st_size,
        note=f"unrecognized extension: {ext or '(none)'}",
    )


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_report(report: PreviewReport) -> str:
    lines = [f"TYPE: {report.content_type.value}"]

    if report.content_type is ContentType.MIXED:
        lines.append("ACTION: NEEDS_REVIEW")
        if report.note:
            lines.append(f"NOTE: {report.note}")
        lines.append(f"SOURCE: {report.source}")
        return "\n".join(lines)

    if report.package_format:
        lines.append(f"FORMAT: {report.package_format}")
    if report.title_id:
        confidence = "confirmed by structure" if report.title_id_confident else "from filename, not confirmed"
        lines.append(f"TITLE_ID: {report.title_id} ({confidence})")
    lines.append(f"SIZE: {_format_size(report.size)}")
    lines.append(f"SOURCE: {report.source}")
    if report.destination:
        lines.append(f"DESTINATION: {report.destination}")
    if report.mode:
        lines.append(f"MODE: {report.mode}")
    if report.file_count is not None:
        lines.append(f"FILES: {report.file_count}")
    if report.conflicts:
        # ARCH-004: this compares against config.MOCK_SD_CARD_DIR (a local
        # placeholder folder on THIS PC, see _atmosphere_destination_dir()
        # above), never a real connected Switch -- the label must say so
        # explicitly, or a real CONFLICT line here reads exactly like a
        # live check against the user's actual device, which it is not.
        # The real, live check for an actual transfer already happens
        # separately, at send time, via backend.exists() (see
        # queue_worker._run_job_transfer()) -- unrelated to this estimate
        # and unaffected by this label change.
        lines.append(f"LOCAL PREVIEW CONFLICTS (against {config.MOCK_SD_CARD_DIR}, NOT your real Switch): "
                      f"{_summarize_conflicts(report.conflicts)}")
        for c in report.conflicts:
            if c.state is not ConflictState.NEW:
                lines.append(f"  {c.state.value:<8} {c.relative_path}")
    if report.job_id:
        lines.append(f"JOB_ID: {report.job_id}")
        lines.append(f"WORK_DIR: {report.work_dir}")
    if report.content_type is ContentType.EMUIIBO:
        version = report.emuiibo_version or "unknown"
        outdated = " (outdated -- latest known is " + emuiibo.LATEST_KNOWN_VERSION + ")"             if emuiibo.is_outdated(report.emuiibo_version) else ""
        lines.append(f"EMUIIBO VERSION: {version}{outdated}")
    if report.amiibo_collection is not None:
        summary = report.amiibo_collection.summary()
        lines.append(f"AMIIBO: {summary['count']} in {len(summary['groups'])} folder(s)"
                     + (f", {summary['disabled']} without amiibo.flag (emuiibo ignores those)"
                        if summary["disabled"] else ""))
    if report.note:
        lines.append(f"NOTE: {report.note}")
    if (report.title_id is None and report.content_type not in (
            ContentType.UNKNOWN, ContentType.EMUIIBO, ContentType.AMIIBO)):
        lines.append("ACTION: NEEDS_REVIEW")

    return "\n".join(lines)
