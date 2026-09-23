from pathlib import Path

import pytest

from switchagent.mtp.mock import MockMtpBackend


def test_download_refuses_parent_reparse_attribute(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from switchagent.mtp.errors import TransferFailedError
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == tmp_path:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', lstat)
    backend = MockMtpBackend()
    backend.add_storage('SAVES').write_file('data', b'abc')
    backend.connect()
    with pytest.raises(TransferFailedError, match='link'):
        backend.receive_file('SAVES', 'data', tmp_path / 'copy')
    assert not (tmp_path / 'copy').exists()


def test_mock_reads_nested_files_without_device_writes(tmp_path):
    backend = MockMtpBackend()
    tree = backend.add_storage('SAVES')
    tree.ensure_directory('Game/Profile/empty')
    tree.write_file('Game/Profile/data', b'progress')
    backend.connect()
    entries = backend.list_directory('SAVES', 'Game/Profile')
    assert [(e.name, e.is_dir, e.size) for e in entries] == [('data', False, 8), ('empty', True, None)]
    target = tmp_path / 'copy'
    assert backend.receive_file('SAVES', 'Game/Profile/data', target) == 8
    assert target.read_bytes() == b'progress'
    assert tree.read_file('Game/Profile/data') == b'progress'


def test_restore_refuses_source_reparse_before_traversal(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from switchagent.mtp.errors import InvalidOperationError
    backend = MockMtpBackend()
    tree = backend.add_storage('SAVES')
    tree.ensure_directory('Game/Profile')
    tree.write_file('Game/Profile/old', b'original')
    backend.connect()
    backend.set_save_identity('SAVES', 'Game/Profile', title_id='title',
                              user_id='user', environment_id='console')
    (tmp_path / 'new').write_bytes(b'new')
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == tmp_path:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', lstat)
    def no_traverse(*args, **kwargs):
        pytest.fail('traversed rejected reparse source')
    monkeypatch.setattr(Path, 'rglob', no_traverse)
    with pytest.raises(InvalidOperationError, match='link'):
        backend.replace_save_tree('SAVES', 'Game/Profile', tmp_path,
            expected_identity=backend.save_identity('SAVES', 'Game/Profile'),
            expected_session=backend.session_token)
    assert tree.read_file('Game/Profile/old') == b'original'


def test_wpd_enumeration_keeps_duplicates_and_refuses_ambiguous_path(monkeypatch):
    from switchagent.mtp import wpd
    from switchagent.mtp.errors import AmbiguousPathError
    session = wpd.WpdSession('fake')
    monkeypatch.setattr(wpd, 'enum_children', lambda *args: ['1', '2'])
    monkeypatch.setattr(wpd, 'read_props', lambda *args: {str(wpd.WPD_OBJECT_NAME): 'same'})
    assert [obj for obj, props in session.children('root')] == ['1', '2']
    with pytest.raises(AmbiguousPathError):
        session.resolve_read_path('root', 'same')


def test_real_enumeration_unknown_size_and_identity_are_not_guessed(monkeypatch):
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp import wpd
    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    session = wpd.WpdSession('fake')
    monkeypatch.setattr(backend, '_wpd_session', lambda: session)
    monkeypatch.setattr(session, 'storages', lambda: [('root', '7: Saves')])
    monkeypatch.setattr(session, 'children', lambda obj: [('a', {str(wpd.WPD_OBJECT_NAME): 'profile'})])
    result = backend.list_directory('SAVES')
    assert len(result) == 1
    assert result[0].size is None
    assert result[0].metadata['type_known'] is False
    assert backend.save_identity('SAVES', 'profile')['verified'] is False


def test_real_unspecified_save_with_default_resource_can_be_read(tmp_path, monkeypatch):
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp import wpd

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    session = wpd.WpdSession('fake')
    monkeypatch.setattr(backend, '_wpd_session', lambda: session)
    monkeypatch.setattr(session, 'storages', lambda: [('root', '7: Saves')])
    props = {
        str(wpd.WPD_OBJECT_ORIGINAL_FILE_NAME): 'data',
        str(wpd.WPD_OBJECT_CONTENT_TYPE): str(wpd.WPD_CONTENT_TYPE_UNSPECIFIED),
        str(wpd.WPD_OBJECT_SIZE): 3,
    }
    monkeypatch.setattr(session, 'children', lambda obj: [('object', props)])
    monkeypatch.setattr(wpd, 'read_props', lambda *_: props)
    monkeypatch.setattr(session, 'supported_resources', lambda obj: [wpd.WPD_RESOURCE_DEFAULT])
    monkeypatch.setattr(session, 'receive_file', lambda obj, dest, **kwargs: dest.write_bytes(b'abc'))

    entry = backend.list_directory('SAVES')[0]
    assert (entry.is_dir, entry.size, entry.metadata['type_known']) == (False, 3, True)
    assert backend.stat('SAVES', 'data').metadata['type_known'] is True
    destination = tmp_path / 'data'
    assert backend.receive_file('SAVES', 'data', destination) == 3
    assert destination.read_bytes() == b'abc'


@pytest.mark.parametrize('original,keys,kind', [
    (None, 'default', 'unspecified'),
    ('data', 'missing', 'unspecified'),
    ('data', 'duplicate', 'unspecified'),
    ('data', 'default', 'unknown'),
])
def test_real_unspecified_file_proof_fails_closed(tmp_path, monkeypatch, original, keys, kind):
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp import wpd
    from switchagent.mtp.errors import InvalidOperationError

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    session = wpd.WpdSession('fake')
    monkeypatch.setattr(backend, '_wpd_session', lambda: session)
    monkeypatch.setattr(session, 'storages', lambda: [('root', '7: Saves')])
    props = {
        str(wpd.WPD_OBJECT_NAME): 'data',
        str(wpd.WPD_OBJECT_CONTENT_TYPE): str(wpd.WPD_CONTENT_TYPE_UNSPECIFIED)
            if kind == 'unspecified' else 'other',
        str(wpd.WPD_OBJECT_SIZE): 3,
    }
    if original is not None:
        props[str(wpd.WPD_OBJECT_ORIGINAL_FILE_NAME)] = original
    monkeypatch.setattr(session, 'children', lambda obj: [('object', props)])
    monkeypatch.setattr(wpd, 'read_props', lambda *_: props)
    resource_keys = {'default': [wpd.WPD_RESOURCE_DEFAULT], 'missing': [],
                     'duplicate': [wpd.WPD_RESOURCE_DEFAULT] * 2}[keys]
    monkeypatch.setattr(session, 'supported_resources', lambda obj: resource_keys)
    monkeypatch.setattr(session, 'receive_file', lambda *_args, **_kwargs:
                        pytest.fail('opened unverified resource'))

    assert backend.list_directory('SAVES')[0].metadata['type_known'] is False
    assert backend.stat('SAVES', 'data').metadata['type_known'] is False
    with pytest.raises(InvalidOperationError, match='known file'):
        backend.receive_file('SAVES', 'data', tmp_path / 'data')
    assert not (tmp_path / 'data').exists()


def test_real_folder_is_not_file_even_with_default_resource(tmp_path, monkeypatch):
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp import wpd
    from switchagent.mtp.errors import InvalidOperationError

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    session = wpd.WpdSession('fake')
    monkeypatch.setattr(backend, '_wpd_session', lambda: session)
    monkeypatch.setattr(session, 'storages', lambda: [('root', '7: Saves')])
    props = {
        str(wpd.WPD_OBJECT_ORIGINAL_FILE_NAME): 'folder',
        str(wpd.WPD_OBJECT_CONTENT_TYPE): str(wpd.WPD_CONTENT_TYPE_FOLDER),
    }
    monkeypatch.setattr(session, 'children', lambda obj: [('object', props)])
    monkeypatch.setattr(wpd, 'read_props', lambda *_: props)
    monkeypatch.setattr(session, 'supported_resources', lambda obj: [wpd.WPD_RESOURCE_DEFAULT])
    monkeypatch.setattr(session, 'receive_file', lambda *_args, **_kwargs:
                        pytest.fail('opened folder resource as file'))

    entry = backend.list_directory('SAVES')[0]
    assert (entry.is_dir, entry.metadata['type_known']) == (True, True)
    with pytest.raises(InvalidOperationError, match='known file'):
        backend.receive_file('SAVES', 'folder', tmp_path / 'folder')
    assert not (tmp_path / 'folder').exists()


@pytest.mark.parametrize('failure', [None, 'enumerate', 'count', 'at'])
def test_wpd_supported_resources_releases_com_objects(monkeypatch, failure):
    from switchagent.mtp import wpd

    released = []

    def call(pointer, slot, signature, *args, what='', **_kwargs):
        if what == 'Transfer':
            args[0]._obj.value = 101
        elif what == 'GetSupportedResources':
            args[1]._obj.value = 102
            if failure == 'enumerate':
                raise wpd.ComError(-1, what)
        elif what == 'ResourceKeys::GetCount':
            args[0]._obj.value = 1
            if failure == 'count':
                raise wpd.ComError(-1, what)
        elif what == 'ResourceKeys::GetAt':
            args[1]._obj.fmtid = wpd.WPD_RESOURCE_DEFAULT.fmtid
            args[1]._obj.pid = wpd.WPD_RESOURCE_DEFAULT.pid
            if failure == 'at':
                raise wpd.ComError(-1, what)
        else:
            pytest.fail(what)
        return 0

    monkeypatch.setattr(wpd, 'vcall', call)
    monkeypatch.setattr(wpd, 'release', lambda value: released.append(value.value))
    if failure is None:
        assert wpd.supported_resources(99, 'object') == [wpd.WPD_RESOURCE_DEFAULT]
    else:
        with pytest.raises(wpd.ComError):
            wpd.supported_resources(99, 'object')
    assert released == [102, 101]


def test_real_unspecified_resource_com_error_invalidates_session(monkeypatch):
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp import wpd
    from switchagent.mtp.errors import TransferFailedError

    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    session = wpd.WpdSession('fake')
    monkeypatch.setattr(backend, '_wpd_session', lambda: session)
    monkeypatch.setattr(session, 'storages', lambda: [('root', '7: Saves')])
    monkeypatch.setattr(session, 'children', lambda obj: [('object', {
        str(wpd.WPD_OBJECT_NAME): 'data',
        str(wpd.WPD_OBJECT_ORIGINAL_FILE_NAME): 'data',
        str(wpd.WPD_OBJECT_CONTENT_TYPE): str(wpd.WPD_CONTENT_TYPE_UNSPECIFIED),
    })])
    monkeypatch.setattr(session, 'supported_resources',
                        lambda obj: (_ for _ in ()).throw(wpd.ComError(-1, 'resources')))

    with pytest.raises(TransferFailedError):
        backend.list_directory('SAVES')
    assert not backend.is_connected


def test_mock_restore_replaces_only_verified_original_tree(tmp_path):
    from switchagent.mtp.errors import InvalidOperationError, StaleSessionError
    backend = MockMtpBackend()
    tree = backend.add_storage('SAVES')
    tree.ensure_directory('Game/One')
    tree.ensure_directory('Game/Two')
    tree.write_file('Game/One/old', b'old')
    tree.write_file('Game/Two/keep', b'keep')
    backend.connect()
    backend.set_save_identity('SAVES', 'Game/One', title_id='0100000000000000',
                              user_id='user-1', environment_id='mock-console-1')
    identity = backend.save_identity('SAVES', 'Game/One')
    token = backend.session_token
    backend.connect()
    assert token == backend.session_token
    (tmp_path / 'new').write_bytes(b'new')
    with pytest.raises(InvalidOperationError):
        backend.replace_save_tree('SAVES', 'Game/One', tmp_path,
            expected_identity={**identity, 'user_id': 'other'}, expected_session=token)
    backend.replace_save_tree('SAVES', 'Game/One', tmp_path,
        expected_identity=identity, expected_session=token)
    assert tree.list_files() == ['Game/One/new', 'Game/Two/keep']
    backend.disconnect()
    backend.connect()
    with pytest.raises(StaleSessionError):
        backend.list_directory('SAVES', expected_session=token)


@pytest.mark.parametrize('mode', ['short', 'disconnect', 'error'])
def test_read_failure_removes_partial_file_and_preserves_source(tmp_path, mode):
    from switchagent.mtp.errors import MtpError
    backend = MockMtpBackend()
    tree = backend.add_storage('SAVES')
    tree.write_file('data', b'abcdefgh')
    backend.connect()
    backend.arm_read_failure(mode, storage='SAVES', source_path='data')
    with pytest.raises(MtpError):
        backend.receive_file('SAVES', 'data', tmp_path / 'partial')
    assert not (tmp_path / 'partial').exists()
    assert tree.read_file('data') == b'abcdefgh'


def test_mock_presence_loss_invalidates_session_without_explicit_disconnect():
    from switchagent.mtp.errors import DeviceDisconnectedError, StaleSessionError
    backend = MockMtpBackend()
    backend.add_storage('SAVES')
    backend.connect()
    token = backend.session_token
    backend.set_device_present(False)
    with pytest.raises(DeviceDisconnectedError):
        backend.list_directory('SAVES')
    backend.set_device_present(True)
    backend.connect()
    with pytest.raises(StaleSessionError):
        backend.list_directory('SAVES', expected_session=token)


def test_real_failed_connect_expires_old_session(monkeypatch):
    from types import SimpleNamespace
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp.errors import DeviceNotFoundError
    backend = RealMtpBackend('fake')
    item = SimpleNamespace(GetFolder=object(), Name='DBI')
    monkeypatch.setattr(backend, '_find_device_item', lambda: item)
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    backend.connect()
    token = backend.session_token
    backend.connect()
    assert backend.session_token == token
    monkeypatch.setattr(backend, '_find_device_item', lambda: None)
    with pytest.raises(DeviceNotFoundError):
        backend.connect()
    monkeypatch.setattr(backend, '_find_device_item', lambda: item)
    backend.connect()
    assert backend.session_token != token


def test_real_wpd_access_denied_invalidates_read_session(monkeypatch):
    from switchagent.mtp import wpd
    from switchagent.mtp.windows import RealMtpBackend
    from switchagent.mtp.errors import ReadAccessDeniedError, DeviceDisconnectedError
    backend = RealMtpBackend('fake')
    backend._connected = True
    backend._read_session_token = 'live'
    monkeypatch.setattr(backend, '_device_currently_present', lambda: True)
    with pytest.raises(ReadAccessDeniedError):
        backend._read_failure(wpd.ComError(0x80070005, 'access denied'))
    with pytest.raises(DeviceDisconnectedError):
        _ = backend.session_token


@pytest.mark.parametrize('phase', ['before', 'during'])
def test_cancel_download_removes_partial_file(tmp_path, phase):
    from switchagent.mtp.errors import OperationCancelledError
    backend = MockMtpBackend()
    tree = backend.add_storage('SAVES')
    data = b'x' * (2 * 1024 * 1024)
    tree.write_file('data', data)
    backend.connect()
    destination = tmp_path / 'partial'
    def cancelled():
        return phase == 'before' or (destination.exists() and destination.stat().st_size > 0)
    with pytest.raises(OperationCancelledError):
        backend.receive_file('SAVES', 'data', destination, cancel=cancelled)
    assert not destination.exists()
    assert tree.read_file('data') == data


def test_restore_file_source_rejected_without_changing_target(tmp_path):
    from switchagent.mtp.errors import InvalidOperationError
    backend = MockMtpBackend()
    tree = backend.add_storage('SAVES')
    tree.ensure_directory('Game/Profile')
    tree.write_file('Game/Profile/old', b'original')
    backend.connect()
    backend.set_save_identity('SAVES', 'Game/Profile', title_id='title',
                              user_id='user', environment_id='console')
    source = tmp_path / 'file'
    source.write_bytes(b'new')
    with pytest.raises(InvalidOperationError, match='directory'):
        backend.replace_save_tree('SAVES', 'Game/Profile', source,
            expected_identity=backend.save_identity('SAVES', 'Game/Profile'),
            expected_session=backend.session_token)
    assert tree.list_files() == ['Game/Profile/old']
    assert tree.read_file('Game/Profile/old') == b'original'


@pytest.mark.parametrize('outcome', ['eof', 'short', 'hresult', 'count', 'getstream', 'cancel'])
def test_wpd_read_stream_contract_and_release(tmp_path, monkeypatch, outcome):
    import ctypes
    from switchagent.mtp import wpd
    from switchagent.mtp.errors import TransferFailedError, OperationCancelledError
    released = []
    counts = []
    read_index = 0
    def call(pointer, slot, signature, *args, what='', check=True):
        nonlocal read_index
        if what == 'Transfer':
            args[0]._obj.value = 101
        elif what == 'GetStream(read)':
            assert args[0] == 'object'
            assert args[2] == 0  # STGM_READ
            if outcome == 'getstream':
                raise wpd.ComError(-1, what)
            args[3]._obj.value = 2**31  # untrusted preferred chunk must be capped
            args[4]._obj.value = 102
        elif what == 'IStream::Read':
            counts.append(args[1])
            assert check is False
            if outcome == 'hresult':
                return -2147467259
            if outcome == 'count':
                args[2]._obj.value = args[1] + 1
                return 0
            payload = b'abc' if read_index == 0 else b''
            read_index += 1
            ctypes.memmove(args[0], payload, len(payload))
            args[2]._obj.value = len(payload)
            return 1  # S_FALSE is normal at EOF, including a final short chunk
        else:
            pytest.fail(what)
        return 0
    monkeypatch.setattr(wpd, 'vcall', call)
    monkeypatch.setattr(wpd, 'release', lambda value: released.append(value.value))
    destination = tmp_path / 'data'
    session = wpd.WpdSession('fake')
    if outcome == 'eof':
        assert session.receive_file('object', destination, size=3) == 3
        assert destination.read_bytes() == b'abc'
    else:
        error = (TransferFailedError if outcome == 'short' else
                 OperationCancelledError if outcome == 'cancel' else wpd.ComError)
        with pytest.raises(error):
            session.receive_file('object', destination, size=4,
                                 cancel=lambda: outcome == 'cancel')
        assert not destination.exists()
    assert released == ([101] if outcome == 'getstream' else [101, 102])
    assert all(count == 4 * 1024 * 1024 for count in counts)
    if outcome in ('getstream', 'cancel'):
        assert counts == []
    else:
        assert counts
