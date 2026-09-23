"""D02 save-write contract; every test uses in-memory objects, never USB."""

import pytest

from switchagent.mtp.base import save_write_path
from switchagent.mtp.errors import (
    DestinationNotFoundError, FileAlreadyExistsError, InvalidOperationError,
    StaleSessionError, TransferFailedError,
)
from switchagent.mtp.mock import MockMtpBackend


ROOT = 'Installed games/Game/Profile'


def _mock():
    backend = MockMtpBackend()
    tree = backend.add_storage('SAVES')
    tree.ensure_directory(ROOT)
    backend.connect()
    return backend, tree, backend.session_token


@pytest.mark.parametrize('storage,root,path', [
    ('SD_CARD', ROOT, ROOT + '/file'),
    ('SAVES', 'Game/Profile', 'Game/Profile/file'),
    ('SAVES', 'Other games/Game/Profile', 'Other games/Game/Profile/file'),
    ('SAVES', ROOT, ROOT),
    ('SAVES', ROOT, ROOT + '2/file'),
    ('SAVES', ROOT, ROOT + '/../Other/file'),
    ('SAVES', ROOT, ROOT + '/folder//file'),
])
def test_save_write_boundary_rejects_unsafe_paths(storage, root, path):
    with pytest.raises(InvalidOperationError):
        save_write_path(storage, root, path)


def test_mock_save_primitives_preserve_root_and_report_operations(tmp_path):
    backend, tree, token = _mock()
    tree.write_file(ROOT + '/old', b'old')
    source = tmp_path / 'new'
    source.write_bytes(b'new content')
    backend.delete_save_object('SAVES', ROOT, ROOT + '/old', expected_session=token)
    backend.create_save_directory('SAVES', ROOT, ROOT + '/nested', expected_session=token)
    backend.write_save_file('SAVES', ROOT, ROOT + '/nested/new', source,
                            replace=False, expected_session=token)
    assert tree.is_dir(ROOT)
    assert tree.read_file(ROOT + '/nested/new') == b'new content'
    assert [op.operation for op in backend.operation_log[-3:]] == [
        'DELETE_SAVE_OBJECT', 'CREATE_SAVE_DIRECTORY', 'WRITE_SAVE_FILE']


def test_mock_write_requires_existing_root_parent_session_and_explicit_replace(tmp_path):
    backend, tree, token = _mock()
    source = tmp_path / 'new'
    source.write_bytes(b'new')
    tree.write_file(ROOT + '/old', b'old')
    with pytest.raises(FileAlreadyExistsError):
        backend.write_save_file('SAVES', ROOT, ROOT + '/old', source,
                                replace=False, expected_session=token)
    with pytest.raises(StaleSessionError):
        backend.delete_save_object('SAVES', ROOT, ROOT + '/old', expected_session='stale')
    with pytest.raises(InvalidOperationError):
        backend.delete_save_object('SAVES', ROOT, ROOT + '/old',
                                   expected_session=token, recursive='false')
    with pytest.raises(InvalidOperationError):
        backend.write_save_file('SAVES', ROOT, ROOT + '/old', source,
                                replace='false', expected_session=token)
    with pytest.raises(InvalidOperationError):
        backend.write_save_file('SAVES', ROOT, ROOT + '/old', source,
                                replace=True, expected_session='')
    with pytest.raises(DestinationNotFoundError):
        backend.write_save_file('SAVES', ROOT, ROOT + '/missing/new', source,
                                replace=False, expected_session=token)
    assert tree.read_file(ROOT + '/old') == b'old'


@pytest.mark.parametrize('action', ['delete', 'create', 'write'])
def test_mock_armed_save_fault_stops_action(tmp_path, action):
    backend, tree, token = _mock()
    path = ROOT + '/target'
    tree.write_file(path, b'old')
    source = tmp_path / 'new'
    source.write_bytes(b'new')
    if action == 'create':
        tree.delete(path)
    backend.arm_save_failure(action, path=path)
    with pytest.raises(TransferFailedError):
        if action == 'delete':
            backend.delete_save_object('SAVES', ROOT, path, expected_session=token)
        elif action == 'create':
            backend.create_save_directory('SAVES', ROOT, path, expected_session=token)
        else:
            backend.write_save_file('SAVES', ROOT, path, source,
                                    replace=True, expected_session=token)
    assert tree.is_dir(ROOT)
    assert tree.exists(path) == (action != 'create')


