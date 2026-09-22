"""Public service seams: inventory, durable snapshots, interchange, restore."""

from pathlib import Path
import json
import zipfile

import pytest

from switchagent.mtp.mock import MockMtpBackend
from switchagent.backup_manager import BackupManager, BackupError


def populated_backend():
    backend = MockMtpBackend()
    saves = backend.add_storage('SAVES')
    saves.ensure_directory('Installed games/Game A/Profile A/Default')
    saves.write_file('Installed games/Game A/Profile A/Default/main.dat', b'original progress')
    saves.ensure_directory('Installed games/Game B/Profile B/Default')
    saves.write_file('Installed games/Game B/Profile B/Default/main.dat', b'other')
    games = backend.add_storage('INSTALLED_GAMES')
    games.write_file('Game A.nsp', b'package')
    backend.connect()
    backend.set_save_identity('SAVES', 'Installed games/Game A/Profile A/Default',
                              title_id='0100000000000001', user_id='uid-a',
                              environment_id='installation-a')
    backend.set_save_identity('SAVES', 'Installed games/Game B/Profile B/Default',
                              title_id='0100000000000002', user_id='uid-b',
                              environment_id='installation-a')
    return backend


def test_inventory_reports_verified_account_saves_and_game_packages(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'backups')
    saves = service.inventory_saves(backend)
    games = service.inventory_games(backend)
    assert [(item['path'], item['size'], item['identity']['user_id']) for item in saves] == [
        ('Installed games/Game A/Profile A/Default', 17, 'uid-a'),
        ('Installed games/Game B/Profile B/Default', 5, 'uid-b'),
    ]
    assert [(item['path'], item['size']) for item in games] == [('Game A.nsp', 7)]


def test_snapshot_persists_files_and_hashes_without_device_writes(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'backups')
    path = 'Installed games/Game A/Profile A/Default'
    snapshot = service.create_snapshot(backend, path)
    assert snapshot['state'] == 'ready'
    assert snapshot['identity']['user_id'] == 'uid-a'
    assert snapshot['files'] == [{
        'path': 'main.dat', 'size': 17,
        'sha256': '8489ba24525bfe8c7561c8fe378d9d922933f3760d7e254cfe337de6c34a717e',
    }]
    assert (tmp_path / 'backups' / 'snapshots' / snapshot['id'] / 'files' / 'main.dat').read_bytes() == b'original progress'
    assert BackupManager(tmp_path / 'backups').list_snapshots()[0]['id'] == snapshot['id']
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


def test_short_device_read_retains_incomplete_marker_and_never_publishes(tmp_path):
    backend = populated_backend()
    backend.arm_read_failure('short', storage='SAVES',
                             source_path='Installed games/Game A/Profile A/Default/main.dat')
    service = BackupManager(tmp_path / 'backups')
    with pytest.raises(Exception, match='short source'):
        service.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    assert service.list_snapshots() == []
    assert len(service.list_incomplete()) == 1
    assert service.list_incomplete()[0]['state'] == 'incomplete'


