"""Public service seams: inventory, durable snapshots, interchange, restore."""

from pathlib import Path
from dataclasses import replace
import json
import zipfile

import pytest

from switchagent.mtp.mock import MockMtpBackend
from switchagent.backup_manager import BackupManager, BackupError, BackupLimits


def populated_backend():
    backend = MockMtpBackend()
    saves = backend.add_storage('SAVES')
    saves.ensure_directory('Installed games/Game A/Profile A')
    saves.write_file('Installed games/Game A/Profile A/main.dat', b'original progress')
    saves.ensure_directory('Installed games/Game B/Profile B')
    saves.write_file('Installed games/Game B/Profile B/main.dat', b'other')
    games = backend.add_storage('INSTALLED_GAMES')
    games.write_file('Game A.nsp', b'package')
    backend.connect()
    backend.set_save_identity('SAVES', 'Installed games/Game A/Profile A',
                              title_id='0100000000000001', user_id='uid-a',
                              environment_id='installation-a')
    backend.set_save_identity('SAVES', 'Installed games/Game B/Profile B',
                              title_id='0100000000000002', user_id='uid-b',
                              environment_id='installation-a')
    return backend


def test_inventory_reports_verified_account_saves_and_game_packages(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'backups')
    saves = service.inventory_saves(backend)
    games = service.inventory_games(backend)
    assert [(item['path'], item['size'], item['identity']['user_id']) for item in saves] == [
        ('Installed games/Game A/Profile A', 17, 'uid-a'),
        ('Installed games/Game B/Profile B', 5, 'uid-b'),
    ]
    assert [(item['path'], item['size']) for item in games] == [('Game A.nsp', 7)]


def test_inventory_uses_only_dbi_profile_roots_and_counts_nested_files(tmp_path):
    backend = populated_backend()
    tree = backend.storage_tree('SAVES')
    tree.ensure_directory('Installed games/Game A/Profile A/nested/deeper')
    tree.write_file('Installed games/Game A/Profile A/nested/deeper/extra.dat', b'abc')
    rows = BackupManager(tmp_path / 'store').inventory_saves(backend)
    assert len(rows) == 2
    profile = next(row for row in rows if row['name'] == 'Profile A')
    assert (profile['size'], profile['file_count'], profile['selectable']) == (20, 2, True)
    assert all(len(row['path'].split('/')) == 3 for row in rows)


def test_device_game_and_profile_names_may_contain_windows_forbidden_characters(tmp_path):
    backend = populated_backend()
    path = 'Uninstalled games/Game: Special?/Player: One'
    tree = backend.storage_tree('SAVES')
    tree.ensure_directory(path)
    tree.write_file(path + '/Default', b'save')
    service = BackupManager(tmp_path / 'store')
    row = next(row for row in service.inventory_saves(backend) if row['path'] == path)
    assert row['selectable'] is True
    snapshot = service.create_snapshot(backend, path)
    assert snapshot['source_path'] == path
    assert snapshot['files'][0]['path'] == 'Default'


def test_bad_save_subtree_is_unselectable_without_hiding_other_saves(tmp_path):
    backend = populated_backend()
    bad = 'Installed games/Game A/Profile A'
    backend.storage_tree('SAVES').write_file(bad + '/bad:name', b'bad')
    service = BackupManager(tmp_path / 'store')
    rows = service.inventory_saves(backend)
    assert len(rows) == 2
    assert next(row for row in rows if row['path'] == bad)['selectable'] is False
    assert next(row for row in rows if row['name'] == 'Profile B')['selectable'] is True
    with pytest.raises(BackupError, match='unsafe'):
        service.create_snapshot(backend, bad)


def test_unreadable_game_folder_does_not_hide_other_games(tmp_path, monkeypatch):
    from switchagent.mtp.errors import ReadAccessDeniedError
    backend = populated_backend()
    original_list = backend.list_directory
    bad = 'Installed games/Game A'

    def list_with_one_unreadable_game(storage, path='', **kwargs):
        if path == bad:
            raise ReadAccessDeniedError('one game cannot be enumerated')
        return original_list(storage, path, **kwargs)

    monkeypatch.setattr(backend, 'list_directory', list_with_one_unreadable_game)
    rows = BackupManager(tmp_path / 'store').inventory_saves(backend)
    assert next(row for row in rows if row['path'] == bad)['selectable'] is False
    assert next(row for row in rows if row['name'] == 'Profile B')['selectable'] is True