def test_mock_partial_write_reports_unknown_state(tmp_path):
    backend, tree, token = _mock()
    source = tmp_path / 'new'
    source.write_bytes(b'123456')
    backend.arm_save_failure('write', mode='partial')
    with pytest.raises(TransferFailedError, match='unknown'):
        backend.write_save_file('SAVES', ROOT, ROOT + '/new', source,
                                replace=False, expected_session=token)
    assert tree.read_file(ROOT + '/new') == b'123'


def test_real_write_re_resolves_parent_after_replace_without_shell(tmp_path, monkeypatch):
    from switchagent.mtp import wpd
    from switchagent.mtp.windows import RealMtpBackend

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    monkeypatch.setattr(backend, '_shell', lambda: pytest.fail('Shell fallback'))
    folder = {str(wpd.WPD_OBJECT_CONTENT_TYPE): str(wpd.WPD_CONTENT_TYPE_FOLDER)}
    files = {'old': {str(wpd.WPD_OBJECT_NAME): 'file',
                     str(wpd.WPD_OBJECT_CONTENT_TYPE): str(wpd.WPD_CONTENT_TYPE_GENERIC_FILE),
                     str(wpd.WPD_OBJECT_SIZE): 3}}

    class Session:
        _properties = object()
        def storages(self):
            return [('storage', '7: Saves')]
        def resolve_read_path(self, root, path):
            assert (root, path) in (('storage', ROOT), ('root', ''))
            return 'root'
        def children(self, parent):
            assert parent == 'root'
            return list(files.items())
        def delete_save_object(self, parent, object_id, *, recursive):
            del files[object_id]
        def write_save_file(self, parent, name, source, *, check_session, cancel, progress):
            check_session()
            files['new'] = {str(wpd.WPD_OBJECT_NAME): name,
                            str(wpd.WPD_OBJECT_CONTENT_TYPE): str(wpd.WPD_CONTENT_TYPE_GENERIC_FILE),
                            str(wpd.WPD_OBJECT_SIZE): source.stat().st_size}

    session = Session()
    monkeypatch.setattr(backend, '_wpd_session', lambda: session)
    monkeypatch.setattr(wpd, 'read_props', lambda props, obj: folder)
    source = tmp_path / 'file'
    source.write_bytes(b'fresh')
    backend.write_save_file('SAVES', ROOT, ROOT + '/file', source,
                            replace=True, expected_session='live')
    assert list(files) == ['new']


def test_real_ambiguous_target_refused_before_delete(monkeypatch):
    from switchagent.mtp import wpd
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp.errors import AmbiguousPathError

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    folder = {str(wpd.WPD_OBJECT_CONTENT_TYPE): str(wpd.WPD_CONTENT_TYPE_FOLDER)}
    class Session:
        _properties = object()
        def storages(self):
            return [('storage', '7: Saves')]
        def resolve_read_path(self, root, path):
            return root
        def children(self, parent):
            return [('a', {str(wpd.WPD_OBJECT_NAME): 'file'}),
                    ('b', {str(wpd.WPD_OBJECT_NAME): 'file'})]
        def delete_save_object(self, *args, **kwargs):
            pytest.fail('ambiguous object deleted')
    session = Session()
    monkeypatch.setattr(backend, '_wpd_session', lambda: session)
    monkeypatch.setattr(wpd, 'read_props', lambda props, obj: folder)
    with pytest.raises(AmbiguousPathError):
        backend.delete_save_object('SAVES', ROOT, ROOT + '/file', expected_session='live')


def test_real_ambiguous_saves_storage_refused_before_delete(monkeypatch):
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp.errors import AmbiguousPathError

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    class Session:
        def storages(self):
            return [('one', '7: Saves'), ('two', '7: Saves')]
        def resolve_read_path(self, *args):
            pytest.fail('ambiguous storage traversed')
    monkeypatch.setattr(backend, '_wpd_session', lambda: Session())
    with pytest.raises(AmbiguousPathError):
        backend.delete_save_object('SAVES', ROOT, ROOT + '/file', expected_session='live')