def test_group_zip_is_flat_and_imports_verified_snapshots(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    paths = ['Installed games/Game A/Profile A/Default',
             'Installed games/Game B/Profile B/Default']
    originals = service.create_snapshots(backend, paths)
    archive = service.create_archive([item['id'] for item in originals], tmp_path / 'group.zip')
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert names.count('manifest.json') == 1
        assert len(names) == 3
        assert all(name == 'manifest.json' or name.startswith('snapshots/') for name in names)
    imported = BackupManager(tmp_path / 'imported').import_archive(archive)
    assert [item['identity']['user_id'] for item in imported] == ['uid-a', 'uid-b']
    assert all(item['state'] == 'ready' for item in imported)
    assert all(item['id'] != originals[i]['id'] for i, item in enumerate(imported))


def test_archive_keeps_existing_destination_untouched(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    snapshot = service.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    destination = tmp_path / 'existing.zip'
    destination.write_bytes(b'user owned archive')
    with pytest.raises(FileExistsError):
        service.create_archive([snapshot['id']], destination)
    assert destination.read_bytes() == b'user owned archive'


def test_imported_snapshot_restores_exact_mock_target_after_prebackup(tmp_path):
    backend = populated_backend()
    path = 'Installed games/Game A/Profile A/Default'
    original = BackupManager(tmp_path / 'first')
    source = original.create_snapshot(backend, path)
    archive = original.create_archive([source['id']], tmp_path / 'save.zip')
    restored = BackupManager(tmp_path / 'second')
    imported = restored.import_archive(archive)[0]
    tree = backend.storage_tree('SAVES')
    tree.write_file(path + '/main.dat', b'new progress')
    tree.write_file(path + '/extra.dat', b'new extra')
    plan = restored.prepare_restore(backend, imported['id'])
    assert plan['prebackup_id'] in [s['id'] for s in restored.list_snapshots()]
    outcome = restored.confirm_restore(backend, plan['id'])
    assert outcome['state'] == 'completed'
    assert outcome['verification'] == 'readback-hash'
    assert tree.read_file(path + '/main.dat') == b'original progress'
    assert not tree.exists(path + '/extra.dat')


def test_selected_game_package_exports_to_local_store(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    exports = service.export_games(backend, ['Game A.nsp'])
    assert len(exports) == 1
    assert exports[0]['size'] == 7
    assert exports[0]['source_path'] == 'Game A.nsp'
    assert (tmp_path / 'store' / 'game-exports' / exports[0]['id'] / 'Game A.nsp').read_bytes() == b'package'


def test_restore_refuses_unlisted_file_in_snapshot_tree(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    source = service.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    extra = tmp_path / 'store' / 'snapshots' / source['id'] / 'files' / 'rogue.dat'
    extra.write_bytes(b'not in manifest')
    with pytest.raises(BackupError, match='unlisted'):
        service.prepare_restore(backend, source['id'])
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


def test_import_rejects_manifest_missing_parent_directory(tmp_path):
    service = BackupManager(tmp_path / 'store')
    original_id = 'a' * 32
    archive = tmp_path / 'invalid.zip'
    manifest = {'format': 'switchagent-backup', 'version': 1, 'snapshots': [{
        'format': 'switchagent-backup', 'version': 1, 'id': original_id,
        'state': 'ready', 'kind': 'save', 'storage': 'SAVES',
        'source_path': 'Game/Profile', 'identity': {}, 'created_at': 1,
        'directories': [], 'files': [{'path': 'nested/progress', 'size': 1,
                                      'sha256': '2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881'}],
    }]}
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('manifest.json', json.dumps(manifest))
        bundle.writestr(f'snapshots/{original_id}/files/nested/progress', b'x')
    with pytest.raises(BackupError, match='parent'):
        service.import_archive(archive)
    assert service.list_snapshots() == []


def test_operation_journal_survives_manager_restart(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    snapshot = service.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    events = BackupManager(tmp_path / 'store').list_events()
    assert any(event['kind'] == 'snapshot' and event['state'] == 'ready'
               and event['snapshot_id'] == snapshot['id'] for event in events)


def test_source_changed_during_target_recheck_refuses_before_write(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A/Default'
    source = service.create_snapshot(backend, path)
    plan = service.prepare_restore(backend, source['id'])
    source_file = tmp_path / 'store' / 'snapshots' / source['id'] / 'files' / 'main.dat'
    original_receive = backend.receive_file
    def mutate_after_target_read(*args, **kwargs):
        result = original_receive(*args, **kwargs)
        source_file.write_bytes(b'x' * 17)
        return result
    monkeypatch.setattr(backend, 'receive_file', mutate_after_target_read)
    with pytest.raises(BackupError, match='source changed'):
        service.confirm_restore(backend, plan['id'])
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


def test_restore_diagnostics_names_unproven_identity_and_capability(tmp_path):
    backend = populated_backend()
    path = 'Installed games/Game A/Profile A/Default'
    backend.set_save_identity('SAVES', path, title_id=None, user_id=None,
                              environment_id=None, verified=False)
    diagnostics = BackupManager(tmp_path / 'store').restore_diagnostics(backend, path)
    assert diagnostics['eligible'] is False
    assert {'title_id', 'user_id', 'environment_id'} <= set(diagnostics['missing'])


def test_snapshot_remains_ready_when_journal_fails_after_publish(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    monkeypatch.setattr(service, '_record', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('journal full')))
    snapshot = service.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    assert snapshot['state'] == 'ready'
    assert [item['id'] for item in service.list_snapshots()] == [snapshot['id']]
    assert service.list_incomplete() == []


def test_target_changed_after_first_recheck_refuses_before_write(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A/Default'
    source = service.create_snapshot(backend, path)
    plan = service.prepare_restore(backend, source['id'])
    original_identity = backend.save_identity
    calls = 0

    def change_during_final_identity_check(*args, **kwargs):
        nonlocal calls
        calls += 1
        identity = original_identity(*args, **kwargs)
        if calls == 2:
            backend.storage_tree('SAVES').write_file(path + '/main.dat', b'progress changed again')
        return identity

    monkeypatch.setattr(backend, 'save_identity', change_during_final_identity_check)
    with pytest.raises(BackupError, match='target progress changed'):
        service.confirm_restore(backend, plan['id'])
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


def test_import_rejects_crc_tampering_as_backup_error(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'source')
    snapshot = service.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    valid = service.create_archive([snapshot['id']], tmp_path / 'valid.zip')
    tampered = tmp_path / 'tampered.zip'
    with zipfile.ZipFile(valid) as reader, zipfile.ZipFile(tampered, 'w', compression=zipfile.ZIP_STORED) as writer:
        for name in reader.namelist():
            writer.writestr(name, reader.read(name))
    raw = tampered.read_bytes()
    assert raw.count(b'original progress') == 1
    tampered.write_bytes(raw.replace(b'original progress', b'Xriginal progress'))
    imported = BackupManager(tmp_path / 'imported')
    with pytest.raises(BackupError, match='ZIP'):
        imported.import_archive(tampered)
    assert imported.list_snapshots() == []


@pytest.mark.parametrize('device_name', ['CONIN$', 'CONOUT$', 'COM¹', 'LPT²'])
def test_import_rejects_windows_device_names_before_extracting(tmp_path, device_name):
    archive = tmp_path / 'reserved.zip'
    original_id = 'a' * 32
    manifest = {'format': 'switchagent-backup', 'version': 1, 'snapshots': [{
        'format': 'switchagent-backup', 'version': 1, 'id': original_id,
        'state': 'ready', 'kind': 'save', 'storage': 'SAVES',
        'source_path': 'Game/Profile', 'identity': {}, 'created_at': 1,
        'directories': [], 'files': [{'path': device_name, 'size': 1,
                                      'sha256': '2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881'}],
    }]}
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('manifest.json', json.dumps(manifest))
        bundle.writestr(f'snapshots/{original_id}/files/{device_name}', b'x')
    service = BackupManager(tmp_path / 'store')
    with pytest.raises(BackupError, match='unsafe archive path'):
        service.import_archive(archive)
    assert service.list_snapshots() == []


def test_journal_symlink_cannot_redirect_writes_outside_store(tmp_path):
    service = BackupManager(tmp_path / 'store')
    outside = tmp_path / 'outside.txt'
    outside.write_text('sentinel', encoding='utf-8')
    try:
        (service.root / 'events.jsonl').symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'file symlinks unavailable: {exc}')
    snapshot = service.create_snapshot(populated_backend(),
                                       'Installed games/Game A/Profile A/Default')
    assert snapshot['state'] == 'ready'
    assert outside.read_text(encoding='utf-8') == 'sentinel'
    assert snapshot['journal_warning'] == 'event journal unavailable'


def test_journal_reparse_path_is_rejected_before_write(tmp_path, monkeypatch):
    from switchagent import backup_manager as module
    service = BackupManager(tmp_path / 'store')
    journal = service.root / 'events.jsonl'
    journal.touch()
    original_predicate = module.is_link_or_reparse
    monkeypatch.setattr(module, 'is_link_or_reparse',
                        lambda path: Path(path) == journal or original_predicate(path))
    snapshot = service.create_snapshot(populated_backend(),
                                       'Installed games/Game A/Profile A/Default')
    assert snapshot['state'] == 'ready'
    assert snapshot['journal_warning'] == 'event journal unavailable'
    assert journal.read_bytes() == b''


@pytest.mark.parametrize('field,replacement', [
    ('title_id', '0100000000000002'),
    ('user_id', 'uid-other'),
    ('environment_id', 'installation-other'),
    ('save_type', 'Device'),
])
def test_restore_refuses_foreign_target_identity_before_prebackup(tmp_path, field, replacement):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A/Default'
    source = service.create_snapshot(backend, path)
    identity = backend.save_identity('SAVES', path)
    identity[field] = replacement
    backend.set_save_identity('SAVES', path, **{
        key: identity[key] for key in ('title_id', 'user_id', 'environment_id', 'save_type', 'verified')
    })
    with pytest.raises(BackupError, match='identity'):
        service.prepare_restore(backend, source['id'])
    assert [item['id'] for item in service.list_snapshots()] == [source['id']]
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


def test_reconnect_consumes_restore_plan_without_writing(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A/Default'
    source = service.create_snapshot(backend, path)
    plan = service.prepare_restore(backend, source['id'])
    backend.disconnect()
    backend.connect()
    with pytest.raises(BackupError, match='connection changed'):
        service.confirm_restore(backend, plan['id'])
    with pytest.raises(BackupError, match='already consumed'):
        service.confirm_restore(backend, plan['id'])
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


def test_restore_disconnect_attempts_write_once_and_keeps_prebackup(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A/Default'
    source = service.create_snapshot(backend, path)
    plan = service.prepare_restore(backend, source['id'])
    backend.arm_failure('disconnect', storage='SAVES', dest_path=path)
    with pytest.raises(BackupError, match='target state is unknown'):
        service.confirm_restore(backend, plan['id'])
    assert [item.operation for item in backend.operation_log].count('REPLACE_SAVE_TREE') == 1
    assert plan['prebackup_id'] in {item['id'] for item in service.list_snapshots()}
    assert any(event['kind'] == 'restore' and event['state'] == 'unknown'
               for event in service.list_events())
    with pytest.raises(BackupError, match='already consumed'):
        service.confirm_restore(backend, plan['id'])


def test_snapshot_cancel_after_first_file_stays_incomplete(tmp_path):
    from switchagent.mtp.errors import OperationCancelledError
    backend = populated_backend()
    path = 'Installed games/Game A/Profile A/Default'
    backend.storage_tree('SAVES').write_file(path + '/second.dat', b'second')
    service = BackupManager(tmp_path / 'store')
    stopped = False

    def mark_progress(done, total):
        nonlocal stopped
        stopped = done > 0

    with pytest.raises(OperationCancelledError):
        service.create_snapshot(backend, path, cancel=lambda: stopped,
                                progress=mark_progress)
    assert service.list_snapshots() == []
    assert len(service.list_incomplete()) == 1
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


def test_local_disk_fault_does_not_publish_partial_snapshot(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')

    def fail_local_write(storage, source_path, destination_path, **kwargs):
        Path(destination_path).write_bytes(b'partial')
        raise OSError('disk full')

    monkeypatch.setattr(backend, 'receive_file', fail_local_write)
    with pytest.raises(OSError, match='disk full'):
        service.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    assert service.list_snapshots() == []
    assert len(service.list_incomplete()) == 1
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


@pytest.mark.parametrize('extra_name', [
    '../escape.dat',
    'snapshots/{id}/files/MAIN.DAT',
])
def test_import_rejects_escape_and_case_colliding_members(tmp_path, extra_name):
    backend = populated_backend()
    source_service = BackupManager(tmp_path / 'source')
    snapshot = source_service.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    valid = source_service.create_archive([snapshot['id']], tmp_path / 'valid.zip')
    invalid = tmp_path / 'invalid.zip'
    with zipfile.ZipFile(valid) as reader, zipfile.ZipFile(invalid, 'w') as writer:
        for name in reader.namelist():
            writer.writestr(name, reader.read(name))
        writer.writestr(extra_name.format(id=snapshot['id']), b'bad')
    service = BackupManager(tmp_path / 'imported')
    with pytest.raises(BackupError):
        service.import_archive(invalid)
    assert service.list_snapshots() == []


def test_published_archive_import_and_game_survive_journal_fault(tmp_path, monkeypatch):
    backend = populated_backend()
    source = BackupManager(tmp_path / 'source')
    snapshot = source.create_snapshot(backend, 'Installed games/Game A/Profile A/Default')
    monkeypatch.setattr(source, '_record', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('journal full')))
    archive = source.create_archive([snapshot['id']], tmp_path / 'ready.zip')
    assert archive.is_file()
    imported_store = BackupManager(tmp_path / 'imported')
    monkeypatch.setattr(imported_store, '_record', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('journal full')))
    imported = imported_store.import_archive(archive)
    assert len(imported) == 1
    assert imported[0]['journal_warning'] == 'event journal unavailable'
    assert imported_store.list_snapshots()[0]['id'] == imported[0]['id']
    game_store = BackupManager(tmp_path / 'games')
    monkeypatch.setattr(game_store, '_record', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('journal full')))
    exported = game_store.export_games(backend, ['Game A.nsp'])
    assert exported[0]['journal_warning'] == 'event journal unavailable'
    assert game_store.game_export_path(exported[0]['id']).read_bytes() == b'package'