def test_unreadable_save_group_does_not_hide_other_group(tmp_path, monkeypatch):
    from switchagent.mtp.errors import AmbiguousPathError
    backend = populated_backend()
    tree = backend.storage_tree('SAVES')
    tree.ensure_directory('Uninstalled games/Game C/Profile C')
    tree.write_file('Uninstalled games/Game C/Profile C/data', b'progress')
    original_list = backend.list_directory

    def list_with_one_ambiguous_group(storage, path='', **kwargs):
        if path == 'Installed games':
            raise AmbiguousPathError('one group cannot be enumerated')
        return original_list(storage, path, **kwargs)

    monkeypatch.setattr(backend, 'list_directory', list_with_one_ambiguous_group)
    rows = BackupManager(tmp_path / 'store').inventory_saves(backend)
    assert next(row for row in rows if row['path'] == 'Installed games')['selectable'] is False
    assert next(row for row in rows if row['name'] == 'Profile C')['selectable'] is True


def test_inventory_does_not_mask_disconnect_as_one_bad_game(tmp_path, monkeypatch):
    from switchagent.mtp.errors import DeviceDisconnectedError, ReadAccessDeniedError
    backend = populated_backend()
    original_list = backend.list_directory

    def disconnect_during_game_list(storage, path='', **kwargs):
        if path == 'Installed games/Game A':
            backend.simulate_disconnect()
            raise ReadAccessDeniedError('connection ended during game listing')
        return original_list(storage, path, **kwargs)

    monkeypatch.setattr(backend, 'list_directory', disconnect_during_game_list)
    with pytest.raises(DeviceDisconnectedError):
        BackupManager(tmp_path / 'store').inventory_saves(backend)


def test_game_inventory_excludes_dbi_csv_and_non_packages(tmp_path):
    backend = populated_backend()
    tree = backend.storage_tree('INSTALLED_GAMES')
    tree.write_file('InstalledApplications.csv', b'csv')
    tree.write_file('metadata.txt', b'text')
    tree.write_file('Other.NSZ', b'nsz')
    tree.write_file('Third.xci', b'xci')
    tree.write_file('Fourth.xcz', b'xcz')
    paths = {row['path'] for row in BackupManager(tmp_path / 'store').inventory_games(backend)}
    assert paths == {'Game A.nsp', 'Other.NSZ', 'Third.xci', 'Fourth.xcz'}


def test_snapshot_origin_uses_device_fingerprint_and_dbi_path(tmp_path):
    from switchagent.mtp.windows import device_fingerprint
    backend = populated_backend()
    path = 'Installed games/Game A/Profile A'
    snapshot = BackupManager(tmp_path / 'store').create_snapshot(backend, path)
    assert snapshot['origin'] == {
        'device_fingerprint': device_fingerprint(backend.device_id),
        'group': 'Installed games', 'game': 'Game A', 'profile': 'Profile A'}
    assert backend.device_id not in json.dumps(snapshot['origin'])


def test_snapshot_persists_files_and_hashes_without_device_writes(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'backups')
    path = 'Installed games/Game A/Profile A'
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
                             source_path='Installed games/Game A/Profile A/main.dat')
    service = BackupManager(tmp_path / 'backups')
    with pytest.raises(Exception, match='short source'):
        service.create_snapshot(backend, 'Installed games/Game A/Profile A')
    assert service.list_snapshots() == []
    assert len(service.list_incomplete()) == 1
    assert service.list_incomplete()[0]['state'] == 'incomplete'


