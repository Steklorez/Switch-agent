"""Local, synchronous backup service. Call from the existing device worker only.

The store and archive formats are deliberately independent of HTTP and COM.
Only a backend's explicit identity/capability can authorize a restore.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .mtp.base import MtpBackend, MtpEntry, read_path
from .mtp.errors import (AmbiguousPathError, InvalidOperationError,
                         OperationCancelledError, ReadAccessDeniedError,
                         SourceNotFoundError, UnsupportedOperationError)
from .mtp.reading import is_link_or_reparse


class BackupError(Exception):
    """Invalid backup, uncertain identity, insufficient resources, or stale plan."""


@dataclass(frozen=True)
class BackupLimits:
    max_files: int = 500_000
    max_total_bytes: int = 8 * 1024**4
    max_file_bytes: int = 2 * 1024**4
    max_manifest_bytes: int = 8 * 1024**2
    max_ratio: int = 10_000


FORMAT_VERSION = 1
_SAVE_GROUPS = frozenset({'Installed games', 'Uninstalled games'})
_NON_PROFILE_SAVE_TYPES = frozenset({'system', 'device', 'bcat', 'cache',
                                     'temporary', 'systembcat'})
_CHUNK = 1024 * 1024
_RESERVED = re.compile(
    r'^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|'
    r'COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\..*)?$',
    re.I,
)


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel and cancel():
        raise OperationCancelledError('backup operation cancelled')


def _valid_part(part: str) -> bool:
    return (bool(part) and part not in ('.', '..') and part[-1] not in (' ', '.')
            and not _RESERVED.match(part) and not any(ord(ch) < 32 for ch in part)
            and not any(ch in '<>:"\\|?*' for ch in part))


def _device_part(part: str) -> bool:
    """An MTP directory name need not be a valid Windows filename."""
    return (isinstance(part, str) and bool(part) and part not in ('.', '..')
            and not any(ch in '/\\\x00' for ch in part))


def _safe_rel(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith('/'):
        raise BackupError('unsafe archive path')
    parts = value.split('/')
    if not all(_valid_part(p) for p in parts):
        raise BackupError('unsafe archive path')
    return '/'.join(parts)


def _ensure_plain_path(path: Path) -> None:
    for parent in (path, *path.parents):
        if os.path.lexists(parent) and is_link_or_reparse(parent):
            raise BackupError('local path traverses a link or reparse point')


def _hash_file(path: Path, cancel=None) -> tuple[str, int]:
    _ensure_plain_path(path)
    if not path.is_file():
        raise BackupError('snapshot file is missing')
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while chunk := source.read(_CHUNK):
            _check_cancel(cancel)
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class BackupManager:
    """Owns a per-mode local store; no background thread or device discovery."""

    def __init__(self, root: Path | str, *, limits: BackupLimits | None = None):
        self.root = Path(root).absolute()
        _ensure_plain_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.limits = limits or BackupLimits()
        self._plans: dict[str, dict] = {}
        self.journal_warnings: list[str] = []

    def _record(self, kind: str, state: str, **details) -> None:
        event = {'id': uuid.uuid4().hex, 'at': time.time(),
                 'kind': kind, 'state': state, **details}
        path = self.root / 'events.jsonl'
        _ensure_plain_path(path)
        with path.open('a', encoding='utf-8') as journal:
            journal.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n')
            journal.flush()
            os.fsync(journal.fileno())

    def _record_ready(self, kind: str, **details) -> bool:
        """A committed result stays ready if its auxiliary journal is unavailable."""
        try:
            self._record(kind, 'ready', **details)
        except (OSError, BackupError) as exc:
            self.journal_warnings.append(f'{kind}: {exc}')
            return False
        return True

    def list_events(self, *, limit: int = 100) -> list[dict]:
        if not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise BackupError('invalid journal limit')
        path = self.root / 'events.jsonl'
        _ensure_plain_path(path)
        if not path.exists():
            return []
        with path.open('r', encoding='utf-8') as journal:
            lines = deque(journal, maxlen=limit)
        return [json.loads(line) for line in lines]

    def _walk(self, backend: MtpBackend, storage: str, root: str = '', *,
              session: str, cancel=None, max_depth: int = 16):
        """Yield directory entries with full paths; never collapse duplicate names."""
        stack = [(root, 0)]
        seen = 0
        while stack:
            _check_cancel(cancel)
            path, depth = stack.pop()
            if depth > max_depth:
                raise BackupError('device directory depth limit exceeded')
            children = backend.list_directory(storage, path, expected_session=session)
            names = set()
            for child in children:
                if not _valid_part(child.name) or child.name.casefold() in names:
                    raise BackupError('ambiguous or unsafe device name')
                names.add(child.name.casefold())
                if child.metadata.get('type_known') is False:
                    raise BackupError('device object type is unknown')
                if child.path != (f'{path}/{child.name}' if path else child.name):
                    raise BackupError('device returned inconsistent path')
            seen += len(children)
            if seen > self.limits.max_files:
                raise BackupError('device object limit exceeded')
            yield path, children
            stack.extend((c.path, depth + 1) for c in reversed(children) if c.is_dir)

    def inventory_saves(self, backend: MtpBackend, *, storage: str = 'SAVES',
                        cancel=None) -> list[dict]:
        """List DBI group/game/profile roots, isolating unsafe save subtrees."""
        session = backend.session_token
        rows = []
        for group in backend.list_directory(storage, '', expected_session=session):
            _check_cancel(cancel)
            if not group.is_dir or group.name not in _SAVE_GROUPS:
                continue
            try:
                games = backend.list_directory(storage, group.path, expected_session=session)
            except (AmbiguousPathError, InvalidOperationError,
                    ReadAccessDeniedError, SourceNotFoundError):
                backend._check_read_session(session)
                rows.append({'path': group.path, 'name': group.name,
                             'size': None, 'file_count': None, 'identity': None,
                             'selectable': False, 'session_token': session,
                             'reason': 'Cannot safely enumerate this save group'})
                continue
            for game in games:
                _check_cancel(cancel)
                if not game.is_dir or not _device_part(game.name):
                    continue
                try:
                    saves = backend.list_directory(storage, game.path, expected_session=session)
                except (AmbiguousPathError, InvalidOperationError,
                        ReadAccessDeniedError, SourceNotFoundError):
                    backend._check_read_session(session)
                    rows.append({'path': game.path, 'name': game.name,
                                 'size': None, 'file_count': None, 'identity': None,
                                 'selectable': False, 'session_token': session,
                                 'reason': 'Cannot safely enumerate this game folder'})
                    continue
                for save in saves:
                    _check_cancel(cancel)
                    if not save.is_dir:
                        continue
                    row = {'path': save.path, 'name': save.name, 'size': None,
                           'file_count': None, 'identity': None, 'selectable': False,
                           'session_token': session}
                    if not _device_part(save.name) or save.path != f'{game.path}/{save.name}':
                        row['reason'] = 'Invalid MTP save folder name'
                    else:
                        try:
                            # Inventory must not recursively scan large DBI save trees.
                            # Snapshot creation performs the complete validation later.
                            children = backend.list_directory(
                                storage, save.path, expected_session=session)
                            if len(children) > self.limits.max_files:
                                raise BackupError('device object limit exceeded')
                            names = set()
                            has_directories = False
                            known_size = 0
                            all_sizes_known = True
                            for child in children:
                                _check_cancel(cancel)
                                if not isinstance(child.name, str):
                                    raise BackupError('ambiguous or unsafe device object')
                                name = child.name.casefold()
                                if (not _valid_part(child.name) or name in names
                                        or child.path != f'{save.path}/{child.name}'
                                        or child.metadata.get('type_known') is False):
                                    raise BackupError('ambiguous or unsafe device object')
                                names.add(name)
                                if child.is_dir:
                                    has_directories = True
                                elif child.size is None:
                                    all_sizes_known = False
                                else:
                                    if child.size > self.limits.max_file_bytes:
                                        raise BackupError('file size limit exceeded')
                                    known_size += child.size
                                    if known_size > self.limits.max_total_bytes:
                                        raise BackupError('total size limit exceeded')
                            row['identity'] = backend.save_identity(
                                storage, save.path, expected_session=session)
                            if not has_directories:
                                row['file_count'] = len(children)
                                if all_sizes_known:
                                    row['size'] = known_size
                            row['selectable'] = True
                        except (BackupError, AmbiguousPathError, InvalidOperationError,
                                ReadAccessDeniedError, SourceNotFoundError):
                            # A transport fault can invalidate the whole session;
                            # never return a partial inventory in that case.
                            backend._check_read_session(session)
                            row['reason'] = 'Cannot safely enumerate this save'
                    rows.append(row)
        return rows

    def inventory_games(self, backend: MtpBackend, *, storage: str = 'INSTALLED_GAMES',
                        cancel=None) -> list[dict]:
        session = backend.session_token
        rows = []
        for path, children in self._walk(backend, storage, session=session, cancel=cancel):
            for child in children:
                if not child.is_dir and child.name.casefold().endswith(('.nsp', '.nsz', '.xci', '.xcz')):
                    rows.append({'path': child.path, 'name': child.name, 'size': child.size,
                                 'kind': 'package', 'session_token': session})
        return rows

    def _snapshot_dir(self, snapshot_id: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', snapshot_id):
            raise BackupError('invalid snapshot id')
        return self.root / 'snapshots' / snapshot_id

    def _manifest_for(self, snapshot_id: str) -> dict:
        folder = self._snapshot_dir(snapshot_id)
        _ensure_plain_path(folder)
        path = folder / 'manifest.json'
        if not path.is_file() or path.stat().st_size > self.limits.max_manifest_bytes:
            raise BackupError('snapshot is incomplete or missing')
        with path.open('r', encoding='utf-8') as source:
            manifest = json.load(source)
        if manifest.get('state') != 'ready' or manifest.get('id') != snapshot_id:
            raise BackupError('snapshot is not ready')
        return manifest

    def list_snapshots(self, *, kind: str | None = None) -> list[dict]:
        folder = self.root / 'snapshots'
        if not folder.exists():
            return []
        rows = []
        for entry in folder.iterdir():
            if not entry.is_dir():
                continue
            try:
                manifest = self._manifest_for(entry.name)
            except (BackupError, ValueError, json.JSONDecodeError, OSError):
                continue
            if kind is None or manifest['kind'] == kind:
                rows.append(manifest)
        return sorted(rows, key=lambda item: item['created_at'], reverse=True)

    def list_incomplete(self) -> list[dict]:
        folder = self.root / '.partial'
        if not folder.exists():
            return []
        rows = []
        for entry in folder.iterdir():
            _ensure_plain_path(entry)
            marker = entry / 'incomplete.json'
            if marker.is_file():
                try:
                    rows.append(json.loads(marker.read_text(encoding='utf-8')))
                except (ValueError, OSError):
                    continue
        return sorted(rows, key=lambda item: item['created_at'], reverse=True)

    def _enumerate_tree(self, backend: MtpBackend, storage: str, path: str,
                        session: str, cancel=None) -> tuple[list[tuple[str, MtpEntry]], list[str]]:
        root = backend.stat(storage, path, expected_session=session)
        if not root.is_dir:
            raise BackupError('snapshot source must be a directory')
        files = []
        directories = []
        total = 0
        for current, children in self._walk(backend, storage, path, session=session, cancel=cancel):
            if current != path:
                directories.append(_safe_rel(current[len(path) + 1:]))
            for child in children:
                if child.is_dir:
                    continue
                relative = _safe_rel(child.path[len(path) + 1:])
                if child.size is not None:
                    if child.size > self.limits.max_file_bytes:
                        raise BackupError('file size limit exceeded')
                    total += child.size
                    if total > self.limits.max_total_bytes:
                        raise BackupError('total size limit exceeded')
                files.append((relative, child))
                if len(files) > self.limits.max_files:
                    raise BackupError('file count limit exceeded')
        return files, directories

    def create_snapshot(self, backend: MtpBackend, path: str, *, storage: str = 'SAVES',
                        kind: str = 'save', cancel=None, progress=None) -> dict:
        """Read one complete tree into an immutable local snapshot."""
        if kind != 'save' or storage != 'SAVES':
            raise BackupError('snapshot kind/storage mismatch')
        path = read_path(path)
        parts = path.split('/')
        if len(parts) != 3 or parts[0] not in _SAVE_GROUPS:
            raise BackupError('select an individual save folder')
        session = backend.session_token
        identity = backend.save_identity(storage, path, expected_session=session)
        files, directories = self._enumerate_tree(backend, storage, path, session, cancel)
        known_size = sum(e.size or 0 for _, e in files)
        if known_size > shutil.disk_usage(self.root).free:
            raise BackupError('insufficient local disk space')
        snapshot_id = uuid.uuid4().hex
        staging = self.root / '.partial' / snapshot_id
        _ensure_plain_path(staging.parent)
        staging.mkdir(parents=True)
        content = staging / 'files'
        content.mkdir()
        from .mtp.windows import device_fingerprint
        manifest = {'format': 'switchagent-backup', 'version': FORMAT_VERSION,
                    'id': snapshot_id, 'state': 'incomplete', 'kind': kind,
                    'created_at': time.time(), 'storage': storage,
                    'source_path': path, 'identity': identity,
                    'origin': {'device_fingerprint': device_fingerprint(backend.device_id),
                               'group': parts[0], 'game': parts[1], 'profile': parts[2]},
                    'session_token': session, 'files': [], 'directories': directories}
        done = 0
        try:
            for directory in directories:
                (content / Path(*directory.split('/'))).mkdir(parents=True, exist_ok=True)
            for relative, entry in files:
                _check_cancel(cancel)
                destination = content / Path(*relative.split('/'))
                destination.parent.mkdir(parents=True, exist_ok=True)
                def check_transfer_size(received, _total):
                    if received > self.limits.max_file_bytes or done + received > self.limits.max_total_bytes:
                        raise BackupError('actual source size limit exceeded')
                backend.receive_file(storage, entry.path, destination,
                                     expected_session=session, cancel=cancel,
                                     progress=check_transfer_size)
                digest, size = _hash_file(destination, cancel)
                if entry.size is not None and entry.size != size:
                    raise BackupError('source file size changed during copy')
                if size > self.limits.max_file_bytes or done + size > self.limits.max_total_bytes:
                    raise BackupError('actual source size limit exceeded')
                manifest['files'].append({'path': relative, 'size': size, 'sha256': digest})
                done += size
                if progress:
                    progress(done, known_size if all(e.size is not None for _, e in files) else None)
            _check_cancel(cancel)
            backend._check_read_session(session)
            if backend.save_identity(storage, path, expected_session=session) != identity:
                raise BackupError('save identity changed during copy')
            manifest['state'] = 'ready'
            payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
            if len(payload) > self.limits.max_manifest_bytes:
                raise BackupError('manifest size limit exceeded')
            with (staging / 'manifest.json').open('xb') as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            target = self._snapshot_dir(snapshot_id)
            _ensure_plain_path(target)
            target.parent.mkdir(exist_ok=True)
            _ensure_plain_path(target)
            os.replace(staging, target)
        except BaseException as exc:
            # An incomplete staging tree is intentionally retained for diagnosis.
            try:
                (staging / 'incomplete.json').write_text(json.dumps({
                    'id': snapshot_id, 'state': 'incomplete', 'kind': kind,
                    'source_path': path, 'created_at': manifest['created_at'],
                }), encoding='utf-8')
            except OSError as marker_error:
                exc.add_note(f'could not write incomplete marker: {marker_error}')
            try:
                self._record('snapshot', 'incomplete', snapshot_id=snapshot_id,
                             source_path=path)
            except (OSError, BackupError) as journal_error:
                exc.add_note(f'could not write snapshot failure event: {journal_error}')
            raise
        if not self._record_ready('snapshot', snapshot_id=snapshot_id,
                                  source_path=path, kind_detail=kind):
            manifest = dict(manifest, journal_warning='event journal unavailable')
        return manifest

    def create_snapshots(self, backend: MtpBackend, paths: Iterable[str], *,
                         storage: str = 'SAVES', cancel=None, progress=None) -> list[dict]:
        selected = list(paths)
        if not selected or len(set(selected)) != len(selected):
            raise BackupError('select unique save paths')
        return [self.create_snapshot(backend, path, storage=storage,
                                     cancel=cancel, progress=progress) for path in selected]

    def _validate_file_table(self, manifest: dict) -> None:
        if not isinstance(manifest, dict) or manifest.get('format') != 'switchagent-backup' \
                or manifest.get('version') != FORMAT_VERSION or manifest.get('kind') != 'save' \
                or manifest.get('storage') != 'SAVES' or not isinstance(manifest.get('identity'), dict):
            raise BackupError('unsupported or invalid backup manifest')
        files = manifest.get('files')
        directories = manifest.get('directories')
        if not isinstance(files, list) or not isinstance(directories, list) \
                or len(files) > self.limits.max_files:
            raise BackupError('invalid backup file table')
        seen = set()
        directory_paths = set()
        file_paths = set()
        total = 0
        for directory in directories:
            relative = _safe_rel(directory)
            folded = relative.casefold()
            if folded in seen:
                raise BackupError('colliding backup paths')
            seen.add(folded)
            directory_paths.add(folded)
        for item in files:
            if not isinstance(item, dict):
                raise BackupError('invalid backup file')
            relative = _safe_rel(item.get('path'))
            folded = relative.casefold()
            if folded in seen:
                raise BackupError('colliding backup paths')
            seen.add(folded)
            file_paths.add(folded)
            size = item.get('size')
            digest = item.get('sha256')
            if not isinstance(size, int) or isinstance(size, bool) or size < 0 \
                    or size > self.limits.max_file_bytes \
                    or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise BackupError('invalid file size or hash')
            total += size
            if total > self.limits.max_total_bytes:
                raise BackupError('backup size limit exceeded')
        for path in seen:
            segments = path.split('/')
            if any('/'.join(segments[:i]) not in directory_paths
                   for i in range(1, len(segments))):
                raise BackupError('backup path has an undeclared parent directory')
            if any('/'.join(segments[:i]) in file_paths for i in range(1, len(segments))):
                raise BackupError('file is ancestor of another backup path')
        if not isinstance(manifest.get('source_path'), str) or not manifest['source_path']:
            raise BackupError('backup source path is missing')
        read_path(manifest['source_path'])

    def _verify_snapshot(self, manifest: dict, cancel=None) -> None:
        self._validate_file_table(manifest)
        base = self._snapshot_dir(manifest['id']) / 'files'
        _ensure_plain_path(base)
        if not base.is_dir():
            raise BackupError('snapshot files directory is missing')
        actual_files = set()
        actual_directories = set()
        stack = [base]
        while stack:
            current = stack.pop()
            for entry in current.iterdir():
                _check_cancel(cancel)
                _ensure_plain_path(entry)
                relative = _safe_rel(entry.relative_to(base).as_posix())
                if entry.is_dir():
                    actual_directories.add(relative)
                    stack.append(entry)
                elif entry.is_file():
                    actual_files.add(relative)
                else:
                    raise BackupError('snapshot contains an unusual object')
                if len(actual_files) + len(actual_directories) > self.limits.max_files * 2:
                    raise BackupError('snapshot object limit exceeded')
        if actual_files != {item['path'] for item in manifest['files']} \
                or actual_directories != set(manifest['directories']):
            raise BackupError('snapshot contains missing or unlisted paths')
        for item in manifest['files']:
            path = base / Path(*item['path'].split('/'))
            digest, size = _hash_file(path, cancel)
            if digest != item['sha256'] or size != item['size']:
                raise BackupError('snapshot content hash mismatch')

    def create_archive(self, snapshot_ids: Iterable[str], destination: Path | str, *,
                       cancel=None, progress=None) -> Path:
        """Write one ZIP64 with one versioned manifest and flat snapshot folders."""
        ids = list(snapshot_ids)
        if not ids or len(ids) != len(set(ids)):
            raise BackupError('select unique snapshots')
        snapshots = [self._manifest_for(i) for i in ids]
        for item in snapshots:
            self._verify_snapshot(item, cancel)
        destination = Path(destination).absolute()
        _ensure_plain_path(destination.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        bundle_manifest = {'format': 'switchagent-backup', 'version': FORMAT_VERSION,
                           'snapshots': [{k: v for k, v in item.items() if k != 'session_token'}
                                         for item in snapshots]}
        payload = json.dumps(bundle_manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
        if len(payload) > self.limits.max_manifest_bytes:
            raise BackupError('group manifest size limit exceeded')
        total = sum(f['size'] for s in snapshots for f in s['files'])
        done = 0
        created = False
        try:
            _ensure_plain_path(destination)
            with destination.open('xb') as output:
                created = True
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=1, allowZip64=True) as archive:
                    for snapshot in snapshots:
                        base = self._snapshot_dir(snapshot['id']) / 'files'
                        for item in snapshot['files']:
                            _check_cancel(cancel)
                            source = base / Path(*item['path'].split('/'))
                            name = f"snapshots/{snapshot['id']}/files/{item['path']}"
                            with source.open('rb') as reader, archive.open(name, 'w', force_zip64=True) as writer:
                                while chunk := reader.read(_CHUNK):
                                    _check_cancel(cancel)
                                    writer.write(chunk)
                                    done += len(chunk)
                                    if progress:
                                        progress(done, total)
                    archive.writestr('manifest.json', payload)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        self._record_ready('archive', snapshot_ids=ids, name=destination.name)
        return destination

    def import_archive(self, source: Path | str, *, cancel=None, progress=None) -> list[dict]:
        """Validate and stream a SwitchAgent ZIP into fresh immutable IDs."""
        source = Path(source).absolute()
        _ensure_plain_path(source)
        if not source.is_file():
            raise BackupError('archive is missing')
        stage_root = self.root / '.partial' / ('import-' + uuid.uuid4().hex)
        _ensure_plain_path(stage_root.parent)
        stage_root.mkdir(parents=True)
        imported = []
        published: list[Path] = []
        try:
            with zipfile.ZipFile(source, 'r', allowZip64=True) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > self.limits.max_files + 1:
                    raise BackupError('archive file count limit exceeded')
                names = set()
                infos_by_name = {}
                declared_total = 0
                for info in infos:
                    name = _safe_rel(info.filename)
                    folded = name.casefold()
                    if folded in names or info.is_dir():
                        raise BackupError('duplicate or directory ZIP member')
                    names.add(folded)
                    infos_by_name[name] = info
                    if (info.flag_bits & 1 or (info.create_system == 3 and
                            stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK)):
                        raise BackupError('encrypted or linked ZIP member')
                    if info.file_size < 0 or info.file_size > self.limits.max_file_bytes:
                        raise BackupError('ZIP member size limit exceeded')
                    if info.compress_size and info.file_size > max(16 * _CHUNK,
                                                                   info.compress_size * self.limits.max_ratio):
                        raise BackupError('ZIP expansion ratio exceeded')
                    declared_total += info.file_size
                    if declared_total > self.limits.max_total_bytes:
                        raise BackupError('ZIP total size limit exceeded')
                top = infos_by_name.get('manifest.json')
                if top is None or top.file_size > self.limits.max_manifest_bytes:
                    raise BackupError('SwitchAgent manifest is missing or too large')
                with archive.open(top) as reader:
                    raw = reader.read(self.limits.max_manifest_bytes + 1)
                if len(raw) > self.limits.max_manifest_bytes:
                    raise BackupError('manifest size limit exceeded')
                bundle = json.loads(raw.decode('utf-8'))
                snapshots = bundle.get('snapshots')
                if bundle.get('format') != 'switchagent-backup' or bundle.get('version') != FORMAT_VERSION \
                        or not isinstance(snapshots, list) or not snapshots:
                    raise BackupError('not a supported SwitchAgent archive')
                expected = {'manifest.json'}
                originals = set()
                for snapshot in snapshots:
                    self._validate_file_table(snapshot)
                    self._restore_origin(snapshot)
                    if snapshot.get('state') != 'ready':
                        raise BackupError('archive contains a non-ready snapshot')
                    old_id = snapshot.get('id')
                    if not isinstance(old_id, str) or not re.fullmatch(r'[0-9a-f]{32}', old_id) \
                            or old_id in originals:
                        raise BackupError('invalid or duplicate snapshot id')
                    originals.add(old_id)
                    expected.update(f"snapshots/{old_id}/files/{f['path']}" for f in snapshot['files'])
                if set(infos_by_name) != expected:
                    raise BackupError('ZIP members differ from manifest')
                if sum(f['size'] for s in snapshots for f in s['files']) > shutil.disk_usage(self.root).free:
                    raise BackupError('insufficient local disk space')
                total = sum(f['size'] for s in snapshots for f in s['files'])
                done = 0
                for snapshot in snapshots:
                    _check_cancel(cancel)
                    new_id = uuid.uuid4().hex
                    stage = stage_root / new_id
                    content = stage / 'files'
                    content.mkdir(parents=True)
                    for directory in snapshot['directories']:
                        (content / Path(*directory.split('/'))).mkdir(parents=True, exist_ok=True)
                    for item in snapshot['files']:
                        _check_cancel(cancel)
                        name = f"snapshots/{snapshot['id']}/files/{item['path']}"
                        info = infos_by_name[name]
                        if info.file_size != item['size']:
                            raise BackupError('ZIP size differs from manifest')
                        target = content / Path(*item['path'].split('/'))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        count = 0
                        with archive.open(info) as reader, target.open('xb') as writer:
                            while chunk := reader.read(_CHUNK):
                                _check_cancel(cancel)
                                count += len(chunk)
                                if count > item['size'] or count > self.limits.max_file_bytes:
                                    raise BackupError('ZIP stream exceeds declared size')
                                digest.update(chunk)
                                writer.write(chunk)
                                done += len(chunk)
                                if progress:
                                    progress(done, total)
                        if count != item['size'] or digest.hexdigest() != item['sha256']:
                            raise BackupError('ZIP content hash mismatch')
                    local = dict(snapshot, id=new_id, imported_from=snapshot['id'], state='ready')
                    local.pop('session_token', None)
                    (stage / 'manifest.json').write_text(
                        json.dumps(local, ensure_ascii=False, sort_keys=True), encoding='utf-8')
                    imported.append(local)
            destination = self.root / 'snapshots'
            _ensure_plain_path(destination)
            destination.mkdir(exist_ok=True)
            _ensure_plain_path(destination)
            for item in imported:
                target = destination / item['id']
                _ensure_plain_path(target)
                os.replace(stage_root / item['id'], target)
                published.append(target)
        except BaseException as exc:
            rollback_errors = []
            for target in published:
                try:
                    _ensure_plain_path(target)
                    shutil.rmtree(target)
                except (OSError, BackupError) as cleanup_error:
                    exc.add_note(f'could not roll back imported snapshot: {cleanup_error}')
                    rollback_errors.append(str(target))
            # This directory was generated under our private .partial root.
            if stage_root.resolve().is_relative_to((self.root / '.partial').resolve()):
                shutil.rmtree(stage_root, ignore_errors=True)
            if isinstance(exc, (zipfile.BadZipFile, EOFError, UnicodeError,
                                json.JSONDecodeError, zlib.error)):
                raise BackupError('invalid or corrupted ZIP archive') from exc
            if rollback_errors:
                raise BackupError(f'import partially published; recovery required: {rollback_errors}') from exc
            raise
        try:
            stage_root.rmdir()
        except OSError as exc:
            self.journal_warnings.append(f'import staging cleanup: {exc}')
        for item in imported:
            if not self._record_ready('import', snapshot_id=item['id'],
                                      archive_name=source.name):
                item['journal_warning'] = 'event journal unavailable'
        return imported

    @staticmethod
    def _save_class(profile: str) -> str:
        folded = profile.casefold()
        return folded if folded in _NON_PROFILE_SAVE_TYPES else 'profile'

    @staticmethod
    def _declared_type_matches(identity: dict, save_class: str) -> bool:
        declared = identity.get('save_type')
        if declared is None:
            return identity.get('verified') is not True
        if not isinstance(declared, str):
            return False
        return (declared == 'Account' if save_class == 'profile'
                else declared.casefold() == save_class)

    @staticmethod
    def _restore_origin(source: dict) -> dict:
        """Validate provenance shape; an imported manifest remains unsigned."""
        origin = source.get('origin')
        if not isinstance(origin, dict) or set(origin) != {
                'device_fingerprint', 'group', 'game', 'profile'}:
            raise BackupError('restore source origin is missing or malformed')
        if (not isinstance(origin['device_fingerprint'], str)
                or not re.fullmatch(r'[0-9a-f]{16}', origin['device_fingerprint'])
                or origin['group'] not in _SAVE_GROUPS
                or not all(_device_part(origin[key]) for key in ('game', 'profile'))
                or source.get('source_path') != '/'.join(
                    (origin['group'], origin['game'], origin['profile']))):
            raise BackupError('restore source origin is missing or malformed')
        return origin

    @staticmethod
    def _restore_identity_matches(source: dict, target: dict, *, cross_profile: bool) -> bool:
        if not isinstance(source, dict) or not isinstance(target, dict):
            return False
        source_verified = source.get('verified') is True
        target_verified = target.get('verified') is True
        if source_verified != target_verified:
            return False
        if source_verified:
            fields = ('title_id', 'environment_id', 'save_type')
            if not all(source.get(field) and source[field] == target.get(field)
                       for field in fields):
                return False
            if not source.get('user_id') or not target.get('user_id'):
                return False
            if not cross_profile and source['user_id'] != target['user_id']:
                return False
        else:
            # Values in an unverified identity are not treated as proof, but a
            # contradiction is still reason to refuse the write.
            if any((source.get(field) or target.get(field))
                   and source.get(field) != target.get(field)
                   for field in ('title_id', 'environment_id', 'save_type')):
                return False
            if (not cross_profile and (source.get('user_id') or target.get('user_id'))
                    and source.get('user_id') != target.get('user_id')):
                return False
        return True

    def _restore_target(self, backend: MtpBackend, source: dict, target_path: str,
                        session: str) -> tuple[dict, bool, dict]:
        from .mtp.windows import device_fingerprint
        origin = self._restore_origin(source)
        parts = target_path.split('/')
        if (len(parts) != 3 or parts[0] not in _SAVE_GROUPS
                or not all(_device_part(part) for part in parts)
                or parts[:2] != [origin['group'], origin['game']]):
            raise BackupError('restore target must be an existing save of the same game')
        if device_fingerprint(backend.device_id) != origin['device_fingerprint']:
            raise BackupError('restore source belongs to another console')
        source_class = self._save_class(origin['profile'])
        if source_class != self._save_class(parts[2]):
            raise BackupError('restore target save type differs from source')
        cross_profile = parts[2] != origin['profile']
        if cross_profile and source_class != 'profile':
            raise BackupError('restore target save type differs from source')
        entry = backend.stat('SAVES', target_path, expected_session=session)
        if not entry.is_dir:
            raise BackupError('restore target is not an existing save folder')
        identity = backend.save_identity('SAVES', target_path,
                                         expected_session=session)
        if (not self._declared_type_matches(source['identity'], source_class)
                or not self._declared_type_matches(identity, self._save_class(parts[2]))):
            raise BackupError('restore target save type differs from source')
        if backend.capabilities().get('verified_save_identity') and (
                source['identity'].get('verified') is not True
                or identity.get('verified') is not True):
            raise BackupError('save identity is unknown or differs from target')
        if not self._restore_identity_matches(source['identity'], identity,
                                              cross_profile=cross_profile):
            raise BackupError('save identity is unknown or differs from target')
        return identity, cross_profile, origin

    def restore_diagnostics(self, backend: MtpBackend, path: str, *,
                            storage: str = 'SAVES') -> dict:
        """Read-only target/adapter check; source and hardware remain unproven."""
        session = backend.session_token
        path = read_path(path)
        capabilities = backend.capabilities()
        missing = []
        if storage != 'SAVES':
            missing.append('SAVES storage')
        parts = path.split('/')
        if (len(parts) != 3 or parts[0] not in _SAVE_GROUPS
                or not all(_device_part(part) for part in parts)):
            missing.append('existing DBI save folder')
        entry = backend.stat(storage, path, expected_session=session)
        if not entry.is_dir:
            missing.append('existing DBI save folder')
        identity = backend.save_identity(storage, path, expected_session=session)
        if capabilities.get('verified_save_identity'):
            for field in ('title_id', 'user_id', 'environment_id', 'save_type'):
                if not identity.get(field):
                    missing.append(field)
            if identity.get('verified') is not True:
                missing.append('verified adapter identity')
        if not capabilities.get('save_write'):
            missing.append('MTP save write capability')
        return {'eligible': not missing, 'missing': missing,
                'identity': identity, 'capabilities': capabilities,
                'session_token': session,
                'eligibility_scope': 'target-and-adapter-only',
                'source_origin_and_content_pending': True,
                'hardware_qualified': False}

    @staticmethod
    def _content_digest(files: list[dict], directories: list[str]) -> str:
        payload = {'files': sorted(files, key=lambda f: f['path']),
                   'directories': sorted(directories)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()

    def _read_device_tree(self, backend: MtpBackend, path: str, session: str,
                          *, cancel=None) -> tuple[str, list[dict], list[str]]:
        files, directories = self._enumerate_tree(backend, 'SAVES', path, session, cancel)
        parent = self.root / '.partial'
        _ensure_plain_path(parent)
        parent.mkdir(exist_ok=True)
        _ensure_plain_path(parent)
        fingerprints = []
        total = 0
        with tempfile.TemporaryDirectory(prefix='verify-', dir=parent) as temporary:
            for relative, entry in files:
                _check_cancel(cancel)
                destination = Path(temporary) / uuid.uuid4().hex
                def check_transfer_size(received, _expected):
                    if received > self.limits.max_file_bytes or total + received > self.limits.max_total_bytes:
                        raise BackupError('actual target size limit exceeded')
                backend.receive_file('SAVES', entry.path, destination,
                                     expected_session=session, cancel=cancel,
                                     progress=check_transfer_size)
                digest, size = _hash_file(destination, cancel)
                if size > self.limits.max_file_bytes or total + size > self.limits.max_total_bytes:
                    raise BackupError('actual target size limit exceeded')
                total += size
                fingerprints.append({'path': relative, 'size': size, 'sha256': digest})
        return self._content_digest(fingerprints, directories), fingerprints, directories

    def _fingerprint_device_tree(self, backend: MtpBackend, path: str, session: str,
                                 *, cancel=None) -> str:
        return self._read_device_tree(backend, path, session, cancel=cancel)[0]

    def prepare_restore(self, backend: MtpBackend, snapshot_id: str, *,
                        target_path: str | None = None, cancel=None,
                        progress=None) -> dict:
        """Make a prebackup and one-use confirmation plan, before any write."""
        if not backend.capabilities().get('save_write'):
            raise UnsupportedOperationError('this adapter cannot write a save through MTP')
        source = self._manifest_for(snapshot_id)
        self._verify_snapshot(source, cancel)
        target_path = read_path(target_path or source['source_path'])
        session = backend.session_token
        target, cross_profile, origin = self._restore_target(
            backend, source, target_path, session)
        size = sum(item['size'] for item in source['files'])
        storage = backend.get_storage('SAVES')
        if not storage.writable:
            raise BackupError('save storage is read-only')
        free = storage.free_bytes
        if free is not None and free < size:
            raise BackupError('insufficient target free space')
        for existing in self._plans.values():
            if existing['device_id'] == backend.device_id and existing['expires_at'] > time.time():
                raise BackupError('another restore plan already reserves this device')
        prebackup = self.create_snapshot(backend, target_path, cancel=cancel, progress=progress)
        if prebackup['identity'] != target or self._restore_origin(prebackup) != {
                'device_fingerprint': origin['device_fingerprint'],
                'group': origin['group'], 'game': origin['game'],
                'profile': target_path.split('/')[2]}:
            raise BackupError('target identity changed while making prebackup')
        plan_id = uuid.uuid4().hex
        plan = {'id': plan_id, 'snapshot_id': snapshot_id,
                'prebackup_id': prebackup['id'], 'target_path': target_path,
                'device_id': backend.device_id, 'session_token': session,
                'identity': target, 'expires_at': time.time() + 600,
                'cross_profile': cross_profile,
                'requires_profile_confirmation': cross_profile,
                'target_profile': target_path.split('/')[2],
                'origin_assurance': 'unsigned-manifest',
                'source_digest': self._content_digest(source['files'], source['directories']),
                'target_digest': self._content_digest(prebackup['files'], prebackup['directories'])}
        self._plans[plan_id] = plan
        self._record('restore', 'prepared', plan_id=plan_id,
                     snapshot_id=snapshot_id, prebackup_id=prebackup['id'])
        return {k: v for k, v in plan.items() if k not in ('source_digest', 'target_digest')}

    def cancel_restore(self, plan_id: str) -> bool:
        existed = self._plans.pop(plan_id, None) is not None
        if existed:
            self._record('restore', 'cancelled', plan_id=plan_id)
        return existed

    def confirm_restore(self, backend: MtpBackend, plan_id: str, *,
                        confirm_profile: str | None = None, cancel=None,
                        progress=None) -> dict:
        """Consume a confirmed plan, revalidate, apply a diff, and read back."""
        plan = self._plans.get(plan_id)
        if plan is None:
            raise BackupError('restore plan is absent or already consumed')
        if plan['cross_profile'] and confirm_profile != plan['target_profile']:
            raise BackupError('enter the exact target profile name to confirm restore')
        self._plans.pop(plan_id)
        _check_cancel(cancel)
        if time.time() >= plan['expires_at'] or backend.device_id != plan['device_id'] \
                or backend.session_token != plan['session_token']:
            raise BackupError('restore plan expired or connection changed')
        if not backend.capabilities().get('save_write'):
            raise UnsupportedOperationError('restore capability is no longer available')
        source = self._manifest_for(plan['snapshot_id'])
        prebackup = self._manifest_for(plan['prebackup_id'])
        self._verify_snapshot(source, cancel)
        self._verify_snapshot(prebackup, cancel)
        if self._content_digest(source['files'], source['directories']) != plan['source_digest'] \
                or self._content_digest(prebackup['files'], prebackup['directories']) != plan['target_digest']:
            raise BackupError('restore source or prebackup changed')
        identity, cross_profile, _ = self._restore_target(
            backend, source, plan['target_path'], plan['session_token'])
        if identity != plan['identity'] or cross_profile != plan['cross_profile']:
            raise BackupError('target identity changed')
        current_digest = self._fingerprint_device_tree(backend, plan['target_path'],
                                                       plan['session_token'], cancel=cancel)
        if current_digest != plan['target_digest']:
            raise BackupError('target progress changed after prebackup')
        try:
            self._verify_snapshot(source, cancel)
        except BackupError as exc:
            raise BackupError('restore source changed before write') from exc
        identity, cross_profile, _ = self._restore_target(
            backend, source, plan['target_path'], plan['session_token'])
        if identity != plan['identity'] or cross_profile != plan['cross_profile']:
            raise BackupError('target identity changed before write')
        _check_cancel(cancel)
        size = sum(item['size'] for item in source['files'])
        storage = backend.get_storage('SAVES')
        if not storage.writable:
            raise BackupError('save storage is read-only')
        free = storage.free_bytes
        if free is not None and free < size:
            raise BackupError('insufficient target free space')
        final_target_digest, target_files, target_directories = self._read_device_tree(
            backend, plan['target_path'], plan['session_token'], cancel=cancel)
        if final_target_digest != plan['target_digest']:
            raise BackupError('target progress changed before write')
        try:
            self._verify_snapshot(source, cancel)
        except BackupError as exc:
            raise BackupError('restore source changed before write') from exc
        identity, cross_profile, _ = self._restore_target(
            backend, source, plan['target_path'], plan['session_token'])
        if identity != plan['identity'] or cross_profile != plan['cross_profile'] \
                or backend.session_token != plan['session_token']:
            raise BackupError('target identity changed before write')
        source_files = {item['path']: item for item in source['files']}
        current_files = {item['path']: item for item in target_files}
        stale_files = sorted(current_files.keys() - source_files.keys())
        stale_directories = sorted(set(target_directories) - set(source['directories']),
                                   key=lambda value: (-value.count('/'), value))
        new_directories = sorted(set(source['directories']) - set(target_directories),
                                 key=lambda value: (value.count('/'), value))
        changed_files = [item for item in source['files']
                         if current_files.get(item['path']) != item]
        save_root = plan['target_path']
        session = plan['session_token']
        # All checks above precede the first possible device write. Any failure
        # below must be reported as an uncertain target; no retry or rollback.
        try:
            for relative in stale_files:
                _check_cancel(cancel)
                backend.delete_save_object('SAVES', save_root, save_root + '/' + relative,
                                           expected_session=session, recursive=False)
            for relative in stale_directories:
                _check_cancel(cancel)
                backend.delete_save_object('SAVES', save_root, save_root + '/' + relative,
                                           expected_session=session, recursive=False)
            for relative in new_directories:
                _check_cancel(cancel)
                backend.create_save_directory('SAVES', save_root, save_root + '/' + relative,
                                              expected_session=session)
            done = 0
            total = sum(item['size'] for item in changed_files)
            for item in changed_files:
                _check_cancel(cancel)
                relative = item['path']
                backend.write_save_file(
                    'SAVES', save_root, save_root + '/' + relative,
                    self._snapshot_dir(source['id']) / 'files' / Path(*relative.split('/')),
                    replace=relative in current_files, expected_session=session,
                    cancel=cancel,
                    progress=(lambda received, _total, offset=done:
                              progress(offset + received, total)) if progress else None)
                done += item['size']
                if progress:
                    progress(done, total)
            readback = self._fingerprint_device_tree(backend, plan['target_path'],
                                                     plan['session_token'], cancel=cancel)
            if readback != plan['source_digest']:
                raise BackupError('restore readback differs from source')
            if self._restore_target(backend, source, save_root, session)[0] != identity:
                raise BackupError('target identity changed during restore')
        except BaseException as exc:
            try:
                self._record('restore', 'unknown', plan_id=plan_id,
                             prebackup_id=prebackup['id'])
            except (OSError, BackupError) as journal_error:
                self.journal_warnings.append(f'restore unknown: {journal_error}')
                exc.add_note(f'could not write restore outcome event: {journal_error}')
            raise BackupError('restore failed; target state is unknown; prebackup remains ready') from exc
        try:
            self._record('restore', 'completed', plan_id=plan_id,
                         snapshot_id=source['id'], prebackup_id=prebackup['id'])
            recorded = True
        except (OSError, BackupError) as journal_error:
            self.journal_warnings.append(f'restore completed: {journal_error}')
            recorded = False
        result = {'state': 'completed', 'verification': 'readback-hash',
                'snapshot_id': source['id'], 'prebackup_id': prebackup['id'],
                'target_path': plan['target_path']}
        if not recorded:
            result['journal_warning'] = 'event journal unavailable'
        return result

    def export_games(self, backend: MtpBackend, paths: Iterable[str], *,
                     storage: str = 'INSTALLED_GAMES', cancel=None,
                     progress=None) -> list[dict]:
        """Export only explicitly selected packages to the configured store."""
        if storage != 'INSTALLED_GAMES':
            raise BackupError('game export storage mismatch')
        selected = list(paths)
        if not selected or len(selected) != len(set(selected)):
            raise BackupError('select unique game packages')
        session = backend.session_token
        entries = []
        for path in selected:
            path = read_path(path)
            entry = backend.stat(storage, path, expected_session=session)
            if entry.is_dir or entry.metadata.get('type_known') is False:
                raise BackupError('selected game package is not a known file')
            _safe_rel(entry.name)
            if entry.size is not None and entry.size > self.limits.max_file_bytes:
                raise BackupError('game package size limit exceeded')
            entries.append(entry)
        known_size = sum(e.size or 0 for e in entries)
        if known_size > shutil.disk_usage(self.root).free:
            raise BackupError('insufficient local disk space')
        results = []
        done = 0
        total = known_size if all(e.size is not None for e in entries) else None
        for entry in entries:
            _check_cancel(cancel)
            export_id = uuid.uuid4().hex
            stage = self.root / '.partial' / ('game-' + export_id)
            _ensure_plain_path(stage)
            stage.mkdir(parents=True)
            target = stage / entry.name
            try:
                def check_transfer_size(received, _expected):
                    if received > self.limits.max_file_bytes or done + received > self.limits.max_total_bytes:
                        raise BackupError('actual game export size limit exceeded')
                backend.receive_file(storage, entry.path, target,
                                     expected_session=session, cancel=cancel,
                                     progress=check_transfer_size)
                digest, size = _hash_file(target, cancel)
                if entry.size is not None and size != entry.size:
                    raise BackupError('game package size changed')
                if size > self.limits.max_file_bytes or done + size > self.limits.max_total_bytes:
                    raise BackupError('actual game export size limit exceeded')
                done += size
                if progress:
                    progress(done, total)
                item = {'id': export_id, 'state': 'ready', 'kind': 'game-package',
                        'name': entry.name, 'source_path': entry.path,
                        'size': size, 'sha256': digest, 'created_at': time.time()}
                (stage / 'manifest.json').write_text(json.dumps(item, sort_keys=True),
                                                     encoding='utf-8')
                destination = self.root / 'game-exports'
                _ensure_plain_path(destination)
                destination.mkdir(exist_ok=True)
                final = destination / export_id
                _ensure_plain_path(final)
                os.replace(stage, final)
            except BaseException as exc:
                try:
                    (stage / 'incomplete.json').write_text(json.dumps({
                        'id': export_id, 'state': 'incomplete', 'kind': 'game-package',
                        'source_path': entry.path, 'created_at': time.time(),
                    }), encoding='utf-8')
                except OSError as marker_error:
                    exc.add_note(f'could not write incomplete marker: {marker_error}')
                try:
                    self._record('game-export', 'incomplete', export_id=export_id,
                                 source_path=entry.path)
                except (OSError, BackupError) as journal_error:
                    exc.add_note(f'could not write game export failure event: {journal_error}')
                raise
            if not self._record_ready('game-export', export_id=export_id,
                                      source_path=entry.path):
                item['journal_warning'] = 'event journal unavailable'
            results.append(item)
        return results

    def list_game_exports(self) -> list[dict]:
        folder = self.root / 'game-exports'
        if not folder.exists():
            return []
        rows = []
        for entry in folder.iterdir():
            _ensure_plain_path(entry)
            if not re.fullmatch(r'[0-9a-f]{32}', entry.name):
                continue
            marker = entry / 'manifest.json'
            if marker.is_file() and marker.stat().st_size <= self.limits.max_manifest_bytes:
                try:
                    item = json.loads(marker.read_text(encoding='utf-8'))
                    if item.get('id') == entry.name and item.get('state') == 'ready':
                        rows.append(item)
                except (ValueError, OSError):
                    continue
        return sorted(rows, key=lambda item: item['created_at'], reverse=True)

    def game_export_path(self, export_id: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', export_id):
            raise BackupError('invalid game export id')
        folder = self.root / 'game-exports' / export_id
        _ensure_plain_path(folder)
        marker = folder / 'manifest.json'
        if not marker.is_file():
            raise BackupError('game export is missing')
        item = json.loads(marker.read_text(encoding='utf-8'))
        if item.get('id') != export_id or item.get('state') != 'ready':
            raise BackupError('game export is incomplete')
        name = _safe_rel(item.get('name'))
        if '/' in name:
            raise BackupError('invalid game export name')
        path = folder / name
        digest, size = _hash_file(path)
        if digest != item['sha256'] or size != item['size']:
            raise BackupError('game export changed after copy')
        return path
