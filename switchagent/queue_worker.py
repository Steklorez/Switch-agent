"""Sequential queue worker: processes CONFIRMED jobs one at a time, each
strictly against its own already-assigned target device AND its own frozen
content snapshot.

Two guarantees this module exists to enforce, both driven by findings from
a pre-Stage-5 architecture audit (see docs/STAGE4.1.md):

  1. Multi-device (stage 4): a job's target_device_id is fixed at creation
     and NEVER substituted. If that specific device isn't reachable right
     now, the job waits -- it is not sent to some other, currently-
     available device just because one happens to be plugged in.

  2. Content pinning (stage 4.1): a job's target CONTENT is equally fixed
     at creation, as a manifest (switchagent/manifest.py) built from the
     exact PreviewReport the user was shown. The worker never re-scans
     inbox/ to decide what to send -- it re-verifies the frozen manifest
     against whatever is currently on disk, and refuses (does not silently
     send something different) if they've drifted apart. It also never
     assumes an existing destination file is safe to skip just because it
     exists -- only a file THIS job's own prior send_file call is recorded
     (in manifest.py's progress.json) as having delivered gets skipped;
     anything else found already present is treated as an unresolved
     conflict, not as proof of a prior successful send. Real MTP gives no
     cheap way to hash a remote file to prove equivalence, and this
     codebase deliberately does not pretend otherwise.

One worker, one job, one device at a time (the explicitly allowed minimal
shape for this stage) -- but nothing here assumes there is only ever one
device in the system. DeviceRegistry maps many device_id's to their own
MtpBackend instances; run_worker_once() scans CONFIRMED jobs in creation
order and processes the FIRST one whose target device is currently
reachable, skipping (not redirecting) any it passes over.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import db
from . import manifest as manifest_mod
from . import title_id as title_id_mod
from . import work_cleanup
from .model import ContentType
from .mtp.base import MtpBackend, TransferStatus
from .mtp.errors import DeviceNotFoundError, FileAlreadyExistsError, MtpError
from .preview import PreviewReport
from .transfer import STORAGE_SD_CARD, STORAGE_SD_INSTALL

log = logging.getLogger("switchagent.queue_worker")


class DeviceRegistry:
    """Maps a stable device_id to the MtpBackend instance that can reach it.
    Registering a backend here does NOT mean the device is currently
    plugged in -- run_worker_once() finds that out the same way it always
    would for a single device, by calling backend.connect() and handling
    DeviceNotFoundError. This is deliberately just a dict wrapper: device
    identity/enumeration is the backend's concern (see mtp/base.py), not
    this registry's -- it only remembers which backend instance to ask for
    a given device_id."""

    def __init__(self):
        self._backends: dict[str, MtpBackend] = {}

    def register(self, device_id: str, backend: MtpBackend) -> None:
        self._backends[device_id] = backend

    def get(self, device_id: str) -> Optional[MtpBackend]:
        return self._backends.get(device_id)

    def known_device_ids(self) -> list[str]:
        return list(self._backends)


@dataclass
class JobRunOutcome:
    job_id: int
    status: str
    error: Optional[str] = None


def _target_storage_for(report: PreviewReport) -> str:
    if report.content_type is ContentType.GAME_PACKAGE:
        return STORAGE_SD_INSTALL
    if report.content_type is ContentType.ATMOSPHERE_MOD:
        return STORAGE_SD_CARD
    raise manifest_mod.ManifestError(
        f"content type {report.content_type.value} is not eligible for transfer"
    )


def create_job_from_report(
    conn: sqlite3.Connection,
    report: PreviewReport,
    *,
    action: str,
    target_device_id: str,
    inbox_item_id: Optional[int] = None,
    library_item_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    force_overwrite: bool = False,
    retry_of_job_id: Optional[int] = None,
) -> int:
    """The one correct way to create a job that can actually be confirmed
    and run. Builds and freezes a content manifest from `report` -- the
    exact object the user was shown during preview, never re-derived from a
    fresh scan -- then creates the PENDING_CONFIRM row and attaches the
    manifest to it before returning.

    Exactly one of inbox_item_id (Stage 2's inbox/ pipeline) or
    library_item_id (Web UI's library_items) must be given -- see
    db.create_job() for the enforced invariant.

    force_overwrite (W3-006 Override): threaded straight to db.create_job()
    -- see that function's own comment on the column. Only
    services.override_job() ever passes True.

    retry_of_job_id: threaded straight to db.create_job() -- see that
    column's own comment. Only services.retry_job()'s direct-source path
    passes this (the staged/frozen-payload path sets it separately, after
    this returns, alongside manifest_path/payload_batch_id).

    Raises manifest.ManifestError if `report` has nothing locally
    stageable yet (MIXED/UNKNOWN content, or an archive that was never
    extracted with preview_path(path, extract=True)). If manifest building
    fails AFTER the job row was inserted (staging error), the row is left
    behind as FAILED rather than stuck, unconfirmable, in PENDING_CONFIRM
    forever -- but the exception still propagates so the caller knows
    creation did not succeed."""
    target_storage = _target_storage_for(report)  # raises before any DB row exists, for MIXED/UNKNOWN
    job_id = db.create_job(
        conn, inbox_item_id=inbox_item_id, library_item_id=library_item_id, action=action,
        target_storage=target_storage, target_device_id=target_device_id, batch_id=batch_id,
        force_overwrite=force_overwrite, retry_of_job_id=retry_of_job_id,
    )
    try:
        manifest = manifest_mod.build_manifest_and_stage(
            report, job_id, target_storage=target_storage, batch_id=batch_id,
        )
    except manifest_mod.ManifestError as exc:
        db.update_job_status(conn, job_id, "FAILED", error=f"could not build manifest: {exc}")
        db.log_job_event(conn, job_id, f"manifest build failed: {exc}")
        raise

    db.set_job_manifest_path(conn, job_id, str(manifest_mod.manifest_path_for(job_id)))
    db.log_job_event(conn, job_id, f"manifest frozen: {len(manifest.files)} file(s)")
    return job_id


def _base_title_id_for_dependency_check(manifest) -> Optional[str]:
    """The family/base TITLE_ID a job's manifest actually depends on for
    install ordering -- mods are tagged with the base's own TITLE_ID
    directly; everything else recovers it via
    title_id.classify_title_variant(). None if the manifest has no
    title_id at all. Shared with _dependency_status below (same
    computation) so run_worker_once's "waiting for base game (X)" message
    always names the ACTUAL base being waited on -- BUG: it used to show
    manifest.title_id (the waiting job's OWN id, e.g. the Update's or
    DLC's own TITLE_ID), not the base's, which is exactly backwards."""
    if not manifest.title_id:
        return None
    if manifest.content_type == ContentType.ATMOSPHERE_MOD.value:
        return manifest.title_id
    return title_id_mod.classify_title_variant(manifest.title_id).base_title_id


def _dependency_status(conn: sqlite3.Connection, manifest, target_device_id: str) -> Optional[str]:
    """Enforces Base Game -> Update/DLC/Mod install order (see
    docs/STATE.md's Quake II investigation: an Update was allowed to
    install while its Base Game job was still outstanding, and the base
    game itself silently never completed -- Horizon showed an icon for a
    title whose actual application data was never delivered).

    Returns None if this job has no unmet dependency -- either it IS a
    base-variant job itself (bases have no dependency), its base is
    already confirmed installed per OUR OWN install_history (never a live
    device read -- this hardware has no MTP-visible installed-games list,
    same limitation as everywhere else in this project), or there simply
    is no competing Base Game job for this title on this device right now
    (nothing in the SAME batch/session to wait behind -- see the
    deliberate scoping note below). Otherwise returns "WAITING_FOR_BASE"
    (a Base Game job for this title exists and hasn't succeeded yet --
    try again next pass) or "BLOCKED_BY_DEPENDENCY" (install_history shows
    the base variant itself failed/was interrupted/conflicted -- this job
    will never auto-run).

    Deliberately scoped to an ACTUAL outstanding base job, not "no
    install_history exists for this title at all": this hardware cannot
    prove what's already installed on the console (no MTP-visible
    installed-games list -- see docs/STAGE5A-MTP-RESEARCH.md), so treating
    "no history" as "must wait forever" would make it impossible to ever
    install an update/DLC/mod for a game the user already owns/installed
    by some other means, which was never broken and is not what the real
    incident (see docs/STATE.md's Quake II investigation) was about -- that
    was specifically a base and its update racing within the SAME
    confirmed batch. What's actually enforced: an update/DLC/mod job
    never overtakes a base job for the SAME title that is genuinely still
    in flight or already known to have failed.

    Cannot classify without a title_id -- returns None (no dependency
    enforced) rather than blocking on an unknown; this mirrors every
    other "cannot determine, so don't guess" rule in this codebase."""
    if not manifest.title_id:
        return None

    if manifest.content_type == ContentType.ATMOSPHERE_MOD.value:
        base_title_id = manifest.title_id  # mods are tagged with the base's own TITLE_ID directly
    else:
        variant, base_title_id = title_id_mod.classify_title_variant(manifest.title_id)
        if variant == "BASE":
            return None

    # A currently-outstanding (non-terminal) Base job for this exact
    # title+device always outranks install_history -- checked FIRST, before
    # any success/failure history lookup. This prevents stale History from
    # letting a new Update/DLC overtake a brand new Base: re-installing a
    # deleted game (a fresh Base+Update+DLC batch) must never let the new
    # Update start just because an OLDER Base install once succeeded on
    # this device -- the new Base, still in flight, must reach a terminal
    # outcome first. DONE_UNVERIFIED still counts as a sufficient transport
    # outcome the moment the new Base finishes (see
    # has_successful_base_install) -- only a base job that is genuinely
    # still non-terminal is prioritized over history here.
    if _has_outstanding_base_job(conn, target_device_id, base_title_id):
        return "WAITING_FOR_BASE"
    if db.has_successful_base_install(conn, target_device_id, base_title_id):
        return None
    if db.has_failed_base_install(conn, target_device_id, base_title_id):
        return "BLOCKED_BY_DEPENDENCY"
    return None


_NON_TERMINAL_JOB_STATUSES = ("PENDING_CONFIRM", "CONFIRMED", "DEVICE_UNAVAILABLE", "RUNNING", "VERIFYING")


def _has_outstanding_base_job(conn: sqlite3.Connection, target_device_id: str, base_title_id: str) -> bool:
    """Is there a Base Game job for this exact title, on this device, that
    hasn't reached a terminal state yet? Deliberately NOT "did a base job
    ever exist" -- a base job that already finished is covered by
    install_history (checked before this is ever called). jobs.title_id
    isn't a column (title_id only lives in each job's frozen manifest
    file), so this loads the manifest for every non-terminal job on this
    device -- fine at this project's scale (a personal library's worth of
    concurrent jobs, not thousands)."""
    placeholders = ",".join("?" * len(_NON_TERMINAL_JOB_STATUSES))
    rows = conn.execute(
        f"SELECT id FROM jobs WHERE target_device_id = ? AND status IN ({placeholders})",
        (target_device_id, *_NON_TERMINAL_JOB_STATUSES),
    ).fetchall()
    for row in rows:
        try:
            candidate = manifest_mod.load_manifest(row["id"])
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            # FAULT-001: a malformed (not just missing) manifest on some
            # OTHER non-terminal job on this device must never crash the
            # dependency check for the job actually being evaluated right
            # now -- that OTHER job's own manifest corruption (if any) is
            # handled separately, in run_worker_once() itself, the next
            # time IT is the one being considered.
            continue
        if candidate.content_type != ContentType.GAME_PACKAGE.value or not candidate.title_id:
            continue
        variant, candidate_base_id = title_id_mod.classify_title_variant(candidate.title_id)
        if variant == "BASE" and candidate_base_id == base_title_id:
            return True
    return False


def _decide_device_absence_status(job_row: sqlite3.Row) -> str:
    """UI-005: distinguishes a job that has NEVER started a transfer
    (attempt_count == 0, no delivered files recorded in progress.json) --
    eligible for the gentler, self-resolving WAITING_FOR_DEVICE -- from one
    that already had at least one attempt or partial delivery, which keeps
    the existing, more conservative DEVICE_UNAVAILABLE (requires an
    explicit manual retry; db.retry_job() deliberately never auto-retries
    an already-attempted job, see its own docstring). A job already
    WAITING_FOR_DEVICE is re-evaluated by this same check on every pass
    (see list_confirmed_jobs()) -- that's what makes it self-resolving the
    moment its SAME target_device_id reappears, no manual action needed.
    Deliberately narrow: a job that already touched the device in any way
    keeps the older, more cautious status -- this never widens what
    DEVICE_UNAVAILABLE used to mean, only carves out the genuinely-never-
    started case."""
    if job_row["attempt_count"] > 0:
        return "DEVICE_UNAVAILABLE"
    if manifest_mod.load_progress(job_row["id"]):
        return "DEVICE_UNAVAILABLE"
    return "WAITING_FOR_DEVICE"


def run_worker_once(conn: sqlite3.Connection, registry: DeviceRegistry) -> Optional[JobRunOutcome]:
    """Does at most one unit of work: finds the oldest CONFIRMED (or
    WAITING_FOR_BASE/WAITING_FOR_DEVICE) job whose target device is
    currently reachable AND whose install-order dependency (Base Game ->
    Update/DLC/Mod, see _dependency_status above) is satisfied, fully
    processes it (to DONE, FAILED, INTERRUPTED, SOURCE_CHANGED,
    DESTINATION_CONFLICT, or BLOCKED_BY_DEPENDENCY), and returns its
    outcome. Jobs it passes over because their device isn't reachable get
    one of two statuses (see _decide_device_absence_status above):
    WAITING_FOR_DEVICE for a job that never started a transfer at all
    (non-terminal, automatically re-checked every pass, no manual action
    needed -- self-resolving the moment its target device reappears), or
    the existing, unchanged DEVICE_UNAVAILABLE for one that already had an
    attempt or partial delivery (requires an explicit db.retry_job() call;
    never retried automatically on a later pass -- see docs/STAGE4.1.md).
    Jobs waiting on their base game are marked WAITING_FOR_BASE and
    re-checked every pass automatically (no explicit action needed, unlike
    DEVICE_UNAVAILABLE) -- this is also what guarantees an Update/DLC/Mod
    can never reach RUNNING while its Base Game job is still outstanding:
    it simply never gets past this check to reach _process_job() at all.

    Returns None if there was nothing processable right now -- either the
    queue is empty, or every remaining job's target device is unreachable
    or dependency-blocked. Never picks a different device than the one a
    job was created with."""
    from .mtp.windows import device_fingerprint

    for job in db.list_confirmed_jobs(conn):
        device_id = job["target_device_id"]
        backend = registry.get(device_id)

        if backend is None:
            status = _decide_device_absence_status(job)
            # Fingerprint, never the raw device_id -- it embeds the
            # device's real USB serial number (see mtp/windows.py's
            # mask_device_id docstring), and this message is stored in
            # jobs.error/job_log, which the Web UI renders directly to a
            # human (queue.html's {{ j.error }}) -- found while adding
            # this status: the pre-existing DEVICE_UNAVAILABLE message
            # below had the same gap, fixed here alongside it since both
            # now share this exact code path.
            error = f"device '{device_fingerprint(device_id)}' is not registered with this worker"
            if status != job["status"]:  # avoid rewriting/re-logging every pass while still absent
                db.update_job_status(conn, job["id"], status, error=error)
                db.log_job_event(conn, job["id"], f"{error} -> {status}")
            continue

        try:
            backend.connect()
        except DeviceNotFoundError as exc:
            status = _decide_device_absence_status(job)
            error = f"device '{device_fingerprint(device_id)}' not currently reachable: {exc}"
            if status != job["status"]:
                db.update_job_status(conn, job["id"], status, error=error)
                db.log_job_event(conn, job["id"], f"{error} -> {status}")
            continue

        try:
            manifest = manifest_mod.load_manifest(job["id"])
        except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
            # FAULT-001: a job's manifest.json can only ever go missing or
            # become unreadable through something outside this application's
            # own control (manual deletion, disk corruption, a future bug) --
            # never guess what it would have said. Fail this ONE job
            # outright (a corrupted content snapshot is not a transient
            # condition a later pass could resolve on its own) and keep
            # scanning for other, still-processable work in this same call.
            # Previously this call was unguarded and raised straight out of
            # this for loop -- uncaught here or anywhere before
            # _worker_loop's own broad handler -- which meant the SAME
            # broken job (still first in creation order) would raise again
            # on every subsequent pass, silently blocking every job behind
            # it, for every device, forever, not just failing this one job.
            error = f"job manifest is missing or unreadable: {type(exc).__name__}: {exc}"
            db.update_job_status(conn, job["id"], "FAILED", error=error, finished_at=db.now_iso())
            db.log_job_event(conn, job["id"], error)
            db.record_install_history(
                conn, job_id=job["id"], title_id=None,
                display_name=display_name_for_job(conn, job),
                target_device_id=device_id, target_storage=job["target_storage"],
                outcome="FAILED", error=error, bytes_total=None,
            )
            continue

        dependency = _dependency_status(conn, manifest, device_id)

        if dependency == "WAITING_FOR_BASE":
            if job["status"] != "WAITING_FOR_BASE":  # write once, not every pass -- avoid spamming job_log
                error = (
                    f"waiting for base game ({_base_title_id_for_dependency_check(manifest)}) "
                    "to install successfully first"
                )
                db.update_job_status(conn, job["id"], "WAITING_FOR_BASE", error=error)
                db.log_job_event(conn, job["id"], "base game not yet installed -> WAITING_FOR_BASE")
            continue

        if dependency == "BLOCKED_BY_DEPENDENCY":
            error = (
                f"base game install for {_base_title_id_for_dependency_check(manifest)} "
                "did not succeed -- see History"
            )
            db.update_job_status(conn, job["id"], "BLOCKED_BY_DEPENDENCY", error=error, finished_at=db.now_iso())
            db.log_job_event(conn, job["id"], f"blocked: {error}")
            db.record_install_history(
                conn, job_id=job["id"], title_id=manifest.title_id,
                display_name=display_name_for_job(conn, job),
                target_device_id=device_id, target_storage=job["target_storage"],
                outcome="BLOCKED_BY_DEPENDENCY", error=error,
                bytes_total=sum(f.size for f in manifest.files),
            )
            continue

        # Found a job whose target device is reachable right now AND whose
        # dependency (if any) is satisfied -- process exactly this one and
        # stop (sequential: one worker, one job at a time, per this
        # stage's explicitly allowed minimal shape).
        return _process_job(conn, backend, job)

    return None


def find_family_base_name_source(
    conn: sqlite3.Connection, family_title_id: str, *, library_items: Optional[list] = None,
) -> Optional[sqlite3.Row]:
    """For a family TITLE_ID (a mod's own title_id IS its family id,
    atmosphere/contents/<TITLE_ID>/ convention; an update/DLC's family id
    is title_id.classify_title_variant()'s recovered base_title_id), finds
    the best available library_items row to borrow a display name from --
    preferring an actual Base Game, falling back to an Update or DLC if no
    base is currently indexed. Returns None if nothing in the library is
    known for that family at all -- callers keep showing the raw TITLE_ID
    rather than inventing a name (title_id.py's "never guess" principle,
    inherited here). Shared by both the Web UI's Library grouping
    (switchagent/web/services.py) and Queue/History (below) so a mod is
    never named one way in one place and differently in another -- found
    via a real UI bug report: a Bread and Fred mod showed as
    "Mod -- 0100AF401B6A4000" (its own TITLE_ID) instead of "Bread and
    Fred", in both places, until this existed.

    PERF-001: `library_items` lets a caller that is about to call this once
    PER ROW of a whole listing (e.g. Library grouping/Queue/History
    rendering) fetch+pass the full `db.list_library_items(conn)` list ONCE
    instead of this function silently re-querying and re-classifying the
    ENTIRE table on every single call -- confirmed via profiling to turn an
    O(mods x library size) full-table rescan into the dominant cost of
    rendering the Library page at real-world scale (measured: ~12s of a
    ~12.2s call for 5,340 rows / 200 mods, ~98% of it inside this function
    alone). Passing nothing preserves the exact old (correct, just
    potentially slower at scale) behavior for any caller that only ever
    needs a single one-off lookup."""
    if library_items is None:
        library_items = db.list_library_items(conn)
    candidates = []
    for row in library_items:
        if row["content_type"] != ContentType.GAME_PACKAGE.value or not row["title_id"]:
            continue
        variant, base_id = title_id_mod.classify_title_variant(row["title_id"])
        if base_id != family_title_id:
            continue
        candidates.append((0 if variant == "BASE" else 1, row))
    if not candidates:
        return None
    candidates.sort(key=lambda vc: vc[0])
    return candidates[0][1]


def resolve_library_item_display_name(
    conn: sqlite3.Connection, row: sqlite3.Row, *, library_items: Optional[list] = None,
) -> str:
    """The full filename/folder-name-based label (see display_name_for_job
    below for the .name-not-.stem convention Queue/History use) -- with
    the mod-parent-name fallback from find_family_base_name_source above
    applied for MOD_FOLDER rows, whose own folder name is ALWAYS just a
    bare TITLE_ID (never a product name at all, unlike a package
    filename). See find_family_base_name_source's own docstring for why
    `library_items` matters at scale."""
    raw_name = Path(row["absolute_path"]).name
    if row["item_type"] != "MOD_FOLDER" or not row["title_id"]:
        return raw_name
    source = find_family_base_name_source(conn, row["title_id"], library_items=library_items)
    return Path(source["absolute_path"]).name if source is not None else raw_name


def display_name_for_job(
    conn: sqlite3.Connection, job_row: sqlite3.Row, *, library_items: Optional[list] = None,
    mod_suffix: bool = True,
) -> str:
    """Best-effort human-readable label for install_history -- derived from
    whichever source row this job came from (library_items or inbox_items,
    see db.py). Falls back to a generic label if that row is gone by the
    time this runs (e.g. deleted from the library after the job already
    ran) -- history must still be recordable, never blocked on the source
    still existing. See find_family_base_name_source's own docstring for
    why `library_items` matters at scale.

    A mod's own label is often borrowed from its base game (see
    resolve_library_item_display_name's mod-parent-name fallback), which
    can be confusing in Queue/History: it looks identical to the base
    game's OWN job, with nothing to say "this one is a mod, not the game".
    mods always target SD_CARD (base/update/DLC
    installs always target SD_INSTALL, see _target_storage_for) -- an
    unambiguous, already-available signal to append " -- Mod" with,
    baked into install_history.display_name at recording time (this same
    function), not just a Queue-only display trick.

    mod_suffix: pass False from a surface that already says "Mod" some
    other way -- Queue rows carry a [Mod] badge now (see web/services.py's
    _job_variant_role), and a row reading "Russian Language Mod — Mod
    [Mod]" says it three times. History has no badge, so it keeps the
    suffix and is the reason this is a parameter rather than a deletion."""
    if job_row["library_item_id"] is not None:
        row = db.get_library_item_by_id(conn, job_row["library_item_id"])
        if row is not None:
            name = resolve_library_item_display_name(conn, row, library_items=library_items)
            return _with_mod_suffix(name, job_row) if mod_suffix else name
    if job_row["inbox_item_id"] is not None:
        row = db.get_inbox_item_by_id(conn, job_row["inbox_item_id"])
        if row is not None:
            name = Path(row["relative_path"]).name
            return _with_mod_suffix(name, job_row) if mod_suffix else name
    return f"job {job_row['id']}"


def _with_mod_suffix(name: str, job_row: sqlite3.Row) -> str:
    if job_row["target_storage"] == STORAGE_SD_CARD:
        return f"{name} — Mod"
    return name


def _process_job(conn: sqlite3.Connection, backend: MtpBackend, job_row: sqlite3.Row) -> JobRunOutcome:
    """Thin wrapper around _run_job_transfer(): every outcome it can
    possibly return is already terminal for THIS attempt (there is no
    "still running" return value -- RUNNING is only ever an intermediate
    status written to the DB mid-function, never the function's own
    result), so every call here gets exactly one install_history row (see
    docs/WEB-UI.md, point 12 -- History must show failures/interruptions
    too, not just successes).

    The try/except here is a deliberate safety net, found necessary by a
    real incident (see docs/STATE.md's Quake II investigation): an
    unexpected, non-MtpError exception raised deep inside a transfer (that
    one was a win32com AttributeError from a COM object invalidated by an
    unrelated thread -- see WebContext's class-level note in
    switchagent/web/context.py for the actual root cause and its fix) used
    to propagate all the way up to _worker_loop's own broad handler, which
    only logs and moves on -- leaving the job stuck at RUNNING forever
    (never terminal, never in install_history, invisible as an error to
    the user) while the worker went on to process the NEXT confirmed job
    as if nothing had happened. Catching it HERE, where job_id is known,
    means ANY unexpected failure -- whatever future cause -- still leaves
    an honest INTERRUPTED record instead of a silent gap. INTERRUPTED
    (not FAILED) because we genuinely don't know how much, if anything,
    reached the device -- same honesty standard already used for a
    detected mid-transfer disconnect."""
    try:
        outcome = _run_job_transfer(conn, backend, job_row)
    except Exception as exc:  # noqa: BLE001 -- see docstring: must never leave a job silently stuck
        error = f"unexpected error during transfer: {type(exc).__name__}: {exc}"
        db.update_job_status(conn, job_row["id"], "INTERRUPTED", error=error)
        db.log_job_event(conn, job_row["id"], f"unexpected exception, marked INTERRUPTED: {exc!r}")
        log.exception("unexpected exception while processing job %s", job_row["id"])
        outcome = JobRunOutcome(job_id=job_row["id"], status="INTERRUPTED", error=error)

    # FAULT-001: this job's own manifest could become unreadable somewhere
    # between _run_job_transfer() having already handled it (or having
    # itself failed for the identical reason, already caught above) and
    # this history-recording step -- never let recording history be the
    # reason a terminal outcome silently loses its permanent record. The
    # job's own status was already durably set above regardless of what
    # happens here.
    try:
        manifest = manifest_mod.load_manifest(job_row["id"])
        title_id = manifest.title_id
        bytes_total = sum(f.size for f in manifest.files)
    except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
        title_id = None
        bytes_total = None
        db.log_job_event(
            conn, job_row["id"],
            f"could not reload manifest for history recording: {type(exc).__name__}: {exc}",
        )

    db.record_install_history(
        conn, job_id=job_row["id"], title_id=title_id,
        display_name=display_name_for_job(conn, job_row),
        target_device_id=job_row["target_device_id"], target_storage=job_row["target_storage"],
        outcome=outcome.status, error=outcome.error,
        bytes_total=bytes_total,
    )
    # The instant this job's own outcome is durable, see whether its whole
    # batch (if any) is now fully done and can have its shared staging
    # removed -- see work_cleanup.cleanup_batch_if_all_done's own docstring
    # for why this is safe (a still-retryable sibling keeps everything
    # alive) and why it's additive to the existing manual sweep.
    work_cleanup.cleanup_batch_if_all_done(conn, job_row["batch_id"])
    if job_row["payload_batch_id"]:
        work_cleanup.cleanup_batch_if_all_done(conn, job_row["payload_batch_id"])
    return outcome


def _run_job_transfer(conn: sqlite3.Connection, backend: MtpBackend, job_row: sqlite3.Row) -> JobRunOutcome:
    job_id = job_row["id"]
    storage = job_row["target_storage"]
    manifest = manifest_mod.load_manifest(job_id)

    # TOCTOU guard (stage 4.1): verify EVERY manifest file still matches its
    # live source BEFORE touching the device at all -- not interleaved with
    # sending, so a source that drifted only partway through isn't left in
    # a confusing in-between state on the device.
    try:
        mismatch = manifest_mod.verify_manifest_against_source(manifest, job_id)
    except manifest_mod.ManifestError as exc:
        # FAULT-001: a manifest declaring an invalid/escaping relative path
        # or an unrecognized source_kind is corrupted data, not a
        # legitimate content-drift case -- fail outright, with a precise
        # reason, rather than let it fall through to the generic
        # unexpected-exception safety net (which would still catch this
        # safely as INTERRUPTED, but "we never even resolved a source path"
        # is a FAILED, not an ambiguous-mid-flight, situation).
        error = f"manifest is invalid: {exc}"
        db.update_job_status(conn, job_id, "FAILED", error=error, finished_at=db.now_iso())
        db.log_job_event(conn, job_id, error)
        return JobRunOutcome(job_id=job_id, status="FAILED", error=error)
    if mismatch is not None:
        if mismatch.kind == "missing":
            error = f"source no longer exists: {mismatch.dest_relative_path}"
            db.update_job_status(conn, job_id, "FAILED", error=error, finished_at=db.now_iso())
            db.log_job_event(conn, job_id, error)
            return JobRunOutcome(job_id=job_id, status="FAILED", error=error)

        error = f"source changed since confirmation: {mismatch.dest_relative_path}"
        db.update_job_status(conn, job_id, "SOURCE_CHANGED", error=error)
        db.log_job_event(conn, job_id, error + " -- create a new job from a fresh preview/confirmation")
        return JobRunOutcome(job_id=job_id, status="SOURCE_CHANGED", error=error)

    delivered = manifest_mod.load_progress(job_id)
    bytes_done = sum(f.size for f in manifest.files if f.dest_relative_path in delivered)
    # UI-006 follow-up: bytes_total/bytes_done were previously only ever
    # written at a TERMINAL status (see the DESTINATION_CONFLICT/FAILED/
    # DONE writes below and in _process_job's history recording) -- during
    # the whole RUNNING phase the jobs row kept showing bytes_total=NULL,
    # bytes_done=0 no matter how much had actually transferred. queue.js's
    # own progress-bar fraction already reads these two columns for
    # SD_CARD jobs; the frontend was ready, the backend just never fed it
    # real numbers, so a real, multi-minute, multi-file transfer looked
    # like a bare spinner with nothing else changing on screen.
    now = db.now_iso()
    db.update_job_status(
        conn, job_id, "RUNNING", started_at=now, last_progress_at=now, increment_attempt=True,
        bytes_done=bytes_done, bytes_total=sum(f.size for f in manifest.files),
    )
    db.log_job_event(conn, job_id, f"running on device '{job_row['target_device_id']}'")

    # Set if ANY file in THIS run completes with TransferStatus.UNVERIFIED
    # (see switchagent/mtp/windows.py -- reached for install-like storages
    # such as SD_INSTALL, where the backend can prove transport was
    # accepted but not prove DBI's own install completion). Downgrades the
    # job's final terminal status from DONE to DONE_UNVERIFIED below the
    # loop. A resumed job can never carry hidden UNVERIFIED history from an
    # earlier run: reaching UNVERIFIED for any file always ends that run's
    # job in the terminal DONE_UNVERIFIED status (see below), and
    # DONE_UNVERIFIED is deliberately not in retry_job()'s retryable set
    # (db.py) -- so _process_job() can never be re-entered for a job that
    # already delivered a file as UNVERIFIED in a prior run.
    any_unverified = False

    for file in manifest.files:
        if file.dest_relative_path in delivered:
            continue

        # UI-006 (stall detection): honest per-file "still actively
        # working" touch-point, in addition to the post-completion one
        # below -- for a single large file whose one send_file() call
        # itself takes a long time (no way to see progress DURING it,
        # this architecture has no such signal, see this function's own
        # docstring), this is what keeps last_progress_at reflecting "just
        # started sending this file" rather than staying frozen at
        # whenever the job first went RUNNING.
        db.update_job_status(conn, job_id, "RUNNING", last_progress_at=db.now_iso())

        parent = "/".join(file.dest_relative_path.split("/")[:-1])
        source_path = manifest_mod.resolve_source_path(file, job_id, batch_id=manifest.batch_id)

        try:
            backend.ensure_directory(storage, parent)

            # W3-006 Override: force_overwrite is only ever true on a job
            # created by services.override_job(), the user's explicit
            # "Override" click on a DESTINATION_CONFLICT card -- for that
            # one job, and that job only, send_file's own overwrite=True
            # replaces whatever is there, no existence check at all. Every
            # other job still refuses blindly -- via send_file's OWN
            # existence check (it already does the identical any(i.Name ==
            # ... for i in parent.GetFolder.Items()) scan internally before
            # copying) rather than a separate backend.exists() call first --
            # that used to re-navigate and re-scan the same destination
            # directory a second time for every single file, pure wasted
            # MTP round-trips this loop doesn't need.
            result = backend.send_file(
                storage, file.dest_relative_path, source_path,
                overwrite=bool(job_row["force_overwrite"]),
            )
        except FileAlreadyExistsError:
            # Exists on the device, but WE have no record (progress.json) of
            # having put it there ourselves during this job -- cannot prove
            # it's the same content, so refuse rather than guess. This is
            # idempotent-but-not-blind: a file THIS job already delivered
            # was already skipped above via `delivered`; anything else
            # present is genuinely unresolved.
            error = (
                f"'{file.dest_relative_path}' already exists on '{storage}' "
                "and was not sent by this job -- refusing to overwrite"
            )
            db.update_job_status(conn, job_id, "DESTINATION_CONFLICT", bytes_done=bytes_done, error=error)
            db.log_job_event(conn, job_id, error)
            return JobRunOutcome(job_id=job_id, status="DESTINATION_CONFLICT", error=error)
        except MtpError as exc:
            db.update_job_status(
                conn, job_id, "FAILED", bytes_done=bytes_done, error=str(exc), finished_at=db.now_iso(),
            )
            db.log_job_event(conn, job_id, f"failed: {exc}")
            return JobRunOutcome(job_id=job_id, status="FAILED", error=str(exc))

        if result.status is TransferStatus.COMPLETED:
            manifest_mod.mark_delivered(job_id, file.dest_relative_path)
            bytes_done += result.bytes_sent
            db.update_job_status(conn, job_id, "RUNNING", last_progress_at=db.now_iso(), bytes_done=bytes_done)
            continue

        if result.status is TransferStatus.UNVERIFIED:
            # Transport accepted, DBI-side result unprovable (see
            # switchagent/mtp/windows.py). Recorded delivered so a
            # (currently unreachable, see `any_unverified` above)
            # re-entry would never redundantly resend this file --
            # resending a real, already-accepted install is worse than
            # leaving it alone. bytes_done advances by the manifest's own
            # known size, not result.bytes_sent: for an install-like node
            # that figure is diagnostic-only and can legitimately read 0
            # even on a real, physically confirmed success (see
            # docs/STAGE5B-REAL-MTP.md) -- using it here would make a
            # correctly-accepted transfer look like it sent nothing.
            manifest_mod.mark_delivered(job_id, file.dest_relative_path)
            bytes_done += file.size
            any_unverified = True
            db.update_job_status(conn, job_id, "RUNNING", last_progress_at=db.now_iso(), bytes_done=bytes_done)
            db.log_job_event(conn, job_id, f"'{file.dest_relative_path}': {result.error or 'unverified'}")
            continue

        if result.status is TransferStatus.DEVICE_DISCONNECTED:
            error = result.error or "device disconnected mid-transfer"
            db.update_job_status(conn, job_id, "INTERRUPTED", bytes_done=bytes_done, error=error)
            db.log_job_event(
                conn, job_id,
                f"device '{job_row['target_device_id']}' disconnected mid-transfer -> INTERRUPTED",
            )
            return JobRunOutcome(job_id=job_id, status="INTERRUPTED", error=error)

        error = result.error or f"transfer of '{file.dest_relative_path}' did not complete ({result.status.value})"
        db.update_job_status(conn, job_id, "FAILED", bytes_done=bytes_done, error=error, finished_at=db.now_iso())
        db.log_job_event(conn, job_id, f"failed: {error}")
        return JobRunOutcome(job_id=job_id, status="FAILED", error=error)

    if any_unverified:
        db.update_job_status(conn, job_id, "DONE_UNVERIFIED", bytes_done=bytes_done, finished_at=db.now_iso())
        db.log_job_event(
            conn, job_id,
            f"done_unverified, {len(manifest.files)} file(s) -- transport accepted, "
            "installation result not confirmed via MTP",
        )
        return JobRunOutcome(job_id=job_id, status="DONE_UNVERIFIED")

    db.update_job_status(conn, job_id, "DONE", bytes_done=bytes_done, finished_at=db.now_iso())
    db.log_job_event(conn, job_id, f"done, {len(manifest.files)} file(s)")
    return JobRunOutcome(job_id=job_id, status="DONE")


def run_worker_loop(
    conn: sqlite3.Connection, registry: DeviceRegistry, *, poll_interval_seconds: float = 5.0,
) -> None:
    """Manual/CLI convenience: repeatedly calls run_worker_once(), sleeping
    between empty passes. Not itself tested (mirrors cli.py's _run_watch,
    which is the same kind of thin infinite-loop wrapper around a
    fully-tested single-pass function) -- run_worker_once is where the
    actual logic and its test coverage live."""
    import time

    from .mtp.windows import device_fingerprint

    fingerprints = [device_fingerprint(d) for d in registry.known_device_ids()]
    print(f"Queue worker started. Known devices: {fingerprints} (Ctrl+C to stop)")
    while True:
        outcome = run_worker_once(conn, registry)
        if outcome is not None:
            print(f"job {outcome.job_id}: {outcome.status}" + (f" ({outcome.error})" if outcome.error else ""))
        else:
            time.sleep(poll_interval_seconds)