def test_group_zip_is_flat_and_imports_verified_snapshots(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    paths = ['Installed games/Game A/Profile A',
             'Installed games/Game B/Profile B']
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
    snapshot = service.create_snapshot(backend, 'Installed games/Game A/Profile A')
    destination = tmp_path / 'existing.zip'
    destination.write_bytes(b'user owned archive')
    with pytest.raises(FileExistsError):
        service.create_archive([snapshot['id']], destination)
    assert destination.read_bytes() == b'user owned archive'


def test_imported_snapshot_restores_exact_mock_target_after_prebackup(tmp_path):
    backend = populated_backend()
    path = 'Installed games/Game A/Profile A'
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
    source = service.create_snapshot(backend, 'Installed games/Game A/Profile A')
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
    snapshot = service.create_snapshot(backend, 'Installed games/Game A/Profile A')
    events = BackupManager(tmp_path / 'store').list_events()
    assert any(event['kind'] == 'snapshot' and event['state'] == 'ready'
               and event['snapshot_id'] == snapshot['id'] for event in events)


def test_source_changed_during_target_recheck_refuses_before_write(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A'
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
    path = 'Installed games/Game A/Profile A'
    backend.set_save_identity('SAVES', path, title_id=None, user_id=None,
                              environment_id=None, save_type=None, verified=False)
    diagnostics = BackupManager(tmp_path / 'store').restore_diagnostics(backend, path)
    assert diagnostics['eligible'] is False
    assert {'title_id', 'user_id', 'environment_id'} <= set(diagnostics['missing'])


def test_snapshot_remains_ready_when_journal_fails_after_publish(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    monkeypatch.setattr(service, '_record', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('journal full')))
    snapshot = service.create_snapshot(backend, 'Installed games/Game A/Profile A')
    assert snapshot['state'] == 'ready'
    assert [item['id'] for item in service.list_snapshots()] == [snapshot['id']]
    assert service.list_incomplete() == []


def test_target_changed_after_first_recheck_refuses_before_write(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A'
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
    snapshot = service.create_snapshot(backend, 'Installed games/Game A/Profile A')
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
                                       'Installed games/Game A/Profile A')
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
                                       'Installed games/Game A/Profile A')
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
    path = 'Installed games/Game A/Profile A'
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
    path = 'Installed games/Game A/Profile A'
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
    path = 'Installed games/Game A/Profile A'
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
    path = 'Installed games/Game A/Profile A'
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
        service.create_snapshot(backend, 'Installed games/Game A/Profile A')
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
    snapshot = source_service.create_snapshot(backend, 'Installed games/Game A/Profile A')
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
    snapshot = source.create_snapshot(backend, 'Installed games/Game A/Profile A')
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


def test_individual_save_only_and_unknown_type_is_exportable(tmp_path):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A'
    backend.set_save_identity('SAVES', path, title_id=None, user_id=None,
                              environment_id=None, save_type=None, verified=False)
    with pytest.raises(BackupError, match='individual save'):
        service.create_snapshot(backend, 'Installed games/Game A')
    with pytest.raises(BackupError, match='individual save'):
        service.create_snapshots(backend, ['Installed games'])
    snapshot = service.create_snapshots(backend, [path])[0]
    assert snapshot['identity']['save_type'] is None
    assert snapshot['state'] == 'ready'
    with pytest.raises(BackupError, match='identity'):
        service.prepare_restore(backend, snapshot['id'])


def test_inventory_all_includes_non_account_individual_saves_only(tmp_path):
    backend = populated_backend()
    path = 'Installed games/Game A/Profile A'
    backend.storage_tree('SAVES').ensure_directory(path + '/nested')
    backend.storage_tree('SAVES').write_file(path + '/nested/progress.dat', b'progress')
    backend.set_save_identity('SAVES', path, title_id=None, user_id=None,
                              environment_id=None, save_type='Device', verified=False)
    service = BackupManager(tmp_path / 'store')
    rows = service.inventory_saves(backend)
    assert [row['path'] for row in rows] == [
        'Installed games/Game A/Profile A', 'Installed games/Game B/Profile B']
    assert rows[0]['selectable'] is True
    snapshot = service.create_snapshot(backend, path)
    assert snapshot['identity']['save_type'] == 'Device'
    assert snapshot['files'][-1]['path'] == 'nested/progress.dat'
    with pytest.raises(BackupError, match='identity'):
        service.prepare_restore(backend, snapshot['id'])


def test_arbitrary_root_group_is_not_an_individual_save(tmp_path):
    backend = populated_backend()
    path = 'Unexpected group/Game X/Profile X'
    backend.storage_tree('SAVES').ensure_directory(path)
    backend.storage_tree('SAVES').write_file(path + '/progress.dat', b'x')
    service = BackupManager(tmp_path / 'store')
    assert path not in {row['path'] for row in service.inventory_saves(backend)}
    with pytest.raises(BackupError, match='individual save'):
        service.create_snapshot(backend, path)
    assert service.list_snapshots() == []


def test_unknown_device_size_respects_actual_snapshot_and_game_limits(tmp_path, monkeypatch):
    backend = populated_backend()
    original_stat = backend.stat
    original_list = backend.list_directory
    monkeypatch.setattr(backend, 'stat', lambda *args, **kwargs:
                        replace(original_stat(*args, **kwargs), size=None))
    monkeypatch.setattr(backend, 'list_directory', lambda *args, **kwargs:
                        [replace(entry, size=None) for entry in original_list(*args, **kwargs)])
    service = BackupManager(tmp_path / 'store', limits=BackupLimits(max_file_bytes=8,
                                                                    max_total_bytes=6))
    with pytest.raises(BackupError, match='actual source size limit'):
        service.create_snapshot(backend, 'Installed games/Game A/Profile A')
    assert service.list_snapshots() == []
    with pytest.raises(BackupError, match='actual game export size limit'):
        service.export_games(backend, ['Game A.nsp'])
    assert service.list_game_exports() == []


def test_snapshot_identity_change_during_read_keeps_incomplete(tmp_path, monkeypatch):
    backend = populated_backend()
    path = 'Installed games/Game A/Profile A'
    original_receive = backend.receive_file

    def change_uid(*args, **kwargs):
        received = original_receive(*args, **kwargs)
        backend.set_save_identity('SAVES', path, title_id='0100000000000001',
                                  user_id='uid-other', environment_id='installation-a')
        return received

    monkeypatch.setattr(backend, 'receive_file', change_uid)
    service = BackupManager(tmp_path / 'store')
    with pytest.raises(BackupError, match='identity changed'):
        service.create_snapshot(backend, path)
    assert service.list_snapshots() == []
    assert len(service.list_incomplete()) == 1


def test_import_rejects_incomplete_snapshot_manifest(tmp_path):
    source = BackupManager(tmp_path / 'source')
    backend = populated_backend()
    snapshot = source.create_snapshot(backend, 'Installed games/Game A/Profile A')
    archive = source.create_archive([snapshot['id']], tmp_path / 'ready.zip')
    incomplete = tmp_path / 'incomplete.zip'
    with zipfile.ZipFile(archive) as reader, zipfile.ZipFile(incomplete, 'w') as writer:
        for name in reader.namelist():
            data = reader.read(name)
            if name == 'manifest.json':
                manifest = json.loads(data)
                manifest['snapshots'][0]['state'] = 'incomplete'
                data = json.dumps(manifest).encode()
            writer.writestr(name, data)
    imported = BackupManager(tmp_path / 'imported')
    with pytest.raises(BackupError, match='non-ready'):
        imported.import_archive(incomplete)
    assert imported.list_snapshots() == []


def test_group_import_rolls_back_first_published_snapshot(tmp_path, monkeypatch):
    from switchagent import backup_manager as module
    source = BackupManager(tmp_path / 'source')
    backend = populated_backend()
    paths = ['Installed games/Game A/Profile A', 'Installed games/Game B/Profile B']
    snapshots = source.create_snapshots(backend, paths)
    archive = source.create_archive([item['id'] for item in snapshots], tmp_path / 'group.zip')
    imported = BackupManager(tmp_path / 'imported')
    original_replace = module.os.replace
    calls = 0

    def fail_second_publish(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('second publish failed')
        return original_replace(src, dst)

    monkeypatch.setattr(module.os, 'replace', fail_second_publish)
    with pytest.raises(OSError, match='second publish failed'):
        imported.import_archive(archive)
    assert imported.list_snapshots() == []
    assert not list((imported.root / 'snapshots').iterdir())


def test_restore_result_survives_journal_failure_after_write(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A'
    source = service.create_snapshot(backend, path)
    plan = service.prepare_restore(backend, source['id'])
    original_record = service._record

    def fail_completed(kind, state, **details):
        if kind == 'restore' and state == 'completed':
            raise OSError('journal full')
        return original_record(kind, state, **details)

    monkeypatch.setattr(service, '_record', fail_completed)
    outcome = service.confirm_restore(backend, plan['id'])
    assert outcome['state'] == 'completed'
    assert outcome['journal_warning'] == 'event journal unavailable'
    assert plan['prebackup_id'] in {item['id'] for item in service.list_snapshots()}


def test_final_source_recheck_after_last_target_read(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A'
    source = service.create_snapshot(backend, path)
    plan = service.prepare_restore(backend, source['id'])
    source_file = service.root / 'snapshots' / source['id'] / 'files' / 'main.dat'
    original_receive = backend.receive_file
    original_identity = backend.save_identity
    target_was_read = False
    identity_rechecked = False
    mutated = False

    def track_target_read(*args, **kwargs):
        nonlocal target_was_read, mutated
        received = original_receive(*args, **kwargs)
        if identity_rechecked and not mutated:
            source_file.write_bytes(b'x' * 17)
            mutated = True
        else:
            target_was_read = True
        return received

    def track_identity(*args, **kwargs):
        nonlocal identity_rechecked
        identity = original_identity(*args, **kwargs)
        if target_was_read:
            identity_rechecked = True
        return identity

    monkeypatch.setattr(backend, 'receive_file', track_target_read)
    monkeypatch.setattr(backend, 'save_identity', track_identity)
    with pytest.raises(BackupError, match='source changed'):
        service.confirm_restore(backend, plan['id'])
    assert mutated
    assert not any(item.operation == 'REPLACE_SAVE_TREE' for item in backend.operation_log)


@pytest.mark.parametrize('zone', ['snapshots', 'import', 'game-exports'])
def test_publication_rejects_reparse_destination_parent(tmp_path, monkeypatch, zone):
    from switchagent import backup_manager as module
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    original_predicate = module.is_link_or_reparse
    if zone == 'snapshots':
        parent = service.root / 'snapshots'
        parent.mkdir()
        action = lambda: service.create_snapshot(backend, 'Installed games/Game A/Profile A')
    elif zone == 'import':
        source = BackupManager(tmp_path / 'source')
        snapshot = source.create_snapshot(backend, 'Installed games/Game A/Profile A')
        archive = source.create_archive([snapshot['id']], tmp_path / 'group.zip')
        parent = service.root / 'snapshots'
        parent.mkdir()
        action = lambda: service.import_archive(archive)
    else:
        parent = service.root / 'game-exports'
        parent.mkdir()
        action = lambda: service.export_games(backend, ['Game A.nsp'])
    monkeypatch.setattr(module, 'is_link_or_reparse',
                        lambda path: Path(path) == parent or original_predicate(path))
    with pytest.raises(BackupError, match='reparse'):
        action()
    assert list(parent.iterdir()) == []


def test_unknown_restore_outcome_survives_journal_failure(tmp_path, monkeypatch):
    backend = populated_backend()
    service = BackupManager(tmp_path / 'store')
    path = 'Installed games/Game A/Profile A'
    source = service.create_snapshot(backend, path)
    plan = service.prepare_restore(backend, source['id'])
    backend.arm_failure('disconnect', storage='SAVES', dest_path=path)
    original_record = service._record

    def fail_unknown(kind, state, **details):
        if kind == 'restore' and state == 'unknown':
            raise OSError('journal full')
        return original_record(kind, state, **details)

    monkeypatch.setattr(service, '_record', fail_unknown)
    with pytest.raises(BackupError, match='target state is unknown'):
        service.confirm_restore(backend, plan['id'])
    assert [item.operation for item in backend.operation_log].count('REPLACE_SAVE_TREE') == 1
    assert plan['prebackup_id'] in {item['id'] for item in service.list_snapshots()}
    assert any('restore unknown:' in warning for warning in service.journal_warnings)
