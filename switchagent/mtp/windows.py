"""Real Windows MTP backend -- implements MtpBackend against an actual
Nintendo Switch running DBI's MTP Responder.

TWO TRANSPORTS live here. File bytes go out over WPD (IPortableDevice, see
switchagent/mtp/wpd.py); device discovery, storage listing and the
installed-games CSV read still go through pywin32's Shell.Application, which
is proven and cheap for those. The Shell's IFileOperation copy engine
remains as an automatic per-connection fallback for the bytes themselves,
and is still the path used for an explicit overwrite.

Why the transport moved (2026-09-18/19, docs/PERF-MTP.md): IFileOperation
took 68.5s for a 123 MiB .nsp that DBI's own screen reported installing in
5s, reproducibly, while every Shell enumeration around it measured in
milliseconds -- so the minute was spent inside PerformOperations() after the
console had already finished. Streaming the identical file to the identical
console over WPD took 5.97s (3.28s of bytes at 37.6 MB/s, 2.69s for the
device to finalise). WPD also reports progress as it writes, which
IFileOperation cannot do at all.

Everything below is the direct, production translation of what Stage 5A /
5A.1 / 5B's hands-on experiments established on real hardware (see
docs/STAGE5A-MTP-RESEARCH.md, docs/STAGE5B-REAL-MTP.md). All of it still
describes the Shell fallback exactly; the notes about shell-cache visibility
lag are specifically what the WPD path does not suffer from, since it reads
object properties live rather than through the shell namespace:

  - device_id = FolderItem.Path (stable across reconnect, confirmed on two
    distinct physical consoles -- see docs/STAGE5A-MTP-RESEARCH.md).
  - Folder.CopyHere() does not reliably complete for this MTP responder
    (tested: queued without error, file never appeared within 60s).
  - SHFileOperation() cannot resolve a WPD virtual path at all
    (ERROR_BAD_PATHNAME) -- string-based path resolution does not work for
    this namespace, in any API that uses it.
  - IFileOperation.CopyItem() DOES work, but only when the destination
    IShellItem is built from the PIDL of an already-live Shell object
    (SHGetIDListFromObject -> SHCreateItemFromIDList) -- never from
    re-parsing a path string (SHCreateItemFromParsingName on a WPD path
    fails with E_INVALIDARG, confirmed).
  - PerformOperations() returning is NOT completion: in the successful
    smoke test, the call returned in ~0.5s but the file only became
    observable ~2.5s later. This backend never reports COMPLETED without a
    separate post-transfer poll confirming the destination file exists and
    its size has stabilized at the expected value.
  - SD_INSTALL (DBI's virtual "install" node) does NOT behave like a real
    filesystem: a transfer later physically confirmed (on the console's own
    screen) to have installed correctly reported System.Size == 0 for its
    entire observable lifetime (see docs/STAGE5B-REAL-MTP.md, "Install
    smoke-test"). Using size-stabilization as a pass/fail signal there, the
    same way it correctly works for SD_CARD, produced a proven false
    negative. send_file() now branches on SIZE_VERIFIABLE_STORAGES: real
    filesystem-like storages keep the size-based
    verify_transfer_completion() check; install-like storages use the
    presence-only verify_install_transport() and can return
    TransferStatus.UNVERIFIED -- "transport accepted, DBI-side result
    unprovable" -- which is never treated as COMPLETED and never treated as
    FAILED by callers.
  - IFileOperation.CopyItem() called on a whole FOLDER (not one file at a
    time), targeting a destination that already has a same-named folder,
    does NOT merge the way Explorer merges two same-named folders on a
    real filesystem. Confirmed on real hardware 2026-09-18
    (tools/mtp_folder_merge_test.py, 3 sequential folder uploads into the
    same destination folder, verified both by size and by downloading
    every file back and comparing its SHA-256): the SECOND and THIRD
    folder-level copies silently deleted every file from the PRIOR
    upload(s) that wasn't also present in the new one -- e.g. after
    uploading {alpha, beta, sub/gamma, sub/delta} and then {epsilon, zeta,
    sub/eta, sub/theta} into the same destination folder, alpha/beta/
    sub-gamma/sub-delta were simply gone. No exception was raised and
    GetAnyOperationsAborted() reported False every time -- this failure is
    completely silent. This is why every transfer in this backend is
    per-file, with its own existence check, never a whole-folder copy: a
    MOD_FOLDER install into an atmosphere/contents/<title_id> folder that
    already has content from a previous mod would otherwise silently wipe
    that previous mod's files.

One addition was made to switchagent/mtp/base.py: TransferStatus gained an
UNVERIFIED member (see that module's docstring) for exactly this "cannot
prove either way" case. send_file() later gained an optional `progress`
callback there too. Nothing else changed -- this class still implements the
ABC's 9 methods exactly as declared.

What did NOT change with the new transport, deliberately: SD_INSTALL still
never reports COMPLETED (a successful Commit() proves the device accepted
and finalised the object, not that DBI installed it), SD_CARD is still
size-verified before COMPLETED, an existing destination file is still never
overwritten without an explicit Override, and a job is still never sent to
a device other than the one it was created for -- a WPD device is addressed
by the PnP id embedded in that job's own recorded device_id
(wpd.pnp_id_from_device_id), never by picking a device off the WPD list.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import wpd
from .. import title_id as title_id_mod
from .base import DeviceInfo, MtpBackend, StorageInfo, TransferResult, TransferStatus
from .errors import (
    DestinationNotFoundError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    FileAlreadyExistsError,
    InvalidOperationError,
    StorageNotFoundError,
    TransferFailedError,
)

log = logging.getLogger("switchagent.mtp.windows")

THIS_PC_NAMESPACE = 0x11

# Copy flags for IFileOperation.SetOperationFlags -- silent, no UI, no
# confirmation dialogs. Same spirit as the flags already proven in
# Projects/Switch/sync-to-switch.ps1's CopyHere() calls (docs/RESEARCH-STAGE1.md),
# translated to the FOF_* constants IFileOperation also accepts.
_FOF_SILENT = 4
_FOF_NOCONFIRMATION = 16
_FOF_NOERRORUI = 1024
COPY_OPERATION_FLAGS = _FOF_SILENT | _FOF_NOCONFIRMATION | _FOF_NOERRORUI

# Transport selection. True: stream files ourselves over WPD
# (switchagent/mtp/wpd.py) and keep the Shell's IFileOperation as the
# automatic fallback. Measured on real hardware 2026-09-18, same 123 MiB
# .nsp, same console, same cable: Shell 68.5s, WPD 5.97s -- and DBI's own
# screen reported the install finished in 5s, so the ~60s difference was
# time the Shell spent after the console was already done.
#
# SWITCHAGENT_TRANSPORT=shell puts every transfer back on the Shell path
# without a rebuild -- an escape hatch that works on a packaged, installed
# copy, which flipping this constant does not.
WPD_TRANSPORT_ENABLED = os.environ.get("SWITCHAGENT_TRANSPORT", "wpd").strip().lower() != "shell"

DEFAULT_VERIFY_TIMEOUT_SECONDS = 60.0
# 2026-09-17 real-hardware finding: a 45-file MOD_FOLDER job (2.3MB total --
# a translation mod, all tiny text/font files) to SD_CARD took 6m12s, i.e.
# ~8.3s PER FILE for a transfer that should be near-instant. Root-caused via
# the job's own install log + manifest (job id 25, library item 1): with the
# old 2.0s interval, the shell-cache visibility lag documented above (~2.5s
# after PerformOperations() returns) means the FIRST poll at t=2.0s almost
# always misses, so reaching stable_reads_required=3 actually costs 4 poll
# iterations, not 3 -- 4 * 2.0s = 8.0s, matching the observed 8.3s/file
# almost exactly. That lag is the shell namespace CACHE catching up, not an
# in-progress write -- PerformOperations() already blocked until the actual
# bytes were written, so polling faster carries no risk of observing a
# half-written file. verify_install_transport() below already polls every
# 0.25s in production for exactly this kind of "is it visible yet" check;
# this brings SD_CARD's cadence in line with that proven-safe precedent,
# without weakening stable_reads_required at all -- same confidence bar,
# just detected sooner. Cuts the same 45-file job to roughly 3s/file.
DEFAULT_VERIFY_POLL_INTERVAL_SECONDS = 0.25
DEFAULT_STABLE_READS_REQUIRED = 3

# SD_INSTALL is a virtual DBI node, not a real filesystem -- proven on real
# hardware (docs/STAGE5B-REAL-MTP.md, "Install smoke-test") that
# System.Size is NOT a meaningful completion signal there: a transfer later
# physically confirmed (on the console's own screen) to have installed
# correctly reported System.Size == 0 for its entire observable lifetime.
# So install verification only confirms the destination NAME appeared
# (transport accepted), never compares size -- see verify_install_transport.
DEFAULT_INSTALL_VERIFY_TIMEOUT_SECONDS = 2.0
DEFAULT_INSTALL_PRESENCE_READS_REQUIRED = 2

# Logical storage names (see logical_storage_name() below) for which
# destination size IS a meaningful, verifiable signal -- real filesystem-like
# storages. Anything not in this set is treated as install-like: verified by
# presence only, never by size (see send_file()'s branch and
# verify_install_transport()). Deliberately an allow-list, not a deny-list
# for "SD_INSTALL" alone -- NAND_INSTALL is DBI's other virtual install
# target and shares the same unverifiable-size characteristic, even though
# it has never been physically tested (no NAND writes performed by this
# project -- see docs/STATE.md safety rules); allow-listing only the
# storages actually proven filesystem-like is the conservative default.
SIZE_VERIFIABLE_STORAGES = frozenset({"SD_CARD", "NAND_USER", "NAND_SYSTEM", "SAVES", "ALBUM"})

# The largest existing destination file send_file() will read back to see
# whether it already holds exactly the bytes being sent (expected_sha256).
# A homebrew port's data is thousands of files of a few KB; reading a
# multi-GB file back just to compare it is not worth it -- that stays a
# conflict, as before.
READ_BACK_MAX_BYTES = 64 * 1024 * 1024


class _WpdWriteFailed(Exception):
    """WPD failed while writing an object this very call had just created, so
    a half-written object of our OWN may now sit at the destination -- the
    one thing a retry of the same file may replace."""

    def __init__(self, cause: BaseException):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause


_ALREADY_THERE = object()  # _send_via_wpd: the destination already holds these exact bytes

_SERIAL_SEGMENT_RE = re.compile(r"(usb#vid_[0-9a-f]{4}&pid_[0-9a-f]{4}#)([^#]+)(#\{)", re.IGNORECASE)


def mask_device_id(device_id: Optional[str]) -> str:
    """Redacts the serial-like segment of a device_id for log messages --
    same technique as tools/mtp_probe.py's mask_serial(), duplicated here
    (not imported) so switchagent/ stays independent of tools/, which is a
    diagnostic-only area outside the package."""
    if not device_id:
        return repr(device_id)
    return _SERIAL_SEGMENT_RE.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", device_id)


def has_placeholder_serial(device_id: Optional[str]) -> bool:
    """True when this device_id's USB serial segment carries no real serial
    at all -- every digit in it is a zero.

    Real-hardware finding (2026-09-18): a Switch plugged in fresh spends a
    few seconds enumerating as name "Nintendo Switch" with the serial
    `XAW00000000000` before it settles into its own identity (DBI's
    responder, name "Switch", real serial). Two polls, ~3 seconds, then
    gone. SwitchAgent recorded that transient as a permanent third device
    in the Devices list.

    Refusing it is not cosmetic. The SAME zeroed id was produced by two
    DIFFERENT physical consoles a week apart (the user's OLED on
    2026-09-11/16, their child's Switch on 2026-09-18) -- it is not an
    identity, it is a placeholder every console passes through. This
    project's core promise is that it "never substitutes a different
    target device than the one a job was created for" (README), and a
    device_id that collides across consoles cannot support that promise.
    So such a device is never enumerated, never registered, never
    recorded, and never a transfer target -- within seconds the same
    console reappears under its real id anyway.
    """
    if not device_id:
        return False
    match = _SERIAL_SEGMENT_RE.search(device_id)
    if match is None:
        return False
    digits = [ch for ch in match.group(2) if ch.isdigit()]
    return bool(digits) and all(ch == "0" for ch in digits)


def device_fingerprint(device_id: str) -> str:
    """Safe-to-log stand-in for a device_id -- sha256[:16], same as
    tools/mtp_probe.py's device_fingerprint()."""
    return hashlib.sha256(device_id.encode("utf-8", "surrogateescape")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Pure logic: storage name resolution -- no COM involved, fully unit-testable
# ---------------------------------------------------------------------------

# Ordered most-specific-first: "SD Card install" must be checked before the
# plain "SD Card" pattern, or every install storage would incorrectly match
# SD_CARD first (confirmed real display name from Stage 5A/5A.1: "5: SD Card
# install" -- but never hardcode the "5:" prefix, only the name text).
_STORAGE_NAME_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SD_INSTALL", ("sd card install", "sd install")),
    ("SD_CARD", ("sd card",)),
    ("NAND_INSTALL", ("nand install",)),
    ("NAND_USER", ("nand user",)),
    ("NAND_SYSTEM", ("nand system",)),
    ("SAVES", ("saves",)),
    ("ALBUM", ("album",)),
    ("INSTALLED_GAMES", ("installed games",)),
)


def parse_installed_applications_csv(text: str) -> set[str]:
    """Parses DBI's own "InstalledApplications.csv" (found inside its
    "Installed games" MTP node, real-hardware confirmed 2026-09-15) --
    one row per installed content unit (base game OR one specific DLC; an
    installed UPDATE bumps its base row's own version column instead of
    adding a row) as "0x<TITLE_ID>,<version>,\"<display name>\"". Returns
    the set of recovered BASE title ids (title_id.classify_title_variant)
    -- the exact same base_title_id every library family is already
    grouped and displayed under (services.family_base_title_id), so a
    caller only needs one set-membership check per family, never a name
    comparison. Malformed/unparseable rows are skipped, never raised on --
    a partial read is better than none."""
    base_ids: set[str] = set()
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        candidate = row[0].strip()
        if candidate.lower().startswith("0x"):
            candidate = candidate[2:]
        if title_id_mod.is_valid_title_id(candidate):
            base_ids.add(title_id_mod.classify_title_variant(candidate.upper()).base_title_id)
    return base_ids


def logical_storage_name(display_name: str) -> str:
    """Maps a DBI-displayed storage name (e.g. "1: SD Card", "5: SD Card
    install") to our logical alias (e.g. "SD_CARD", "SD_INSTALL"). Matches
    by content, never by the leading "N:" index -- that index is not stable
    across DBI versions/configs (confirmed: one real console had 7 storage
    nodes, another had 8, with different numbering -- see
    docs/STAGE5A-MTP-RESEARCH.md). Falls back to a generated identifier for
    anything unrecognized, so list_storages() never silently drops a node."""
    lowered = display_name.lower()
    for logical_name, patterns in _STORAGE_NAME_PATTERNS:
        if any(p in lowered for p in patterns):
            return logical_name
    fallback = re.sub(r"^\d+:\s*", "", display_name).strip().upper().replace(" ", "_")
    return fallback or "UNKNOWN"


def resolve_storage_name(display_name: str, overrides: Optional[dict[str, str]] = None) -> str:
    """UI-007: the manual-override-aware counterpart to
    logical_storage_name() above -- an explicit user mapping (see
    device_storage_mappings in db.py, and Devices page's manual override
    UI) always wins over the automatic pattern match, for the rare case
    where DBI reports a raw display name none of the patterns above
    recognize (logical_storage_name()'s own "generated identifier"
    fallback). Exact raw display-name match only -- never fuzzy, never
    merges with the pattern list. Falls back to logical_storage_name()
    unchanged when no override applies (overrides=None/empty, or this
    specific display_name has no entry), so existing AUTO-only behavior is
    completely unaffected until a user explicitly sets one."""
    if overrides and display_name in overrides:
        return overrides[display_name]
    return logical_storage_name(display_name)


def split_dest_path(dest_path: str) -> tuple[str, str]:
    """('atmosphere/contents/ID/romfs/x.bin') -> ('atmosphere/contents/ID/romfs', 'x.bin')
    ('game.nsp') -> ('', 'game.nsp')"""
    if "/" not in dest_path:
        return "", dest_path
    parent, _, name = dest_path.rpartition("/")
    return parent, name


# ---------------------------------------------------------------------------
# Pure-ish logic: post-transfer completion verification state machine.
# COM interaction is fully injected via `poll_fn` so this is unit-testable
# with a scripted sequence of fake readings -- no real device, no real
# waiting required in tests.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PollReading:
    device_present: bool
    file_exists: bool
    size: Optional[int]


def verify_transfer_completion(
    poll_fn: Callable[[], PollReading],
    *,
    expected_size: int,
    timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_VERIFY_POLL_INTERVAL_SECONDS,
    stable_reads_required: int = DEFAULT_STABLE_READS_REQUIRED,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> tuple[TransferStatus, int, Optional[str]]:
    """The honesty gate: PerformOperations() returning is never treated as
    completion (confirmed on real hardware: the call returned in ~0.5s, the
    file only became observable ~2.5s later -- see docs/STAGE5B-REAL-MTP.md).
    This polls poll_fn() until the destination file is seen with a size that
    matches expected_size and stays stable for `stable_reads_required`
    consecutive reads, or until timeout/disconnect. Returns
    (status, bytes_observed, error_message).

    - Device disappears mid-poll -> DEVICE_DISCONNECTED (matches
      queue_worker.py's existing INTERRUPTED mapping for this status).
    - Times out having NEVER once observed the destination at all -> not
      COMPLETED, but UNVERIFIED rather than FAILED. Seen on real hardware
      with small SD_CARD mod files that copied successfully within seconds
      on the console but were never once seen by poll_fn() within the
      full timeout window, immediately after ensure_directory() had just
      created their parent folder -- consistent with a just-created MTP
      folder listing not yet reflecting a file added moments later. The
      transport call itself completed without raising, so this is the
      same honest "cannot prove it, but not reporting a failure that
      likely didn't happen" call SD_INSTALL already makes (see
      verify_install_transport) -- never claims COMPLETED, just refuses to
      claim FAILED for something never observed even once.
    - Times out HAVING observed the file (just never at a stable, matching
      size) -> FAILED with a clear "verification timed out" message -- we
      saw something concrete and it didn't add up, so this stays a real
      failure signal, not a shrug.
    - Stabilizes at a size that does NOT match expected_size -> FAILED,
      never COMPLETED -- this backend does not use a remote SHA-256 check
      (real MTP gives no cheap way to compute one), only size comparison,
      exactly as scoped."""
    deadline = clock_fn() + timeout_seconds
    last_size: Optional[int] = None
    stable_count = 0
    last_observed_size = 0
    ever_observed = False

    while clock_fn() < deadline:
        sleep_fn(poll_interval_seconds)
        reading = poll_fn()

        if not reading.device_present:
            return TransferStatus.DEVICE_DISCONNECTED, last_observed_size, "device disconnected during verification"

        if not reading.file_exists:
            stable_count = 0
            last_size = None
            continue

        ever_observed = True

        if reading.size is not None:
            last_observed_size = reading.size

        if reading.size is None:
            stable_count = 0
        elif reading.size == last_size:
            stable_count += 1
        else:
            # First observation of a (possibly new) value counts as the
            # first of the run, not zero -- otherwise stable_reads_required
            # consecutive matching reads would actually need N+1 reads to
            # trigger, which is not what the parameter name promises.
            stable_count = 1
        last_size = reading.size

        if stable_count >= stable_reads_required:
            if last_size == expected_size:
                return TransferStatus.COMPLETED, last_size, None
            return (
                TransferStatus.FAILED,
                last_observed_size,
                f"size stabilized at {last_size}, expected {expected_size} -- not reporting success",
            )

    if not ever_observed:
        return (
            TransferStatus.UNVERIFIED,
            last_observed_size,
            f"transport call completed without error, but the destination was never observed within "
            f"{timeout_seconds}s -- likely an MTP/Shell folder-listing lag on this device, not a failed "
            "transfer; installation result cannot be verified via MTP on this device",
        )

    return (
        TransferStatus.FAILED,
        last_observed_size,
        f"verification timed out after {timeout_seconds}s without a stable, matching size",
    )


def verify_install_transport(
    poll_fn: Callable[[], PollReading],
    *,
    timeout_seconds: float = DEFAULT_INSTALL_VERIFY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 0.25,
    presence_reads_required: int = DEFAULT_INSTALL_PRESENCE_READS_REQUIRED,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> tuple[TransferStatus, Optional[int], Optional[str]]:
    """The SD_INSTALL counterpart to verify_transfer_completion() -- for a
    virtual install node, NOT a real filesystem. Deliberately does not
    compare size against anything: real hardware evidence
    (docs/STAGE5B-REAL-MTP.md) shows a genuinely, physically successful
    install can report System.Size == 0 throughout, so treating size as a
    pass/fail signal here would be a proven false negative, not caution.

    This confirms the destination NAME appears and stays present for
    `presence_reads_required` consecutive polls (guards against a transient
    Explorer/shell cache blip, nothing more) and returns UNVERIFIED, never
    COMPLETED -- this function has no way to confirm DBI actually finished
    installing, only that the device accepted something under that name.

    A name that never appears at all before timeout is now ALSO reported as
    UNVERIFIED, not FAILED (real-hardware finding, 2026-09-12, reproduced on
    two separate real installs with near-identical timing): this function
    is only ever reached after `PerformOperations()` has already returned
    without an aborted-operations signal (see send_file()'s caller) -- i.e.
    the transport call itself already succeeded. On real hardware, that
    blocking call's own duration (source.stat().st_size ~570 MB, elapsed
    ~46s total including this function's own polling) was close enough to
    DBI's own self-reported install time (15s, confirmed on the console
    screen both times) that the virtual placeholder this function polls for
    may already have been created AND removed by DBI entirely WITHIN the
    `PerformOperations()` call, before this function's very first poll ever
    runs -- meaning no timeout, however long, and no poll interval, however
    short, can guarantee catching it. Given that, "the name was never
    observed" no longer safely proves "the transport was not accepted" (the
    same class of mistake the SD_INSTALL semantics fix already corrected
    once for `System.Size == 0` -- this is that same lesson applied to
    presence-timeout too, not a new, separate concern). The only way this
    function's caller (`send_file()`) can still report `FAILED` for an
    install-like storage is a genuine COM-level abort/exception from
    `PerformOperations()` itself, handled entirely upstream of this
    function -- never from this function's own polling coming up empty.

    Device disappearing mid-poll -> DEVICE_DISCONNECTED, same mapping
    queue_worker.py already understands (-> INTERRUPTED) -- this is a
    distinct, still-legitimate signal (the device itself vanished, not
    merely "the name wasn't seen"), unaffected by the change above."""
    deadline = clock_fn() + timeout_seconds
    presence_count = 0
    last_observed_size: Optional[int] = None
    observed_presence_at_least_once = False

    while clock_fn() < deadline:
        sleep_fn(poll_interval_seconds)
        reading = poll_fn()

        if not reading.device_present:
            return TransferStatus.DEVICE_DISCONNECTED, last_observed_size, "device disconnected during verification"

        if not reading.file_exists:
            presence_count = 0
            continue

        observed_presence_at_least_once = True
        last_observed_size = reading.size
        presence_count += 1
        if presence_count >= presence_reads_required:
            return (
                TransferStatus.UNVERIFIED,
                last_observed_size,
                "device accepted the transfer (destination name present under SD Card install); "
                "DBI-side installation result cannot be verified via MTP on this device -- "
                "System.Size is not a meaningful signal for this virtual node, see "
                "docs/STAGE5B-REAL-MTP.md",
            )

    detail = (
        "destination observed but not stably present" if observed_presence_at_least_once
        else "destination name never observed"
    )
    return (
        TransferStatus.UNVERIFIED,
        last_observed_size,
        f"transport call completed without error, but {detail} under SD Card install within "
        f"{timeout_seconds}s -- DBI may already have received and installed it before this check "
        "could observe the virtual placeholder (see docs/STATE.md, 2026-09-12 real-hardware finding); "
        "installation result cannot be verified via MTP on this device",
    )


# ---------------------------------------------------------------------------
# COM-touching backend
# ---------------------------------------------------------------------------

class RealMtpBackend(MtpBackend):
    """One instance = one physical device, identified by `device_id`
    (a FolderItem.Path string -- see mtp/base.py's module docstring on
    device identity, and docs/STAGE5A-MTP-RESEARCH.md for why .Path is the
    chosen source of truth). Never auto-selects "the first" device, never
    matches by display name alone."""

    def __init__(self, device_id: str):
        if not device_id:
            raise ValueError("device_id is required -- RealMtpBackend never guesses which Switch to use")
        self._device_id = device_id
        self._connected = False
        self._device_item = None  # live win32com FolderItem, set by connect()
        self._device_folder = None  # live Folder (device_item.GetFolder)
        self._transfers: dict[str, TransferResult] = {}
        self._storage_overrides: dict[str, str] = {}  # UI-007, see set_storage_overrides()
        # Transport state -- see _wpd_session(). The Shell path this class
        # shipped with is never removed, only demoted to the fallback: any
        # WPD fault sets _wpd_unavailable and the connection finishes its
        # work exactly the way it used to.
        self._wpd = None
        self._wpd_unavailable = not WPD_TRANSPORT_ENABLED
        self._wpd_storage_ids: dict[str, str] = {}

    @property
    def device_id(self) -> str:
        """Configured physical target; available before connecting."""
        return self._device_id

    def set_storage_overrides(self, overrides: dict[str, str]) -> None:
        """Replaces the whole override set each call (not merged), so a
        cleared mapping (its device_storage_mappings row deleted) is
        correctly forgotten on the very next refresh -- never queries
        SQLite itself, see MtpBackend.set_storage_overrides()'s own
        docstring."""
        self._storage_overrides = dict(overrides)

    # -- internal: live COM access, isolated so the rest of the class reads cleanly --

    @staticmethod
    def _shell():
        import win32com.client
        return win32com.client.Dispatch("Shell.Application")

    def _find_device_item(self):
        this_pc = self._shell().NameSpace(THIS_PC_NAMESPACE)
        candidates = [i for i in this_pc.Items() if i.IsFolder and not i.IsFileSystem]
        return _match_device_by_id(candidates, self._device_id)

    def _device_currently_present(self) -> bool:
        return self._find_device_item() is not None

    def _require_connected(self) -> None:
        if not self.is_connected:
            raise DeviceDisconnectedError(
                f"device {mask_device_id(self._device_id)} is not connected -- call connect() first"
            )

    def _get_storage_item(self, storage: str):
        self._require_connected()
        for child in self._device_folder.Items():
            if resolve_storage_name(child.Name, self._storage_overrides) == storage:
                return child
        raise StorageNotFoundError(f"storage {storage!r} not found on device {mask_device_id(self._device_id)}")

    def _navigate(self, root_item, path: str, *, create_missing: bool):
        """Walks `path` (posix-style, "" meaning the root itself) from
        root_item, one segment at a time -- never a whole-tree operation,
        matching the merge-dialog pitfall already documented in
        docs/RESEARCH-STAGE1.md. create_missing=True calls NewFolder() for
        segments that don't exist yet and confirms each one appeared before
        descending further (mirrors the proven poll-after-NewFolder pattern
        from the Stage 5B write experiments) -- never assumes success just
        because NewFolder() didn't raise."""
        current = root_item
        if not path:
            return current
        for segment in path.split("/"):
            if not segment:
                continue
            folder = current.GetFolder
            child = next((i for i in folder.Items() if i.Name == segment), None)
            if child is None:
                if not create_missing:
                    raise DestinationNotFoundError(
                        f"'{segment}' does not exist under the requested path -- call ensure_directory() first"
                    )
                folder.NewFolder(segment)
                child = self._poll_for_child(folder, segment)
                if child is None:
                    raise InvalidOperationError(
                        f"NewFolder({segment!r}) did not raise, but the folder never became visible -- "
                        "not reporting success"
                    )
            elif not child.IsFolder:
                raise DestinationNotFoundError(f"'{segment}' exists but is a file, not a directory")
            current = child
        return current

    @staticmethod
    def _poll_for_child(folder, name: str, *, attempts: int = 10, interval: float = 0.5):
        for _ in range(attempts):
            match = next((i for i in folder.Items() if i.Name == name), None)
            if match is not None:
                return match
            time.sleep(interval)
        return None

    @staticmethod
    def _to_ishell_item(folder_item):
        """The PIDL bridge, confirmed the only reliable way to get an
        IShellItem for a WPD virtual object (docs/STAGE5B-REAL-MTP.md):
        SHCreateItemFromParsingName() on the .Path STRING fails with
        E_INVALIDARG for this namespace -- deliberately not used here."""
        import win32com.shell.shell as shell_api

        pidl = shell_api.SHGetIDListFromObject(folder_item)
        return shell_api.SHCreateItemFromIDList(pidl, shell_api.IID_IShellItem)

    # -- MtpBackend: connection lifecycle -----------------------------------

    def connect(self) -> DeviceInfo:
        item = self._find_device_item()
        if item is None:
            self.disconnect()
            raise DeviceNotFoundError(f"device {mask_device_id(self._device_id)} not currently reachable")
        self._device_item = item
        self._device_folder = item.GetFolder
        if not self._connected:
            self._read_session_token = uuid.uuid4().hex
            self._wpd_unavailable = not WPD_TRANSPORT_ENABLED
        self._connected = True
        log.info("connected device=%s name=%r", device_fingerprint(self._device_id), item.Name)
        return DeviceInfo(device_id=self._device_id, name=item.Name, connected=True)

    def disconnect(self) -> None:
        self._close_wpd()
        # Release live references -- deliberately does NOT call
        # pythoncom.CoUninitialize() here: that would tear down the COM
        # apartment for the whole calling thread, which could break
        # unrelated COM usage elsewhere in the same process/thread. Letting
        # Python's normal refcounting release these specific COM objects is
        # sufficient and safer.
        self._device_item = None
        self._device_folder = None
        self._connected = False
        log.info("disconnected device=%s", device_fingerprint(self._device_id))

    @property
    def is_connected(self) -> bool:
        # A real, live presence check every time -- never a cached flag on
        # its own (see docs/STAGE5B-REAL-MTP.md, is_connected()). Both must
        # hold: we haven't explicitly disconnected, AND the device is
        # actually still there right now.
        if self._connected and not self._device_currently_present():
            self.disconnect()
        return self._connected

    def capabilities(self):
        save_write = False
        if self.is_connected:
            session = self._wpd_session()
            if session is not None:
                try:
                    save_write = len(self._known_save_storages(session)) == 1
                except Exception:
                    save_write = False
        return {'read_files': not self._wpd_unavailable, 'exact_restore': False,
                'verified_save_identity': False,
                'save_write': save_write}

    def _known_save_storages(self, session):
        return [object_id for object_id, raw_name in session.storages()
                if (logical_storage_name(raw_name) == 'SAVES'
                    and resolve_storage_name(raw_name, self._storage_overrides) == 'SAVES')]

    def _save_write_context(self, storage, save_root, path, expected_session):
        from .base import save_write_path
        from .errors import AmbiguousPathError, UnsupportedOperationError

        if not expected_session:
            raise InvalidOperationError('save write requires a pinned session')
        root, target = save_write_path(storage, save_root, path)
        token = self._check_read_session(expected_session)
        session = self._wpd_session()
        if session is None:
            raise UnsupportedOperationError('WPD save writing is unavailable; no Shell fallback')
        roots = self._known_save_storages(session)
        if not roots:
            raise UnsupportedOperationError('DBI Saves storage is not uniquely identified')
        if len(roots) != 1:
            raise AmbiguousPathError('multiple DBI Saves storages')
        root_id = session.resolve_read_path(roots[0], root)
        props = wpd.read_props(session._properties, root_id)
        if props.get(str(wpd.WPD_OBJECT_CONTENT_TYPE)) != str(wpd.WPD_CONTENT_TYPE_FOLDER):
            raise InvalidOperationError('save root is not a known directory')
        parent_path, name = target.rsplit('/', 1)
        parent_relative = parent_path[len(root):].lstrip('/')
        parent_id = session.resolve_read_path(root_id, parent_relative)
        parent_props = wpd.read_props(session._properties, parent_id)
        if parent_props.get(str(wpd.WPD_OBJECT_CONTENT_TYPE)) != str(wpd.WPD_CONTENT_TYPE_FOLDER):
            raise InvalidOperationError('save parent is not a known directory')
        return session, root_id, parent_id, name, target, token

    @staticmethod
    def _save_child(session, parent_id, name):
        from .errors import AmbiguousPathError

        matches = [(obj, props) for obj, props in session.children(parent_id)
                   if wpd.object_name(props) == name]
        if len(matches) > 1:
            raise AmbiguousPathError(f'ambiguous save object: {name}')
        return matches[0] if matches else None

    def _save_failure(self, exc):
        if isinstance(exc, wpd.ComError):
            self.disconnect()
            raise TransferFailedError(f'{exc}; save target state unknown') from exc
        self._read_failure(exc)

    def delete_save_object(self, storage, save_root, path, *, expected_session,
                           recursive=False) -> None:
        try:
            if not isinstance(recursive, bool):
                raise InvalidOperationError('recursive must be explicit boolean')
            session, _, parent_id, name, target, token = self._save_write_context(
                storage, save_root, path, expected_session)
            found = self._save_child(session, parent_id, name)
            if found is None:
                raise DestinationNotFoundError(target)
            entry = self._read_entry(session, target, found[0], found[1], token)
            if not entry.metadata['type_known']:
                raise InvalidOperationError('save object type is not known')
            if entry.is_dir and not recursive and session.children(found[0]):
                raise InvalidOperationError('save directory is not empty')
            session.delete_save_object(parent_id, found[0], recursive=recursive)
            self._check_read_session(token)
            if self._save_child(session, parent_id, name) is not None:
                raise TransferFailedError('save object remains after delete; target state unknown')
        except Exception as exc:
            self._save_failure(exc)

    def create_save_directory(self, storage, save_root, path, *, expected_session) -> None:
        try:
            session, _, parent_id, name, target, token = self._save_write_context(
                storage, save_root, path, expected_session)
            if self._save_child(session, parent_id, name) is not None:
                raise FileAlreadyExistsError(target)
            session.create_save_directory(parent_id, name)
            self._check_read_session(token)
            found = self._save_child(session, parent_id, name)
            if found is None or found[1].get(str(wpd.WPD_OBJECT_CONTENT_TYPE)) != str(wpd.WPD_CONTENT_TYPE_FOLDER):
                raise TransferFailedError('save directory not confirmed; target state unknown')
        except Exception as exc:
            self._save_failure(exc)

    def write_save_file(self, storage, save_root, path, source_path, *, replace,
                        expected_session, cancel=None, progress=None) -> None:
        from .reading import is_link_or_reparse

        try:
            source = Path(source_path)
            if not source.is_file() or any(is_link_or_reparse(item) for item in (source, *source.parents)):
                raise InvalidOperationError('restore source is not a regular unlinked file')
            if not isinstance(replace, bool):
                raise InvalidOperationError('replace must be explicit boolean')
            if cancel and cancel():
                from .errors import OperationCancelledError
                raise OperationCancelledError('save write cancelled before transfer')
            session, root_id, parent_id, name, target, token = self._save_write_context(
                storage, save_root, path, expected_session)
            found = self._save_child(session, parent_id, name)
            if found is not None:
                entry = self._read_entry(session, target, found[0], found[1], token)
                if not replace or entry.is_dir or not entry.metadata['type_known']:
                    raise FileAlreadyExistsError(target)
                session.delete_save_object(parent_id, found[0], recursive=False)
                # Deletion is itself a write. Re-resolve before file creation.
                fresh = self._save_write_context(storage, save_root, path, token)
                session, new_root, new_parent, name, target, token = fresh
                if (new_root, new_parent) != (root_id, parent_id):
                    raise TransferFailedError('save parent changed after delete; target state unknown')
                parent_id = new_parent
                if self._save_child(session, parent_id, name) is not None:
                    raise TransferFailedError('old save file remains after delete; target state unknown')
            if cancel and cancel():
                from .errors import OperationCancelledError
                raise OperationCancelledError('save write cancelled before transfer')
            session.write_save_file(parent_id, name, source,
                                    check_session=lambda: self._check_read_session(token),
                                    cancel=cancel, progress=progress)
            self._check_read_session(token)
            found = self._save_child(session, parent_id, name)
            if (found is None or found[1].get(str(wpd.WPD_OBJECT_CONTENT_TYPE)) == str(wpd.WPD_CONTENT_TYPE_FOLDER)
                    or found[1].get(str(wpd.WPD_OBJECT_SIZE)) != source.stat().st_size):
                raise TransferFailedError('save file not confirmed after commit; target state unknown')
        except Exception as exc:
            self._save_failure(exc)

    def _read_objects(self, storage, path, expected_session):
        from .base import read_path
        from .errors import AmbiguousPathError, UnsupportedOperationError
        token = self._check_read_session(expected_session)
        read_path(path)
        session = self._wpd_session()
        if session is None:
            raise UnsupportedOperationError('WPD reading is unavailable; no Shell fallback')
        roots = [obj for obj, name in session.storages()
                 if resolve_storage_name(name, self._storage_overrides) == storage]
        if not roots:
            raise StorageNotFoundError(storage)
        if len(roots) != 1:
            raise AmbiguousPathError('multiple storages share this mapping')
        return session, session.resolve_read_path(roots[0], path), token

    @staticmethod
    def _read_entry(session, path, object_id, props, token):
        from . import wpd
        from .base import MtpEntry
        kind = props.get(str(wpd.WPD_OBJECT_CONTENT_TYPE))
        is_dir = kind == str(wpd.WPD_CONTENT_TYPE_FOLDER)
        type_known = kind in (str(wpd.WPD_CONTENT_TYPE_FOLDER),
                              str(wpd.WPD_CONTENT_TYPE_GENERIC_FILE))
        if kind == str(wpd.WPD_CONTENT_TYPE_UNSPECIFIED):
            # DBI exposes some save files as UNSPECIFIED without OBJECT_NAME.
            # That type alone does not prove a file; require the original file
            # name and exactly one DEFAULT resource.
            original = props.get(str(wpd.WPD_OBJECT_ORIGINAL_FILE_NAME))
            if (isinstance(original, str) and original and '/' not in original
                    and '\\' not in original):
                keys = session.supported_resources(object_id)
                type_known = sum(key == wpd.WPD_RESOURCE_DEFAULT for key in keys) == 1
        size = props.get(str(wpd.WPD_OBJECT_SIZE))
        if is_dir or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            size = None
        return MtpEntry(path, wpd.object_name(props) or '', is_dir, size, object_id,
                        {'type_known': type_known,
                         'wpd_properties': props}, token)

    def _read_failure(self, exc):
        from . import wpd
        if isinstance(exc, wpd.ComError):
            # Object handles must never survive a failed transport operation.
            self.disconnect()
            if exc.hr in (0x80070005, 0x80030005):
                from .errors import ReadAccessDeniedError
                raise ReadAccessDeniedError(str(exc)) from exc
            if exc.hr in (0x8007048F, 0x8007001F, 0x80010108):
                raise DeviceDisconnectedError(str(exc)) from exc
            raise TransferFailedError(str(exc)) from exc
        raise exc

    def list_directory(self, storage, path='', *, expected_session=None):
        from . import wpd
        try:
            session, object_id, token = self._read_objects(storage, path, expected_session)
            if path:
                props = wpd.read_props(session._properties, object_id)
                if props.get(str(wpd.WPD_OBJECT_CONTENT_TYPE)) != str(wpd.WPD_CONTENT_TYPE_FOLDER):
                    raise InvalidOperationError('source is not a known directory')
            result = []
            for child_id, props in session.children(object_id):
                name = wpd.object_name(props)
                if not isinstance(name, str) or not name or '/' in name or '\\' in name:
                    raise InvalidOperationError('object has an unusable name')
                child_path = f'{path}/{name}' if path else name
                result.append(self._read_entry(session, child_path, child_id, props, token))
            self._check_read_session(token)
            return result
        except Exception as exc:
            self._read_failure(exc)

    def stat(self, storage, path, *, expected_session=None):
        from . import wpd
        from .base import MtpEntry
        try:
            session, object_id, token = self._read_objects(storage, path, expected_session)
            if not path:
                return MtpEntry('', '', True, object_id=object_id, session_token=token)
            entry = self._read_entry(session, path, object_id,
                                     wpd.read_props(session._properties, object_id), token)
            self._check_read_session(token)
            return entry
        except Exception as exc:
            self._read_failure(exc)

    def receive_file(self, storage, source_path, destination_path, *, progress=None,
                     cancel=None, expected_session=None):
        from . import wpd
        try:
            session, object_id, token = self._read_objects(storage, source_path, expected_session)
            entry = self._read_entry(session, source_path, object_id,
                                     wpd.read_props(session._properties, object_id), token)
            if entry.is_dir or not entry.metadata['type_known']:
                raise InvalidOperationError('source is not a known file')
            return session.receive_file(object_id, destination_path, size=entry.size,
                                        progress=progress, cancel=cancel,
                                        check_session=lambda: self._check_read_session(token))
        except Exception as exc:
            self._read_failure(exc)

    # -- MtpBackend: storage --------------------------------------------------

    def list_storages(self) -> list[StorageInfo]:
        self._require_connected()
        result = []
        for child in self._device_folder.Items():
            free = _extended_property_int(child, "System.FreeSpace")
            total = _extended_property_int(child, "System.Capacity")
            result.append(StorageInfo(
                name=resolve_storage_name(child.Name, self._storage_overrides), writable=True,
                free_bytes=free, total_bytes=total, raw_name=child.Name,
            ))
        return result

    def get_storage(self, storage: str) -> StorageInfo:
        item = self._get_storage_item(storage)
        free = _extended_property_int(item, "System.FreeSpace")
        total = _extended_property_int(item, "System.Capacity")
        return StorageInfo(name=storage, writable=True, free_bytes=free, total_bytes=total, raw_name=item.Name)

    # -- MtpBackend: paths ---------------------------------------------------

    def exists(self, storage: str, path: str) -> bool:
        session = self._wpd_session()
        if session is not None:
            parent_path, name = split_dest_path(path)
            if not name:
                return True  # storage root always "exists"
            try:
                parent_id = session.navigate(self._wpd_storage_id(session, storage), parent_path,
                                             create_missing=False)
                return parent_id is not None and session.child_id(parent_id, name) is not None
            except StorageNotFoundError:
                raise
            except Exception as exc:  # noqa: BLE001 -- same fallback rule as send_file
                log.warning("WPD exists() failed (%s: %s) -- falling back to the Shell",
                            type(exc).__name__, exc)
                self._wpd_unavailable = True
                self._close_wpd()
        storage_item = self._get_storage_item(storage)
        parent_path, name = split_dest_path(path)
        if not name:
            return True  # storage root always "exists"
        try:
            parent = self._navigate(storage_item, parent_path, create_missing=False)
        except DestinationNotFoundError:
            return False
        return any(i.Name == name for i in parent.GetFolder.Items())

    def ensure_directory(self, storage: str, path: str) -> None:
        for attempt in (1, 2):
            session = self._wpd_session()
            if session is None:
                break
            try:
                # Must go through the SAME transport the following send_file
                # will navigate with: WpdSession caches a folder's children so
                # a 45-file mod doesn't re-enumerate its destination once per
                # file, and a folder created behind that cache's back would be
                # invisible to the very transfer that needs it.
                session.navigate(self._wpd_storage_id(session, storage), path, create_missing=True)
                log.info("ensure_directory storage=%s path=%r -> confirmed (wpd)", storage, path)
                return
            except StorageNotFoundError:
                raise
            except Exception as exc:  # noqa: BLE001 -- same fallback rule as send_file
                self._close_wpd()
                if attempt == 1:
                    # Same reasoning as send_file: one failed call is not yet
                    # a transport that does not work.
                    log.warning("WPD ensure_directory failed (%s: %s) -- reopening the WPD session once",
                                type(exc).__name__, exc)
                    continue
                log.warning("WPD ensure_directory failed again (%s: %s) -- falling back to the Shell",
                            type(exc).__name__, exc)
                self._wpd_unavailable = True
        storage_item = self._get_storage_item(storage)
        self._navigate(storage_item, path, create_missing=True)
        log.info("ensure_directory storage=%s path=%r -> confirmed (shell)", storage, path)

    # -- MtpBackend: transfer -------------------------------------------------

    def send_file(
        self, storage: str, dest_path: str, source_path: Path, *, overwrite: bool = False,
        progress: Optional[Callable[[int, int], None]] = None, expected_sha256: Optional[str] = None,
    ) -> TransferResult:
        self._require_connected()  # re-verifies THIS device specifically, right before touching anything

        if not source_path.is_file():
            raise InvalidOperationError(f"source file does not exist: {source_path}")

        parent_path, filename = split_dest_path(dest_path)
        expected_size = source_path.stat().st_size
        operation_id = uuid.uuid4().hex[:12]
        t0 = time.monotonic()
        session = self._wpd_session()
        log.info(
            "send_file start op=%s storage=%s dest=%r source=%r size=%d transport=%s",
            operation_id, storage, dest_path, source_path.name, expected_size,
            # An overwrite is the one case a WPD session still hands back to
            # the Shell (see _send_via_wpd), so say so up front rather than
            # logging an intent the next line contradicts.
            "wpd" if session is not None and not overwrite else "shell",
        )

        outcome = None
        for attempt in (1, 2):
            if session is None:
                break
            try:
                outcome = self._send_via_wpd(
                    session, storage, parent_path, filename, dest_path, source_path,
                    overwrite=overwrite, expected_size=expected_size, progress=progress,
                    expected_sha256=expected_sha256,
                )
                break
            except (FileAlreadyExistsError, DestinationNotFoundError):
                # Real, meaningful answers about the destination -- identical
                # on either transport (verified on real hardware: the Shell
                # and WPD enumerate DBI's install node identically, phantom
                # post-install placeholder included). Retrying such a job on
                # the other transport would only produce the same refusal a
                # second time, so these propagate unchanged.
                raise
            except Exception as exc:  # noqa: BLE001 -- see _wpd_session(): any WPD fault falls back, never fails the job
                self._close_wpd()
                if isinstance(exc, _WpdWriteFailed) and storage in SIZE_VERIFIABLE_STORAGES:
                    # What may be left at the destination now is our own
                    # half-written object; whichever transport tries next
                    # may replace it -- and only it.
                    overwrite = True
                if attempt == 1 and storage in SIZE_VERIFIABLE_STORAGES:
                    # A console that stops answering for a while is not a
                    # transport that does not work. Real case (2026-09-23):
                    # after 137 files at 0.14s each, one 5 KB write to DBI
                    # timed out (IStream::Write, 0x80070079) and three
                    # seconds later the console was answering again -- but
                    # the whole rest of that 4,625-file job had already been
                    # switched to the Shell, 25 times slower, which then gave
                    # up at the next stall. So: a fresh session, the same
                    # file, once. Install-like storages keep the old rule --
                    # there a second attempt means sending a whole game again.
                    log.warning(
                        "op=%s WPD transport failed (%s: %s) -- reopening the WPD session and trying "
                        "this file once more", operation_id, type(exc).__name__, exc,
                    )
                    session = self._wpd_session()
                    continue
                log.warning(
                    "op=%s WPD transport failed (%s: %s) -- falling back to the Shell copy engine "
                    "for the rest of this connection", operation_id, type(exc).__name__, exc,
                )
                self._wpd_unavailable = True
                outcome = None
                break

        already_present = outcome is _ALREADY_THERE
        if already_present:
            outcome = (TransferStatus.COMPLETED, 0, None)
        if outcome is None:
            outcome = self._send_via_shell(
                storage, parent_path, filename, dest_path, source_path,
                overwrite=overwrite, expected_size=expected_size,
            )
        status, bytes_sent, error = outcome

        elapsed = time.monotonic() - t0
        log.info("send_file end op=%s status=%s elapsed=%.2fs error=%s", operation_id, status.value, elapsed, error)

        transfer_result = TransferResult(
            operation_id=operation_id, status=status, storage=storage, dest_path=dest_path,
            bytes_sent=bytes_sent, bytes_total=expected_size, error=error, already_present=already_present,
        )
        self._transfers[operation_id] = transfer_result
        return transfer_result

    # -- transport A: WPD (default) ------------------------------------------

    def _wpd_session(self):
        """The open WpdSession for this device, or None to use the Shell.

        None is returned -- never an exception -- when WPD is switched off,
        when this device_id isn't a convertible Shell WPD path, or when a
        previous WPD call on this connection failed. Choosing a transport
        must never be able to fail a transfer: the Shell path that shipped
        before this existed stays available underneath at all times.
        """
        if self._wpd_unavailable or not self._connected:
            return None
        if self._wpd is not None:
            return self._wpd
        pnp_id = wpd.pnp_id_from_device_id(self._device_id)
        if pnp_id is None:
            self._wpd_unavailable = True
            return None
        try:
            self._wpd = wpd.WpdSession(pnp_id).open()
        except Exception as exc:  # noqa: BLE001 -- see docstring
            log.warning("could not open a WPD session for device=%s (%s: %s) -- using the Shell transport",
                        device_fingerprint(self._device_id), type(exc).__name__, exc)
            self._wpd = None
            self._wpd_unavailable = True
        return self._wpd

    def _close_wpd(self) -> None:
        if self._wpd is not None:
            try:
                self._wpd.close()
            except Exception:  # noqa: BLE001 -- teardown must not raise over a device that already went away
                log.debug("ignoring error while closing the WPD session", exc_info=True)
        self._wpd = None
        self._wpd_storage_ids = {}

    def _wpd_storage_id(self, session, storage: str) -> str:
        cached = self._wpd_storage_ids.get(storage)
        if cached is not None:
            return cached
        for object_id, raw_name in session.storages():
            if resolve_storage_name(raw_name, self._storage_overrides) == storage:
                self._wpd_storage_ids[storage] = object_id
                return object_id
        raise StorageNotFoundError(f"storage {storage!r} not found on device {mask_device_id(self._device_id)}")

    def _send_via_wpd(
        self, session, storage: str, parent_path: str, filename: str, dest_path: str,
        source_path: Path, *, overwrite: bool, expected_size: int, progress,
        expected_sha256: Optional[str] = None,
    ):
        """Streams the file ourselves through IPortableDevice, which -- unlike
        IFileOperation -- reports progress, separates "bytes moving" from
        "device finalising", and on real hardware moved a 123 MiB .nsp in
        5.97s where the Shell took 68.5s for the identical file on the
        identical console (docs/PERF-MTP.md)."""
        storage_id = self._wpd_storage_id(session, storage)
        parent_id = session.navigate(storage_id, parent_path, create_missing=False)
        if parent_id is None:
            raise DestinationNotFoundError(
                f"'{parent_path}' does not exist under {storage!r} -- call ensure_directory() first"
            )
        existing = session.child_id(parent_id, filename)
        if existing is not None:
            if (expected_sha256 and storage in SIZE_VERIFIABLE_STORAGES
                    and self._holds_exactly(session, existing, expected_size, expected_sha256)):
                return _ALREADY_THERE
            if overwrite:
                # Replacing an existing object means deleting it first; MTP
                # would otherwise happily hold two objects with the same
                # name. The Shell's copy engine already does that replacement
                # correctly, so replacing keeps using it rather than growing a
                # second, less-tested delete implementation here -- but only
                # for a file that is actually there. Handing every file of an
                # Override job to the Shell made a 4,625-file folder a
                # four-hour copy.
                return None
            raise FileAlreadyExistsError(f"'{dest_path}' already exists on '{storage}'")

        try:
            timing = session.send_file(
                parent_id, filename, source_path, progress=progress,
                remember=storage in SIZE_VERIFIABLE_STORAGES,
            )
        except Exception as exc:  # noqa: BLE001 -- re-raised, marked as possibly half-written
            raise _WpdWriteFailed(exc) from exc
        log.info("wpd transfer dest=%r %s", dest_path, timing)

        if timing.bytes_written != expected_size:
            return (
                TransferStatus.FAILED, timing.bytes_written,
                f"only {timing.bytes_written} of {expected_size} bytes were accepted by the device",
            )
        if timing.finalise_timed_out:
            # Every byte was accepted; the device just never answered the
            # commit inside the WPD stack's own timeout. Real-hardware case
            # (2026-09-19): a 388 MiB .nsz whose console screen reported the
            # install finished in 35s while the commit sat for 82s and then
            # returned ERROR_SEM_TIMEOUT -- DBI deletes its virtual install
            # object on completion instead of answering.
            #
            # This must NEVER fall through to the Shell fallback. Doing so
            # re-sent the entire file, so the console installed the same game
            # twice and one job took 192s instead of ~15s. There is nothing
            # left to retry: the bytes are already there.
            return (
                TransferStatus.UNVERIFIED, timing.bytes_written,
                f"all {expected_size} bytes were accepted ({timing.stream_mb_per_second:.1f} MB/s), but the "
                f"device did not answer the end of the transfer within {timing.commit_seconds:.0f}s -- "
                "normal for a large .nsz, which DBI keeps decompressing after the last byte; "
                "check the console screen for the install result",
            )
        if storage in SIZE_VERIFIABLE_STORAGES:
            # A real filesystem-like storage: read the size straight back off
            # the object we just created. No polling and no shell-cache lag to
            # wait out -- that ~2.5s visibility delay was a property of the
            # Shell namespace, not of the device (see this module's docstring).
            try:
                object_id = session.child_id(parent_id, filename)
                observed = session.object_size(object_id) if object_id is not None else None
            except Exception as exc:  # noqa: BLE001 -- see below: this must not reach the Shell fallback
                # The bytes are already committed. Falling back now would
                # re-send a file that is physically there, which for a real
                # filesystem storage means walking straight into this
                # backend's own refuse-to-overwrite check and reporting a
                # destination conflict for a transfer that actually worked.
                log.warning("could not read back the written file's size (%s: %s)", type(exc).__name__, exc)
                return (
                    TransferStatus.UNVERIFIED, timing.bytes_written,
                    f"all {expected_size} bytes were committed, but the written file's size could not be "
                    f"read back to confirm it ({exc})",
                )
            if observed == expected_size:
                return TransferStatus.COMPLETED, expected_size, None
            return (
                TransferStatus.FAILED, timing.bytes_written,
                f"device reports size {observed!r} for the written file, expected {expected_size} "
                "-- not reporting success",
            )
        # Install-like virtual node (SD_INSTALL, NAND_INSTALL). Commit()
        # returning success is the device's own acknowledgement that it
        # accepted and finalised the object -- strictly more than the Shell
        # path could ever establish, and it is still not proof that DBI
        # installed anything, so this stays UNVERIFIED exactly as before.
        return (
            TransferStatus.UNVERIFIED, timing.bytes_written,
            f"device accepted and finalised all {expected_size} bytes "
            f"({timing.stream_mb_per_second:.1f} MB/s, {timing.commit_seconds:.1f}s to finalise); "
            "DBI-side installation result still cannot be verified via MTP -- check the console screen",
        )

    @staticmethod
    def _holds_exactly(session, object_id, expected_size: int, expected_sha256: str) -> bool:
        """Does this existing object already hold exactly the bytes about to
        be sent? Proven only by reading it back and hashing it -- never by
        name or size alone -- and only for files small enough to be worth
        reading. Any doubt (a read error, too big) is "no": the caller then
        treats the file as the conflict it always was."""
        if expected_size > READ_BACK_MAX_BYTES:
            return False
        try:
            if session.object_size(object_id) != expected_size:
                return False
            data = session.read_file(object_id, expected_size)
        except Exception as exc:  # noqa: BLE001 -- a failed comparison is just "not proven identical"
            log.info("could not read back an existing file to compare it (%s: %s)", type(exc).__name__, exc)
            return False
        return data is not None and hashlib.sha256(data).hexdigest() == expected_sha256

    # -- transport B: the Shell's copy engine (fallback) ---------------------

    def _send_via_shell(
        self, storage: str, parent_path: str, filename: str, dest_path: str, source_path: Path,
        *, overwrite: bool, expected_size: int,
    ):
        storage_item = self._get_storage_item(storage)
        parent_item = self._navigate(storage_item, parent_path, create_missing=False)

        if any(i.Name == filename for i in parent_item.GetFolder.Items()) and not overwrite:
            raise FileAlreadyExistsError(f"'{dest_path}' already exists on '{storage}'")

        try:
            result = self._copy_via_ifileoperation(source_path, parent_item, filename)
        except _ComError as exc:
            raise TransferFailedError(f"IFileOperation failed for '{dest_path}': {exc}") from exc

        if result is not None:  # aborted before/without a normal PerformOperations() completion
            return result
        if storage in SIZE_VERIFIABLE_STORAGES:
            return self._verify_after_copy(parent_item, filename, expected_size=expected_size)
        # Install-like virtual node (SD_INSTALL, NAND_INSTALL) -- size is not
        # a meaningful signal here (see module-level comment on
        # SIZE_VERIFIABLE_STORAGES). Never reports COMPLETED; at best
        # UNVERIFIED (transport accepted, DBI-side result unprovable).
        return self._verify_after_install_copy(parent_item, filename)

    def _copy_via_ifileoperation(self, source_path: Path, dest_folder_item, filename: str):
        """Returns None on a normal (exception-free) PerformOperations()
        call -- completion is NEVER inferred from that alone (see
        verify_transfer_completion's docstring); the caller always polls
        afterwards. Returns (status, bytes, error) directly only for the
        case IFileOperation itself reports the operation was aborted.

        Raises _ComError (wrapping the real pywintypes.com_error) for any
        COM-level failure -- wrapped here, at the one place pywintypes is
        actually needed, so the rest of this module doesn't have to import
        it just to catch failures."""
        import pythoncom
        import pywintypes
        import win32com.shell.shell as shell_api

        try:
            source_item = shell_api.SHCreateItemFromParsingName(str(source_path), None, shell_api.IID_IShellItem)
            dest_item = self._to_ishell_item(dest_folder_item)

            op = pythoncom.CoCreateInstance(
                shell_api.CLSID_FileOperation, None, pythoncom.CLSCTX_ALL, shell_api.IID_IFileOperation,
            )
            op.SetOperationFlags(COPY_OPERATION_FLAGS)
            op.CopyItem(source_item, dest_item, filename, None)
            op.PerformOperations()
            aborted = op.GetAnyOperationsAborted()
        except pywintypes.com_error as exc:
            raise _ComError(str(exc)) from exc

        if aborted:
            return TransferStatus.FAILED, 0, "IFileOperation reported the operation was aborted"
        return None

    def _verify_after_copy(self, parent_item, filename: str, *, expected_size: int):
        def poll_fn() -> PollReading:
            if not self.is_connected:
                return PollReading(device_present=False, file_exists=False, size=None)
            match = next((i for i in parent_item.GetFolder.Items() if i.Name == filename), None)
            if match is None:
                return PollReading(device_present=True, file_exists=False, size=None)
            return PollReading(device_present=True, file_exists=True, size=_extended_property_int(match, "System.Size"))

        return verify_transfer_completion(poll_fn, expected_size=expected_size)

    def _verify_after_install_copy(self, parent_item, filename: str):
        """SD_INSTALL/NAND_INSTALL counterpart to _verify_after_copy() --
        presence-only, never size-based (see verify_install_transport())."""
        def poll_fn() -> PollReading:
            if not self.is_connected:
                return PollReading(device_present=False, file_exists=False, size=None)
            match = next((i for i in parent_item.GetFolder.Items() if i.Name == filename), None)
            if match is None:
                return PollReading(device_present=True, file_exists=False, size=None)
            return PollReading(device_present=True, file_exists=True, size=_extended_property_int(match, "System.Size"))

        return verify_install_transport(poll_fn)

    def get_transfer_status(self, operation_id: str) -> TransferResult:
        result = self._transfers.get(operation_id)
        if result is None:
            raise InvalidOperationError(f"unknown operation_id: {operation_id!r}")
        return result

    # -- installed games (see MtpBackend.list_installed_title_ids()'s own
    # docstring for the contract) --------------------------------------------

    def list_installed_title_ids(self) -> Optional[set[str]]:
        """Real-hardware finding (2026-09-15, DBI's "Installed Games"
        setting enabled on-console): the INSTALLED_GAMES storage node's
        root also contains a file, "InstalledApplications.csv", that DBI
        itself writes -- one row per installed content unit (base game or
        one specific DLC; an installed UPDATE just bumps its base row's
        own version column, never a separate row) as
        "0x<TITLE_ID>,<version>,\"<display name>\"". This is exact TITLE_ID
        data straight from the console, not a display-name guess -- far
        more precise than matching by name, so parse_installed_
        applications_csv() is what actually does the matching here."""
        try:
            item = self._get_storage_item("INSTALLED_GAMES")
        except StorageNotFoundError:
            return None
        csv_item = next(
            (c for c in item.GetFolder.Items()
             if not c.IsFolder and c.Name.lower().startswith("installedapplications")),
            None,
        )
        if csv_item is None:
            return None
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = Path(tmp_dir) / csv_item.Name
            self._download_file(csv_item, Path(tmp_dir))
            text = local_path.read_text(encoding="utf-8-sig", errors="replace")
        return parse_installed_applications_csv(text)

    def _download_file(self, source_item, dest_dir: Path) -> None:
        """Copies a real MTP file down to a local directory -- the
        download-direction mirror of _copy_via_ifileoperation()'s upload:
        same IFileOperation + PIDL bridge (_to_ishell_item) for the device
        side; the local side is the DESTINATION here, a real filesystem
        path SHCreateItemFromParsingName already handles directly (that
        call only fails for a WPD/device path, see _to_ishell_item's own
        docstring)."""
        import pythoncom
        import win32com.shell.shell as shell_api

        dest_item = shell_api.SHCreateItemFromParsingName(str(dest_dir), None, shell_api.IID_IShellItem)
        op = pythoncom.CoCreateInstance(
            shell_api.CLSID_FileOperation, None, pythoncom.CLSCTX_ALL, shell_api.IID_IFileOperation,
        )
        op.SetOperationFlags(COPY_OPERATION_FLAGS)
        op.CopyItem(self._to_ishell_item(source_item), dest_item, source_item.Name, None)
        op.PerformOperations()


# ---------------------------------------------------------------------------
# small internal helpers
# ---------------------------------------------------------------------------

class _ComError(Exception):
    """Wraps pywintypes.com_error so the rest of this module (and its
    tests) doesn't need pywin32 imported just to catch COM failures."""


def enumerate_devices() -> list[DeviceInfo]:
    """Live, connection-free enumeration of every MTP device currently
    visible under This PC -- the same Shell.Application walk
    RealMtpBackend._find_device_item() does internally, exposed as a
    standalone helper for callers that just need to LIST what's there
    (CLI's mtp-test device picker, the Web UI's device list -- see
    switchagent/web/) without constructing a backend instance per device.
    Never caches and never touches switchagent/db.py's `devices` table --
    callers wanting "was this seen before" history do that themselves
    (see db.upsert_device_seen)."""
    import win32com.client
    shell = win32com.client.Dispatch("Shell.Application")
    this_pc = shell.NameSpace(THIS_PC_NAMESPACE)
    items = [i for i in this_pc.Items() if i.IsFolder and not i.IsFileSystem]
    return _devices_from_shell_items(items)


def _devices_from_shell_items(items) -> list[DeviceInfo]:
    """Pure mapping/filtering over duck-typed Shell items -- separated from
    the live COM walk above so it is unit-testable with plain fake objects,
    the same split _match_device_by_id() below already uses.

    A console mid-enumeration reports a placeholder serial that EVERY
    console shares (see has_placeholder_serial). Dropping it here, at the
    one point every caller enumerates through, is what keeps it out of the
    registry, out of the devices table and out of the device pickers."""
    devices = []
    for item in items:
        if has_placeholder_serial(item.Path):
            log.info("ignoring a device still enumerating without a real serial: name=%r", item.Name)
            continue
        devices.append(DeviceInfo(device_id=item.Path, name=item.Name, connected=True))
    return devices


def _match_device_by_id(candidates, device_id: str):
    """Pure matching logic over a list of duck-typed candidates (anything
    with .Path) -- separated from live enumeration so it's unit-testable
    with plain fake objects, no COM required."""
    for item in candidates:
        if item.Path == device_id:
            return item
    return None


def _extended_property_int(item, key: str) -> Optional[int]:
    try:
        val = item.ExtendedProperty(key)
    except Exception:  # noqa: BLE001 -- property access failing just means "unknown", not fatal
        return None
    if isinstance(val, int):
        return val
    return None