def test_real_save_write_refuses_manual_mapping_of_other_storage(monkeypatch):
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp.errors import UnsupportedOperationError

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    backend.set_storage_overrides({'1: SD Card': 'SAVES'})
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    class Session:
        def storages(self):
            return [('sd', '1: SD Card')]
        def resolve_read_path(self, *args):
            pytest.fail('non-Saves storage traversed')
    monkeypatch.setattr(backend, '_wpd_session', lambda: Session())
    assert backend.capabilities()['save_write'] is False
    with pytest.raises(UnsupportedOperationError):
        backend.delete_save_object('SAVES', ROOT, ROOT + '/file', expected_session='live')


def test_real_commit_timeout_disconnects_and_reports_unknown(tmp_path, monkeypatch):
    from switchagent.mtp import wpd
    from switchagent.mtp.windows import RealMtpBackend

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    class Session:
        def children(self, parent):
            return []
        def write_save_file(self, *args, **kwargs):
            raise wpd.ComError(wpd.ERROR_SEM_TIMEOUT_HRESULT, 'IStream::Commit')
    monkeypatch.setattr(backend, '_save_write_context', lambda *args: (
        Session(), 'root', 'root', 'file', ROOT + '/file', 'live'))
    source = tmp_path / 'file'
    source.write_bytes(b'data')
    with pytest.raises(TransferFailedError, match='state unknown'):
        backend.write_save_file('SAVES', ROOT, ROOT + '/file', source,
                                replace=False, expected_session='live')
    assert not backend.is_connected


@pytest.mark.parametrize('overall,item_hr,should_pass', [
    (0, 0, True),
    (1, 0, False),  # S_FALSE means partial failure even if one item says S_OK.
    (0, 0x80070005, False),
])
def test_wpd_delete_checks_overall_and_per_object_hresult(monkeypatch, overall, item_hr, should_pass):
    import ctypes
    from switchagent.mtp import wpd

    calls = []
    monkeypatch.setattr(wpd, 'co_create', lambda *args: ctypes.c_void_p(11))
    monkeypatch.setattr(wpd, 'release', lambda ptr: calls.append(('release', ptr.value)))

    def fake_vcall(ptr, index, argtypes, *args, **kwargs):
        calls.append((ptr.value, index))
        if ptr.value == 22 and index == 8:
            assert args[0] == 0  # non-recursive by default
            args[2]._obj.value = 33
            return overall
        if ptr.value == 33 and index == 3:
            args[0]._obj.value = 1
        if ptr.value == 33 and index == 4:
            result = args[1]._obj
            result.vt = 10
            for i, byte in enumerate(item_hr.to_bytes(4, 'little')):
                result.data[i] = byte
        return 0

    monkeypatch.setattr(wpd, 'vcall', fake_vcall)
    if should_pass:
        wpd.delete_object(ctypes.c_void_p(22), 'object-id')
    else:
        with pytest.raises(wpd.ComError):
            wpd.delete_object(ctypes.c_void_p(22), 'object-id')
    assert (11, 5) in calls
    assert (22, 8) in calls
    assert ('release', 11) in calls


@pytest.mark.parametrize('stage,should_revert', [('stream', True), ('commit', False)])
def test_wpd_save_stream_reverts_before_commit_but_never_after_timeout(
        tmp_path, monkeypatch, stage, should_revert):
    from switchagent.mtp import wpd

    source = tmp_path / 'file'
    source.write_bytes(b'content')
    session = wpd.WpdSession('fake')
    session._content = object()
    calls = []
    stream = object()
    monkeypatch.setattr(wpd, 'create_file_object', lambda *args: (stream, 8))
    monkeypatch.setattr(wpd, 'release', lambda ptr: calls.append('release'))
    monkeypatch.setattr(wpd, 'stream_revert', lambda ptr: calls.append('revert'))
    monkeypatch.setattr(wpd, 'stream_write', lambda *args: (
        (_ for _ in ()).throw(wpd.ComError(0x80004005, 'stream'))
        if stage == 'stream' else calls.append('write')))
    monkeypatch.setattr(wpd, 'stream_commit', lambda ptr: (
        (_ for _ in ()).throw(wpd.ComError(wpd.ERROR_SEM_TIMEOUT_HRESULT, 'commit'))))
    with pytest.raises(wpd.ComError):
        session.write_save_file('parent', 'file', source, check_session=lambda: None)
    assert ('revert' in calls) is should_revert
    assert calls[-1] == 'release'
