"""In-memory mock MTP backend.

Lets the whole pipeline -- and its tests -- run against something that
behaves like a Switch running DBI's MTP Responder, without any physical
device: storages, directories, files, existence checks, "already exists"
conflicts, transfer failures, corruption, and mid-transfer disconnects all
behave the way a real backend's callers would see them. See base.py's
module docstring for why the interface looks the way it does.

Nothing here is required by MtpBackend -- add_storage/arm_failure/
simulate_disconnect/operation_log are mock-only test setup and inspection
API, not part of the abstract interface a real backend would also expose.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .base import DeviceInfo, MtpBackend, MtpEntry, StorageInfo, TransferResult, TransferStatus, read_path, save_write_path
from .errors import (
    DestinationNotFoundError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    FileAlreadyExistsError,
    InvalidOperationError,
    OperationCancelledError,
    StorageNotFoundError,
    TransferFailedError,
)

_FAULT_MODES = ("error", "disconnect", "corrupt", "partial", "unverified")


@dataclass
class LogEntry:
    seq: int
    operation: str
    details: dict


class _MockNode:
    __slots__ = ("is_dir", "data")

    def __init__(self, is_dir: bool, data: Optional[bytes] = None):
        self.is_dir = is_dir
        self.data = data


class MockStorage:
    """One storage's in-memory filesystem: a flat dict of normalized path ->
    node, plus an optional synthetic free_bytes counter."""

    def __init__(
        self, name: str, *, writable: bool = True, free_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None, raw_name: Optional[str] = None,
    ):
        self.name = name
        self.writable = writable
        self.free_bytes = free_bytes
        self.total_bytes = total_bytes
        self.nodes: dict[str, _MockNode] = {}
        # UI-007: what a real DBI would have reported this storage as,
        # purely for list_storages()' StorageInfo.raw_name -- defaults to
        # `name` itself (mock storages are always already declared
        # unambiguous by whoever calls add_storage(), unlike real
        # hardware's occasional unrecognized raw names). Never affects
        # this mock's own storage lookup/transfer routing, which stays
        # keyed by `name` exactly as before -- see MockMtpBackend.
        # set_storage_overrides()'s own docstring for why.
        self.raw_name = raw_name if raw_name is not None else name

    @staticmethod
    def normalize(path: str) -> str:
        normalized = path.replace("\\", "/")
        parts = [seg for seg in normalized.split("/") if seg not in ("", ".")]
        if any(seg == ".." for seg in parts):
            raise InvalidOperationError(f"path escapes storage root: {path!r}")
        return "/".join(parts)

    def exists(self, path: str) -> bool:
        return self.normalize(path) in self.nodes

    def is_dir(self, path: str) -> bool:
        norm = self.normalize(path)
        if norm == "":
            return True  # storage root always counts as a directory
        node = self.nodes.get(norm)
        return node is not None and node.is_dir

    def ensure_directory(self, path: str) -> None:
        norm = self.normalize(path)
        if norm == "":
            return
        current = ""
        for part in norm.split("/"):
            current = f"{current}/{part}" if current else part
            existing = self.nodes.get(current)
            if existing is None:
                self.nodes[current] = _MockNode(is_dir=True)
            elif not existing.is_dir:
                raise DestinationNotFoundError(
                    f"'{current}' already exists as a file, cannot use it as a directory"
                )

    def parent_dir_exists(self, path: str) -> bool:
        norm = self.normalize(path)
        parent = "/".join(norm.split("/")[:-1])
        return self.is_dir(parent)

    def write_file(self, path: str, data: bytes) -> None:
        self.nodes[self.normalize(path)] = _MockNode(is_dir=False, data=data)

    def read_file(self, path: str) -> bytes:
        node = self.nodes.get(self.normalize(path))
        if node is None or node.is_dir:
            raise KeyError(path)
        return node.data

    def delete(self, path: str) -> None:
        self.nodes.pop(self.normalize(path), None)

    def list_files(self) -> list[str]:
        return sorted(p for p, n in self.nodes.items() if not n.is_dir)


@dataclass
class _ArmedFault:
    mode: str
    match_storage: Optional[str]
    match_path: Optional[str]


class MockMtpBackend(MtpBackend):
    """One instance = one mock device (see mtp/base.py's module docstring on
    device identity). Multiple simultaneously "connected" Switches are
    modeled as multiple separate MockMtpBackend instances, each with its own
    device_id, its own storages, and its own operation log -- never as one
    instance juggling several devices. device_id defaults to a fixed value
    for backward compatibility with single-device stage-3 code/tests that
    never needed to care about it; multi-device callers must pass distinct
    ids explicitly (e.g. "mock-switch-parent" / "mock-switch-child")."""

    def __init__(
        self, *, device_id: str = "mock-switch", device_name: str = "Switch (mock)", device_present: bool = True,
    ):
        self._device_id = device_id
        self._device_name = device_name
        self._device_present = device_present
        self._connected = False
        self._storages: dict[str, MockStorage] = {}
        self._log: list[LogEntry] = []
        self._log_seq = 0
        self._transfers: dict[str, TransferResult] = {}
        self._armed_faults: list[_ArmedFault] = []
        self._save_faults: list[tuple[str, str, Optional[str]]] = []
        self._storage_overrides: dict[str, str] = {}

    # -- test setup / inspection (mock-only, not part of MtpBackend) --------

    @property
    def device_id(self) -> str:
        """Known without connecting -- a DeviceRegistry keys backends by
        this, same as it would for a real backend's configured target id."""
        return self._device_id

    def add_storage(
        self, name: str, *, writable: bool = True, free_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None, raw_name: Optional[str] = None,
    ) -> MockStorage:
        storage = MockStorage(
            name, writable=writable, free_bytes=free_bytes, total_bytes=total_bytes, raw_name=raw_name,
        )
        self._storages[name] = storage
        return storage

    def set_device_present(self, present: bool) -> None:
        self._device_present = present
        if not present:
            self._connected = False

    def simulate_disconnect(self) -> None:
        """The Switch goes away right now, independent of any in-flight
        call -- as opposed to arm_failure("disconnect", ...), which only
        fires the NEXT time a matching send_file() is attempted."""
        self._connected = False
        self._log_op("DEVICE_DISCONNECTED_EXTERNALLY", {})

    def arm_failure(self, mode: str, *, storage: Optional[str] = None, dest_path: Optional[str] = None) -> None:
        """Queues a one-shot fault: the next send_file() matching `storage`
        and/or `dest_path` (None = match anything) fails in the given way
        instead of succeeding. Consumed on first match."""
        if mode not in _FAULT_MODES:
            raise ValueError(f"unknown fault mode: {mode!r}, expected one of {_FAULT_MODES}")
        self._armed_faults.append(_ArmedFault(mode, storage, dest_path))

    def arm_save_failure(self, action: str, *, mode: str = 'error', path: Optional[str] = None) -> None:
        """One-shot fault for delete/create/write; write also supports partial."""
        if action not in ('delete', 'create', 'write') or mode not in ('error', 'disconnect', 'partial'):
            raise ValueError('unknown save fault')
        self._save_faults.append((action, mode, path))

    def _save_fault(self, action: str, path: str) -> Optional[str]:
        for index, (wanted, mode, wanted_path) in enumerate(self._save_faults):
            if wanted == action and wanted_path in (None, path):
                self._save_faults.pop(index)
                if mode == 'disconnect':
                    self.simulate_disconnect()
                    raise DeviceDisconnectedError('mock disconnected during save write; target state unknown')
                return mode
        return None

    @property
    def operation_log(self) -> list[LogEntry]:
        return list(self._log)

    def storage_tree(self, storage: str) -> MockStorage:
        """Direct access to a storage's in-memory contents, for tests that
        want to assert on exactly what ended up where."""
        return self._get_storage_obj(storage)

    # -- internal -------------------------------------------------------

    def _log_op(self, operation: str, details: dict) -> None:
        self._log_seq += 1
        self._log.append(LogEntry(self._log_seq, operation, dict(details)))

    def _require_connected(self) -> None:
        if not self._connected:
            raise DeviceDisconnectedError("not connected -- call connect() first")

    def _get_storage_obj(self, storage: str) -> MockStorage:
        s = self._storages.get(storage)
        if s is None:
            raise StorageNotFoundError(f"storage not found: {storage!r}")
        return s

    def _pop_matching_fault(self, storage: str, dest_path: str) -> Optional[_ArmedFault]:
        for i, fault in enumerate(self._armed_faults):
            if fault.match_storage not in (None, storage):
                continue
            if fault.match_path not in (None, dest_path):
                continue
            return self._armed_faults.pop(i)
        return None

    # -- MtpBackend -------------------------------------------------------

    def connect(self) -> DeviceInfo:
        if not self._device_present:
            self._log_op("CONNECT_FAILED", {"reason": "device not found", "device_id": self._device_id})
            raise DeviceNotFoundError(
                f"no Switch/DBI MTP Responder found for device_id={self._device_id!r} "
                "(mock: device_present=False)"
            )
        if not self._connected:
            self._read_session_token = uuid.uuid4().hex
        self._connected = True
        self._log_op("CONNECT", {"device_id": self._device_id, "device": self._device_name})
        return DeviceInfo(device_id=self._device_id, name=self._device_name, connected=True)

    def disconnect(self) -> None:
        self._connected = False
        self._log_op("DISCONNECT", {})

    @property
    def is_connected(self) -> bool:
        return self._connected

    def capabilities(self):
        return {'read_files': True, 'exact_restore': True,
                'verified_save_identity': True, 'save_write': True}

    def _save_write_tree(self, storage, save_root, path, expected_session):
        if not expected_session:
            raise InvalidOperationError('save write requires a pinned session')
        self._check_read_session(expected_session)
        root, target = save_write_path(storage, save_root, path)
        tree = self._get_storage_obj(storage)
        if not tree.writable or not tree.is_dir(root):
            raise InvalidOperationError('save root is missing or not writable')
        return tree, target

    def delete_save_object(self, storage, save_root, path, *, expected_session,
                           recursive=False) -> None:
        if not isinstance(recursive, bool):
            raise InvalidOperationError('recursive must be explicit boolean')
        tree, target = self._save_write_tree(storage, save_root, path, expected_session)
        node = tree.nodes.get(target)
        if node is None:
            raise DestinationNotFoundError(target)
        children = [key for key in tree.nodes if key.startswith(target + '/')]
        if children and not recursive:
            raise InvalidOperationError('directory is not empty')
        self._log_op('DELETE_SAVE_OBJECT', {'storage': storage, 'path': target,
                                            'recursive': recursive})
        if self._save_fault('delete', target):
            raise TransferFailedError('mock save delete failed; target state unknown')
        for key in (target, *children):
            tree.delete(key)

    def create_save_directory(self, storage, save_root, path, *, expected_session) -> None:
        tree, target = self._save_write_tree(storage, save_root, path, expected_session)
        parent = target.rsplit('/', 1)[0]
        if not tree.is_dir(parent):
            raise DestinationNotFoundError(parent)
        if tree.exists(target):
            raise FileAlreadyExistsError(target)
        self._log_op('CREATE_SAVE_DIRECTORY', {'storage': storage, 'path': target})
        if self._save_fault('create', target):
            raise TransferFailedError('mock save directory creation failed; target state unknown')
        tree.ensure_directory(target)

    def write_save_file(self, storage, save_root, path, source_path, *, replace,
                        expected_session, cancel=None, progress=None) -> None:
        from .reading import is_link_or_reparse

        if not isinstance(replace, bool):
            raise InvalidOperationError('replace must be explicit boolean')
        tree, target = self._save_write_tree(storage, save_root, path, expected_session)
        parent = target.rsplit('/', 1)[0]
        if not tree.is_dir(parent):
            raise DestinationNotFoundError(parent)
        existing = tree.nodes.get(target)
        if existing is not None and (existing.is_dir or not replace):
            raise FileAlreadyExistsError(target)
        source = Path(source_path)
        if not source.is_file() or any(is_link_or_reparse(item) for item in (source, *source.parents)):
            raise InvalidOperationError('restore source is not a regular unlinked file')
        if cancel and cancel():
            raise OperationCancelledError('save write cancelled before transfer')
        data = source.read_bytes()
        self._check_read_session(expected_session)
        self._log_op('WRITE_SAVE_FILE', {'storage': storage, 'path': target,
                                         'replace': replace, 'bytes': len(data)})
        fault = self._save_fault('write', target)
        if fault == 'partial':
            tree.write_file(target, data[:len(data) // 2])
            raise TransferFailedError('mock partial save write; target state unknown')
        if fault:
            raise TransferFailedError('mock save write failed; target state unknown')
        tree.write_file(target, data)
        if progress:
            progress(len(data), len(data))

    def set_save_identity(self, storage, path, *, title_id, user_id, environment_id,
                          save_type='Account', verified=True):
        """Explicit test-fixture identity; never inferred from display names."""
        if not hasattr(self, '_save_identities'):
            self._save_identities = {}
        self._save_identities[(storage, read_path(path))] = {
            'title_id': title_id, 'user_id': user_id, 'environment_id': environment_id,
            'save_type': save_type, 'verified': verified, 'source': 'mock-fixture'}

    def save_identity(self, storage, path, *, expected_session=None):
        self.stat(storage, path, expected_session=expected_session)
        identity = getattr(self, '_save_identities', {}).get((storage, path))
        return dict(identity) if identity else super().save_identity(storage, path, expected_session=expected_session)

    def replace_save_tree(self, storage, path, source_directory, *, expected_identity,
                          expected_session, cancel=None, progress=None):
        from .errors import OperationCancelledError
        from .reading import is_link_or_reparse
        token = self._check_read_session(expected_session)
        path = read_path(path)
        identity = self.save_identity(storage, path, expected_session=token)
        if (storage != 'SAVES' or not path or not identity.get('verified')
                or identity.get('save_type') != 'Account'
                or any(not identity.get(k) or identity.get(k) != expected_identity.get(k)
                       for k in ('title_id', 'user_id', 'environment_id', 'save_type'))
                or not expected_identity.get('verified')):
            raise InvalidOperationError('save identity is unknown or differs from the original target')
        tree = self._get_storage_obj(storage)
        if not tree.writable or not tree.is_dir(path):
            raise InvalidOperationError('save target is not writable')
        source = Path(source_directory)
        if not source.is_dir():
            raise InvalidOperationError('restore source is not a directory')
        for parent in (source, *source.parents):
            if is_link_or_reparse(parent):
                raise InvalidOperationError('restore source traverses a link')
        candidates = [source]
        replacement = {}
        total = 0
        while candidates:
            local = candidates.pop()
            if is_link_or_reparse(local):
                raise InvalidOperationError('restore source contains a link')
            if cancel and cancel():
                raise OperationCancelledError('restore cancelled before writing')
            relative = local.relative_to(source).as_posix()
            key = path if local == source else path + '/' + read_path(relative)
            if local.is_dir():
                replacement[key] = _MockNode(True)
                candidates.extend(local.iterdir())
            elif local.is_file():
                data = local.read_bytes()  # mock's in-memory storage; real API takes paths
                replacement[key] = _MockNode(False, data)
                total += len(data)
            else:
                raise InvalidOperationError('restore source is not a regular file')
        if tree.free_bytes is not None and total > tree.free_bytes:
            raise TransferFailedError('insufficient space for restore')
        self._check_read_session(token)
        if self.save_identity(storage, path) != identity:
            raise InvalidOperationError('save identity changed')
        fault = self._pop_matching_fault(storage, path)
        self._log_op('REPLACE_SAVE_TREE', {'storage': storage, 'path': path})
        if fault:
            if fault.mode == 'disconnect':
                self.simulate_disconnect()
                raise DeviceDisconnectedError('restore disconnected; target state unknown')
            raise TransferFailedError('mock restore failed; target state unknown')
        for old in list(tree.nodes):
            if old == path or old.startswith(path + '/'):
                del tree.nodes[old]
        tree.nodes.update(replacement)
        if progress:
            progress(total, total)

    def stat(self, storage, path, *, expected_session=None):
        from .errors import SourceNotFoundError
        token = self._check_read_session(expected_session)
        path = read_path(path)
        tree = self._get_storage_obj(storage)
        if not path:
            return MtpEntry('', '', True, session_token=token)
        node = tree.nodes.get(path)
        if node is None:
            raise SourceNotFoundError(path)
        return MtpEntry(path, path.rsplit('/', 1)[-1], node.is_dir,
                        None if node.is_dir else len(node.data), path, {}, token)

    def list_directory(self, storage, path='', *, expected_session=None):
        entry = self.stat(storage, path, expected_session=expected_session)
        if not entry.is_dir:
            raise InvalidOperationError('source is not a directory')
        prefix = path + '/' if path else ''
        return [self.stat(storage, p, expected_session=entry.session_token)
                for p in sorted(self._get_storage_obj(storage).nodes)
                if p.startswith(prefix) and '/' not in p[len(prefix):]]

    def receive_file(self, storage, source_path, destination_path, *, progress=None,
                     cancel=None, expected_session=None):
        import io
        from .reading import receive_stream
        entry = self.stat(storage, source_path, expected_session=expected_session)
        if entry.is_dir:
            raise InvalidOperationError('source is a directory')
        data = self._get_storage_obj(storage).read_file(source_path)
        fault = getattr(self, '_read_fault', None)
        if fault and fault[1] in (None, storage) and fault[2] in (None, source_path):
            self._read_fault = None
        else:
            fault = None
        source = io.BytesIO(data[:len(data) // 2] if fault and fault[0] == 'short' else data)
        def read(count):
            if fault and fault[0] == 'disconnect':
                self.simulate_disconnect()
                raise DeviceDisconnectedError('mock disconnected while reading')
            if fault and fault[0] == 'error':
                raise TransferFailedError('mock read failed')
            return source.read(count)
        return receive_stream(read, destination_path, size=entry.size,
                              progress=progress, cancel=cancel,
                              check_session=lambda: self._check_read_session(entry.session_token))

    def arm_read_failure(self, mode, *, storage=None, source_path=None):
        if mode not in ('short', 'disconnect', 'error'):
            raise ValueError('unknown read failure')
        self._read_fault = (mode, storage, source_path)

    def set_storage_overrides(self, overrides: dict[str, str]) -> None:
        """Stored and reflected in operation_log for test introspection
        (proving the orchestration layer -- WebContext.refresh_devices --
        actually calls this with the right dict), but deliberately does
        NOT affect this mock's own storage lookup/transfer routing (see
        MockStorage.raw_name's own docstring on why: mock storages are
        always already declared unambiguous, unlike the rare real-hardware
        case this feature exists for). RealMtpBackend is where override
        RESOLUTION is actually exercised -- see mtp/windows.py's
        resolve_storage_name(), covered by its own dedicated pure-logic
        tests."""
        self._storage_overrides = dict(overrides)
        self._log_op("SET_STORAGE_OVERRIDES", {"overrides": dict(overrides)})

    def list_storages(self) -> list[StorageInfo]:
        self._require_connected()
        self._log_op("LIST_STORAGE", {"count": len(self._storages)})
        return [
            StorageInfo(
                name=s.name, writable=s.writable, free_bytes=s.free_bytes,
                total_bytes=s.total_bytes, raw_name=s.raw_name,
            )
            for s in self._storages.values()
        ]

    def get_storage(self, storage: str) -> StorageInfo:
        self._require_connected()
        s = self._storages.get(storage)
        self._log_op("GET_STORAGE", {"storage": storage, "found": s is not None})
        if s is None:
            raise StorageNotFoundError(f"storage not found: {storage!r}")
        return StorageInfo(
            name=s.name, writable=s.writable, free_bytes=s.free_bytes,
            total_bytes=s.total_bytes, raw_name=s.raw_name,
        )

    def exists(self, storage: str, path: str) -> bool:
        self._require_connected()
        s = self._get_storage_obj(storage)
        result = s.exists(path)
        self._log_op("EXISTS", {"storage": storage, "path": path, "result": result})
        return result

    def ensure_directory(self, storage: str, path: str) -> None:
        self._require_connected()
        s = self._get_storage_obj(storage)
        s.ensure_directory(path)
        self._log_op("CREATE_DIRECTORY", {"storage": storage, "path": path})

    def send_file(
        self, storage: str, dest_path: str, source_path: Path, *, overwrite: bool = False,
        progress: Optional[Callable[[int, int], None]] = None, expected_sha256: Optional[str] = None,
    ) -> TransferResult:
        self._require_connected()
        s = self._get_storage_obj(storage)
        operation_id = uuid.uuid4().hex[:12]

        if not source_path.is_file():
            raise InvalidOperationError(f"source file does not exist: {source_path}")

        if not s.parent_dir_exists(dest_path):
            raise DestinationNotFoundError(
                f"destination directory for '{dest_path}' does not exist on '{storage}' "
                f"-- call ensure_directory() first"
            )

        if s.exists(dest_path) and expected_sha256 is not None:
            if hashlib.sha256(s.read_file(dest_path)).hexdigest() == expected_sha256:
                self._log_op("SEND_FILE_ALREADY_PRESENT", {"storage": storage, "path": dest_path})
                return TransferResult(
                    operation_id=operation_id, status=TransferStatus.COMPLETED, storage=storage,
                    dest_path=dest_path, bytes_sent=0, bytes_total=source_path.stat().st_size,
                    already_present=True,
                )

        if s.exists(dest_path) and not overwrite:
            self._log_op("SEND_FILE_REJECTED", {"storage": storage, "path": dest_path, "reason": "already exists"})
            raise FileAlreadyExistsError(f"'{dest_path}' already exists on '{storage}'")

        try:
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            raise TransferFailedError(f"could not read source file {source_path}: {exc}") from exc
        total = len(source_bytes)

        fault = self._pop_matching_fault(storage, dest_path)

        if fault is not None and fault.mode == "disconnect":
            self._connected = False
            self._log_op("SEND_FILE_DISCONNECTED", {"storage": storage, "path": dest_path})
            result = TransferResult(
                operation_id, TransferStatus.DEVICE_DISCONNECTED, storage, dest_path,
                bytes_sent=0, bytes_total=total, error="device disconnected mid-transfer",
            )
            self._transfers[operation_id] = result
            return result

        if fault is not None and fault.mode == "error":
            self._log_op("SEND_FILE_FAILED", {"storage": storage, "path": dest_path, "reason": "simulated error"})
            result = TransferResult(
                operation_id, TransferStatus.FAILED, storage, dest_path,
                bytes_sent=0, bytes_total=total, error="simulated transfer error",
            )
            self._transfers[operation_id] = result
            return result

        if fault is not None and fault.mode == "unverified":
            # Simulates an install-like virtual node (see
            # switchagent/mtp/windows.py's SD_INSTALL handling on real
            # hardware): the device DID accept the bytes -- written here,
            # same as a real success -- but this mock reports the same
            # honest "cannot prove it either way" outcome a real backend
            # would for that kind of destination. bytes_sent=0 deliberately
            # mirrors the real, physically-confirmed-successful case where
            # observed size read 0 the whole time (docs/STAGE5B-REAL-MTP.md).
            s.write_file(dest_path, source_bytes)
            self._log_op("SEND_FILE_UNVERIFIED", {"storage": storage, "path": dest_path})
            result = TransferResult(
                operation_id, TransferStatus.UNVERIFIED, storage, dest_path,
                bytes_sent=0, bytes_total=total,
                error="simulated: transport accepted, installation result not verifiable",
            )
            self._transfers[operation_id] = result
            return result

        if fault is not None and fault.mode == "partial":
            partial_len = total // 2
            # A failed/partial transfer must never leave something at
            # dest_path that looks like a complete file -- nothing is
            # written to the visible storage tree.
            self._log_op(
                "SEND_FILE_PARTIAL",
                {"storage": storage, "path": dest_path, "bytes_sent": partial_len, "bytes_total": total},
            )
            result = TransferResult(
                operation_id, TransferStatus.PARTIAL, storage, dest_path,
                bytes_sent=partial_len, bytes_total=total, error="transfer stopped partway",
            )
            self._transfers[operation_id] = result
            return result

        write_bytes = source_bytes
        if fault is not None and fault.mode == "corrupt":
            write_bytes = _corrupt(source_bytes)

        s.write_file(dest_path, write_bytes)

        # One honest progress report for a transfer that is instantaneous
        # here: the callback contract (base.py) only promises calls happen
        # during send_file, not how many. Enough for a caller's own progress
        # bookkeeping to be exercised by tests against this backend.
        if progress is not None:
            progress(total, total)

        # Verify what actually landed before ever reporting success -- a
        # backend must never claim COMPLETED for a transfer that wasn't
        # byte-identical to the source (see base.py's send_file docstring).
        written = s.read_file(dest_path)
        if hashlib.sha256(written).digest() != hashlib.sha256(source_bytes).digest():
            s.delete(dest_path)
            self._log_op("SEND_FILE_CORRUPT", {"storage": storage, "path": dest_path})
            result = TransferResult(
                operation_id, TransferStatus.FAILED, storage, dest_path,
                bytes_sent=len(written), bytes_total=total,
                error="verification failed: written content does not match source",
            )
            self._transfers[operation_id] = result
            return result

        self._log_op("SEND_FILE", {"storage": storage, "path": dest_path, "bytes": total})
        result = TransferResult(
            operation_id, TransferStatus.COMPLETED, storage, dest_path,
            bytes_sent=total, bytes_total=total,
        )
        self._transfers[operation_id] = result
        return result

    def get_transfer_status(self, operation_id: str) -> TransferResult:
        result = self._transfers.get(operation_id)
        if result is None:
            raise InvalidOperationError(f"unknown operation_id: {operation_id!r}")
        return result


def _corrupt(data: bytes) -> bytes:
    if not data:
        return b"\x00"
    mutated = bytearray(data)
    mutated[0] ^= 0xFF
    return bytes(mutated)
