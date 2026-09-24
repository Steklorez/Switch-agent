"""Abstract MTP backend.

Business logic (transfer.py, and eventually a real queue worker) talks only
to this interface -- never to MockMtpBackend or a future RealMtpBackend
directly. That's the whole point of this module: swapping the mock for a
real Windows MTP driver later should mean writing one new class that
implements MtpBackend, and touching nothing in scanner.py, extractor.py,
db.py, preview.py, or transfer.py.

Storage names are logical, not display strings: "SD_CARD" / "SD_INSTALL",
matching the `suggested_target` values already used throughout the codebase
since stage 2 (scanner.py, model.py). A real backend maps these to whatever
DBI happens to display them as on a given firmware version (e.g. "1: SD
Card", "5: SD install" -- see docs/RESEARCH-STAGE1.md) -- that mapping is
the real backend's own problem, not something callers or this interface
need to know about.

Design choice worth calling out: there is deliberately no
"send whole directory" operation here. docs/RESEARCH-STAGE1.md §2 found
(from the existing, battle-tested sync-to-switch.ps1 script) that real MTP
to a Switch only tolerates one transfer at a time, and that copying a whole
folder over an existing one triggers an unsuppressable Windows "merge?"
dialog. Modeling multi-file transfers (atmosphere mods) as repeated
single-file send_file() calls, orchestrated one layer up in transfer.py,
avoids baking an operation into this interface that a real backend
literally could not implement safely.

Device identity (stage 4): one MtpBackend INSTANCE always represents
exactly one physical device, identified by DeviceInfo.device_id. Multiple
simultaneously connected Switches (e.g. a parent's and a child's) are
modeled as multiple separate backend instances, one per device_id -- never
as one backend juggling several devices internally, and never as a
"device_id" parameter threaded through every method. This mirrors how
Windows WPD itself works (IPortableDeviceManager enumerates stable device
IDs; IPortableDevice is instantiated per specific ID), so a future
RealMtpBackend needs no different shape here. device_id MUST stay stable
across reconnects of the SAME physical device -- never derived from USB
enumeration order, list position, or a display name, all of which can
change on replug. Storage identity (StorageInfo.name, e.g. "SD_CARD") is
implicitly scoped to whichever device's backend instance you called it on
-- two devices can both report a storage named "SD_CARD" without collision,
because they are two different MtpBackend instances with two different
device_id's, not two entries under one shared name. Job identity
(switchagent/db.py's `jobs.id`) is a third, unrelated concept -- a job
always records which device_id it targets (`jobs.target_device_id`) and
that target is fixed at job-creation time; nothing in this interface or in
switchagent/queue_worker.py ever substitutes a different device for a job's
already-assigned target.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


class TransferStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"                    # some bytes sent, transfer did not finish
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    # The MTP transport layer accepted the transfer (no COM error, the
    # destination object appeared under the target storage/path), but this
    # backend has no reliable way to confirm the RECEIVING side's own
    # completion -- e.g. DBI's "SD Card install" virtual node, where
    # System.Size was observed at 0 for the entire duration of a transfer
    # that a physical, on-console check later confirmed DID install
    # correctly (see docs/STAGE5B-REAL-MTP.md, "Install smoke-test"). Never
    # returned for a plain filesystem-style storage where size verification
    # is meaningful (e.g. SD_CARD) -- there, the existing COMPLETED/FAILED
    # split via size-stabilization remains the correct, proven check.
    # Callers must not treat UNVERIFIED as a confirmed success, but must
    # also not treat it as a confirmed failure -- it is its own, honestly
    # distinct outcome, never silently folded into either.
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str    # stable across reconnects -- see MtpBackend.connect()
    name: str         # human-readable label only, NOT an identity (can repeat, can change)
    connected: bool


@dataclass(frozen=True)
class StorageInfo:
    name: str                # EFFECTIVE logical name, e.g. "SD_CARD" / "SD_INSTALL"
                              # (post manual-override if one applies -- see UI-007's
                              # set_storage_overrides() below; this is still the exact
                              # same string every existing caller already uses for
                              # transfer routing, unchanged meaning)
    writable: bool
    free_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    # UI-007: the raw, backend-reported display name this `name` was
    # resolved from (e.g. "5: SD Card install" on real hardware) -- None
    # if a given backend has no such concept to report. Display/mapping-UI
    # only; never used for transfer routing (that's always `name`).
    raw_name: Optional[str] = None


@dataclass(frozen=True)
class TransferResult:
    operation_id: str
    status: TransferStatus
    storage: str
    dest_path: str
    bytes_sent: int
    bytes_total: int
    error: Optional[str] = None
    # COMPLETED without writing: the destination already held a file with
    # exactly the expected content, read back and hashed (send_file's
    # expected_sha256). Nothing was sent, and nothing had to be.
    already_present: bool = False


class MtpBackend(ABC):
    """Everything the rest of SwitchAgent is allowed to know about talking
    to a Switch. No method here does anything Windows-specific or
    USB-specific -- that's entirely up to the concrete implementation."""

    # -- connection lifecycle ------------------------------------------------

    @abstractmethod
    def connect(self) -> DeviceInfo:
        """Finds and connects to THE device this backend instance was
        constructed for (see the module docstring on device identity -- one
        instance is always exactly one device). Raises DeviceNotFoundError
        if that specific device isn't currently reachable -- never connects
        to some other, unrelated device instead. Safe to call again while
        already connected (must be idempotent, not raise).

        DeviceInfo.device_id returned here must be identical across
        reconnects of the same physical device -- callers (queue_worker.py)
        rely on it never changing to keep a job pinned to its target."""

    @abstractmethod
    def disconnect(self) -> None:
        """Releases the connection. Safe to call even if not connected."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    # -- storage ---------------------------------------------------------

    @abstractmethod
    def list_storages(self) -> list[StorageInfo]:
        """Raises DeviceDisconnectedError if not connected."""

    @abstractmethod
    def get_storage(self, storage: str) -> StorageInfo:
        """Raises StorageNotFoundError if `storage` doesn't exist on this
        device, DeviceDisconnectedError if not connected."""

    def set_storage_overrides(self, overrides: dict[str, str]) -> None:
        """UI-007: raw_storage_name -> logical_name manual overrides (see
        device_storage_mappings in db.py), loaded and kept current by the
        orchestration layer (WebContext.refresh_devices, worker-thread-only
        -- see that module's own COM-safety notes on why) -- a backend must
        NEVER query SQLite itself for this. Concrete default no-op (not
        abstract): a backend with no raw-name ambiguity to resolve (e.g.
        MockMtpBackend's storages are always already unambiguous by
        construction) can simply ignore this without needing to implement
        anything. RealMtpBackend is the implementation that actually needs
        it, for the rare DBI-reported raw name this project's automatic
        patterns (mtp/windows.py's logical_storage_name()) don't
        recognize."""
        return

    # -- paths -------------------------------------------------------------

    @abstractmethod
    def exists(self, storage: str, path: str) -> bool:
        """True if `path` (file or directory) exists on `storage`. Raises
        StorageNotFoundError / DeviceDisconnectedError as appropriate --
        this is a query, not a soft check, so a bad storage name is still an
        error, not a silent False."""

    @abstractmethod
    def ensure_directory(self, storage: str, path: str) -> None:
        """Get-or-create: makes `path` exist as a directory on `storage`,
        creating intermediate directories as needed. No-op if it already
        exists as a directory. Raises DestinationNotFoundError if `path`
        exists but is a file, not a directory."""

    # -- transfer ------------------------------------------------------------

    @abstractmethod
    def send_file(
        self, storage: str, dest_path: str, source_path: Path, *, overwrite: bool = False,
        progress: Optional[Callable[[int, int], None]] = None, expected_sha256: Optional[str] = None,
    ) -> TransferResult:
        """Sends exactly one local file to storage:dest_path.

        `expected_sha256`: when dest_path already exists, a backend that can
        read it back may compare its content to this hash and, if they are
        the same, report COMPLETED with already_present=True instead of
        raising FileAlreadyExistsError or overwriting anything. Only ever
        proven by reading the bytes -- a matching name or size is not proof.

        `progress(bytes_sent, bytes_total)` is optional in both directions: a
        caller need not pass one, and a backend that cannot observe its own
        transfer mid-flight (the Shell copy engine cannot) simply never calls
        it. A backend that does call it must do so from the calling thread,
        while send_file is still running, and must not call it so often that
        the callback's own cost matters -- roughly once a second. The
        callback may raise errors.TransferAborted to stop the transfer (the
        user pressed Abort); a backend must let that propagate unchanged --
        never retry the file, never fall back to another transport. That is
        also why a backend that cannot report progress cannot be stopped
        mid-file: the callback is the only way in. The parent
        directory of dest_path must already exist (call ensure_directory
        first) -- raises DestinationNotFoundError otherwise, it does not
        create it implicitly.

        Raises FileAlreadyExistsError if dest_path already exists and
        overwrite is False. Never partially overwrites an existing file:
        either the whole transfer lands, or the destination is left exactly
        as it was before the call.

        A backend MUST verify what it wrote before reporting COMPLETED --
        never report success for a transfer that didn't actually complete
        intact. If verification fails, or the device disconnects mid-
        transfer, this returns a TransferResult with a non-COMPLETED status
        (it does not need to raise for that case -- TransferFailedError is
        for operational failures the caller couldn't have predicted from the
        result alone, e.g. an I/O error opening source_path).

        Some destinations (e.g. a virtual "install" node on a real device)
        give no reliable way to verify completion at all -- for those, a
        backend may return TransferStatus.UNVERIFIED instead of guessing
        COMPLETED or FAILED. This still must not be returned for a
        destination where verification IS possible; it exists specifically
        for the case where the backend can prove transport was accepted but
        cannot prove (or disprove) the receiving side's own completion."""

    # -- installed games (DBI's "Installed games" MTP node, real hardware
    # only -- confirmed present and browsable 2026-09-15) -------------------

    def list_installed_title_ids(self) -> Optional[set[str]]:
        """BASE title ids (title_id.classify_title_variant's recovered
        base_title_id) DBI currently reports as installed on this device --
        parsed from "InstalledApplications.csv" inside its own "Installed
        games" MTP storage node (real-hardware-confirmed 2026-09-15,
        gated behind a DBI-side setting, not present on every console/DBI
        session -- see mtp/windows.py's implementation for the exact
        real-hardware finding and CSV shape). Exact TITLE_ID data straight
        from the console, never a display-name guess.

        Returns None (distinct from an empty set, which would mean "the
        node/file exists but nothing is installed") if this backend/
        device doesn't currently expose the node or file at all --
        callers must treat that as "no information", never as "nothing is
        installed".

        Concrete default no-op returning None here: a backend with no such
        concept (MockMtpBackend) doesn't need to implement this at all,
        same pattern as set_storage_overrides() above."""
        return None

    @abstractmethod
    def get_transfer_status(self, operation_id: str) -> TransferResult:
        """Looks up the result of a previous send_file() call by its
        operation_id. Raises InvalidOperationError if operation_id is
        unknown. Exists mainly so a future real backend can expose
        poll-based progress/completion (see docs/RESEARCH-STAGE1.md §2 on
        why Windows MTP has no completion event) with the same interface
        the mock already satisfies synchronously."""
