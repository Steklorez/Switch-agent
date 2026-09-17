import pytest

from switchagent import config, db, manifest, work_cleanup
from switchagent.web import services
from switchagent.web.preparation import PreparationQueue


def setup_sequence(tmp_path, monkeypatch, *, fail=False):
    monkeypatch.setattr(config, 'LOGS_DIR', tmp_path / 'logs')
    events = []
    def prepare(conn, ids, target, progress=None):
        item = ids[0]
        events.append(('prepare', item))
        (tmp_path / str(item)).mkdir()
        return {'created': [{'job_id': item}], 'errors': [], 'batch_id': item}
    def jobs(conn, batch):
        events.append(('install', batch))
        return [{'id': batch, 'status': 'FAILED' if fail else 'DONE_UNVERIFIED', 'error': None}]
    def cleanup(conn, batch):
        events.append(('cleanup', batch))
        (tmp_path / str(batch)).rmdir()
    monkeypatch.setattr(services, 'create_and_confirm_jobs', prepare)
    monkeypatch.setattr(db, 'list_jobs_by_batch', jobs)
    monkeypatch.setattr(work_cleanup, 'cleanup_batch_if_all_done', cleanup)
    monkeypatch.setattr(manifest, 'batch_work_dir', lambda batch: tmp_path / str(batch))
    return PreparationQueue(tmp_path / 'test.db'), events


def test_each_payload_is_cleaned_before_next_and_logs_are_bounded(tmp_path, monkeypatch):
    queue, events = setup_sequence(tmp_path, monkeypatch)
    for n in range(12):
        queue._install_sequentially(None, [1, 2], 'mock', lambda **kw: None, str(n))
    assert events[:6] == [('prepare', 1), ('install', 1), ('cleanup', 1),
                         ('prepare', 2), ('install', 2), ('cleanup', 2)]
    logs = list((config.LOGS_DIR / 'installs').glob('*.log'))
    assert len(logs) == 10
    assert all('Install finished' in p.read_text() for p in logs)


def test_failure_retains_payload_and_does_not_extract_next(tmp_path, monkeypatch):
    queue, events = setup_sequence(tmp_path, monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match='Installation stopped'):
        queue._install_sequentially(None, [1, 2], 'mock', lambda **kw: None, 'failure')
    assert events == [('prepare', 1), ('install', 1)]
    assert (tmp_path / '1').exists()


def test_submit_prunes_previously_finished_states_immediately(tmp_path):
    """Before this fix, a completed ("Ready") or "Failed" preparation only
    got pruned from the panel once 50+ states had piled up -- in normal
    use ("install a batch, wait, install another") that never happened,
    so a fully finished batch just sat in the Queue panel forever, right
    alongside the next one the user started, looking like it never
    actually cleared. submit() now prunes every existing settled
    (Ready/Failed) state unconditionally before adding the new one --
    still-running work is left completely alone."""
    queue = PreparationQueue(tmp_path / 'test.db')
    with db.open_db(queue.db_path):
        pass  # just ensure the schema exists
    queue.states['old-ready'] = {'id': 'old-ready', 'phase': 'Ready', 'started': 0, 'items': {}}
    queue.states['old-failed'] = {'id': 'old-failed', 'phase': 'Failed', 'started': 0, 'items': {}}
    queue.states['still-running'] = {'id': 'still-running', 'phase': 'Preparing', 'started': 0, 'items': {}}

    queue.submit([999999], 'mock-switch')

    assert 'old-ready' not in queue.states
    assert 'old-failed' not in queue.states
    assert 'still-running' in queue.states  # never touched -- not settled


@pytest.mark.parametrize("status", [
    "DESTINATION_CONFLICT", "SOURCE_CHANGED", "DEVICE_UNAVAILABLE", "BLOCKED_BY_DEPENDENCY",
])
def test_stuck_job_status_stops_the_batch_instead_of_polling_forever(tmp_path, monkeypatch, status):
    """Reproduces a real hang: a real DESTINATION_CONFLICT mid-batch left
    the sequential-install poll loop spinning every .5s forever, since that
    status (and these other "needs a user action" ones) wasn't in the set
    that makes this loop give up and raise. Each of these must be detected
    on the FIRST poll -- proven here by only ever mocking a single
    list_jobs_by_batch call's worth of state, no retry/backoff involved."""
    monkeypatch.setattr(config, 'LOGS_DIR', tmp_path / 'logs')
    events = []

    def prepare(conn, ids, target, progress=None):
        item = ids[0]
        events.append(('prepare', item))
        (tmp_path / str(item)).mkdir()
        return {'created': [{'job_id': item}], 'errors': [], 'batch_id': item}

    def jobs(conn, batch):
        events.append(('install', batch))
        return [{'id': batch, 'status': status, 'error': 'stuck'}]

    monkeypatch.setattr(services, 'create_and_confirm_jobs', prepare)
    monkeypatch.setattr(db, 'list_jobs_by_batch', jobs)
    monkeypatch.setattr(manifest, 'batch_work_dir', lambda batch: tmp_path / str(batch))

    queue = PreparationQueue(tmp_path / 'test.db')
    with pytest.raises(RuntimeError, match='Installation stopped'):
        queue._install_sequentially(None, [1, 2], 'mock', lambda **kw: None, status)
    # Exactly one poll of the stuck status -- never looped waiting for it
    # to change on its own.
    assert events == [('prepare', 1), ('install', 1)]
