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
