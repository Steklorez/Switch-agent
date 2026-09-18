import pytest

from switchagent import config, db, manifest, work_cleanup
from switchagent.web import services
from switchagent.web.preparation import PreparationQueue, _group_by_title


def setup_sequence(tmp_path, monkeypatch, *, fail=False):
    monkeypatch.setattr(config, 'LOGS_DIR', tmp_path / 'logs')
    events = []
    # `confirm` mirrors the real create_and_confirm_jobs signature: the
    # caller now decides when a prepared job becomes visible to the worker
    # (see preparation._install_sequentially on preparing one item ahead).
    def prepare(conn, ids, target, progress=None, confirm=True):
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
    # Two real DB calls the loop now makes directly: confirming a prepared
    # job when its turn actually comes, and looking an item up to decide
    # whether it may be prepared ahead at all. This fixture has no database
    # (conn is None), so both are stubbed -- and returning None from the
    # lookup also keeps these tests strictly one-at-a-time, which is exactly
    # the sequencing they assert about.
    monkeypatch.setattr(db, 'confirm_job', lambda conn, job_id: None)
    monkeypatch.setattr(db, 'get_library_item_by_id', lambda conn, item_id: None)
    monkeypatch.setattr(work_cleanup, 'cleanup_batch_if_all_done', cleanup)
    monkeypatch.setattr(manifest, 'batch_work_dir', lambda batch: tmp_path / str(batch))
    return PreparationQueue(tmp_path / 'test.db'), events


def test_each_payload_is_cleaned_before_next_and_logs_are_bounded(tmp_path, monkeypatch):
    queue, events = setup_sequence(tmp_path, monkeypatch)
    for n in range(12):
        # _install_sequentially() now requires its own task_id to still be
        # present in self.states (see its "device connection changed"
        # check) -- normally guaranteed by submit(), reproduced by hand
        # here since these tests call it directly.
        queue.states[str(n)] = {'target': 'mock', 'items': {}}
        queue._install_sequentially(None, [1, 2], 'mock', lambda **kw: None, str(n), {'chain-a': [1, 2]})
    assert events[:6] == [('prepare', 1), ('install', 1), ('cleanup', 1),
                         ('prepare', 2), ('install', 2), ('cleanup', 2)]
    logs = list((config.LOGS_DIR / 'installs').glob('*.log'))
    assert len(logs) == 10
    assert all('Install finished' in p.read_text() for p in logs)


def test_blocked_item_defers_the_rest_of_its_own_chain_but_not_others(tmp_path, monkeypatch):
    """A stuck job (DESTINATION_CONFLICT and friends) no longer kills the
    whole run -- by explicit request, it only defers the REST OF ITS OWN
    CHAIN (e.g. an Update must never install ahead of its still-
    unresolved Base) while a DIFFERENT, independent chain in the same
    batch keeps going. Item 2 here shares item 1's chain, so it must
    never even be attempted; item 3 is its own independent chain and
    must be attempted regardless."""
    queue, events = setup_sequence(tmp_path, monkeypatch, fail=True)
    queue.states['failure'] = {'target': 'mock', 'items': {}}
    result = queue._install_sequentially(
        None, [1, 2, 3], 'mock', lambda **kw: None, 'failure', {'chain-a': [1, 2], 'chain-b': [3]},
    )
    assert events == [('prepare', 1), ('install', 1), ('prepare', 3), ('install', 3)]
    assert result['blocked'] == [1, 3]
    assert (tmp_path / '1').exists()  # payload retained -- never cleaned while unresolved


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


def _make_job(conn, *, status, finished_at=None, abandoned=False):
    import uuid
    item_id = db.upsert_library_item(
        conn, absolute_path=f'/lib/item-{uuid.uuid4().hex}.nsp', item_type='FILE', file_type='NSP',
        size=1, mtime=0.0, content_hash=None, title_id=None, title_id_source=None,
        status='AVAILABLE', suggested_action='INSTALL_VIA_DBI', suggested_target='SD_INSTALL',
    )
    job_id = db.create_job(
        conn, action='INSTALL_VIA_DBI', target_storage='SD_INSTALL',
        target_device_id='mock-switch', library_item_id=item_id,
    )
    db.update_job_status(conn, job_id, status, finished_at=finished_at)
    if abandoned:
        conn.execute('UPDATE jobs SET abandoned=1 WHERE id=?', (job_id,))
        conn.commit()
    return job_id


def test_snapshot_hides_a_resolved_item_once_its_own_grace_period_elapses(tmp_path):
    """By explicit request: once ONE item's own Override/Skip has resolved
    it, its row shouldn't linger forever in a "Failed" batch's panel --
    same 10s grace period submit()'s own whole-batch auto-clear already
    gives a fully successful batch (test above), applied per-item here so
    a sibling item that's still genuinely stuck "Not started" stays
    visible for as long as it needs to."""
    import datetime
    queue = PreparationQueue(tmp_path / 'test.db')
    with db.open_db(queue.db_path) as conn:
        long_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)).isoformat()
        resolved_job = _make_job(conn, status='DONE_UNVERIFIED', finished_at=long_ago)
        stuck_job = _make_job(conn, status='DESTINATION_CONFLICT')

    queue.states['batch-1'] = {
        'id': 'batch-1', 'phase': 'Failed', 'started': 0, 'error': 'Installation stopped', 'items': {
            'resolved-item': {'name': 'resolved', 'phase': 'Failed', 'order': 0, 'job_ids': [resolved_job]},
            'stuck-item': {'name': 'stuck', 'phase': 'Installing', 'order': 1, 'job_ids': [stuck_job]},
        },
    }

    snapshot = queue.snapshot()
    items = snapshot[0]['items']
    assert 'resolved-item' not in items  # long-resolved -- swept from view
    assert 'stuck-item' in items  # still genuinely unresolved -- stays


def test_snapshot_still_shows_a_just_resolved_item_within_the_grace_period(tmp_path):
    queue = PreparationQueue(tmp_path / 'test.db')
    with db.open_db(queue.db_path) as conn:
        just_now = db.now_iso()
        resolved_job = _make_job(conn, status='DONE_UNVERIFIED', finished_at=just_now)

    queue.states['batch-1'] = {
        'id': 'batch-1', 'phase': 'Failed', 'started': 0, 'error': 'Installation stopped', 'items': {
            'resolved-item': {'name': 'resolved', 'phase': 'Failed', 'order': 0, 'job_ids': [resolved_job]},
        },
    }

    snapshot = queue.snapshot()
    assert 'resolved-item' in snapshot[0]['items']  # not yet past the 10s grace period


def test_snapshot_never_hides_an_item_that_never_got_a_job(tmp_path):
    """An item still at "Waiting" (job_ids empty -- the sequential run
    stopped before ever reaching it) must never be swept up by the
    resolved-item grace period: all() on an empty job list is vacuously
    True, which -- without the explicit `rows and ...` guard -- would
    have silently hidden an item that still genuinely needs the user to
    manually re-select and install it from Library."""
    queue = PreparationQueue(tmp_path / 'test.db')
    with db.open_db(queue.db_path):
        pass

    queue.states['batch-1'] = {
        'id': 'batch-1', 'phase': 'Failed', 'started': 0, 'error': 'Installation stopped', 'items': {
            'never-started': {'name': 'never', 'phase': 'Waiting', 'order': 0, 'job_ids': []},
        },
    }

    snapshot = queue.snapshot()
    assert 'never-started' in snapshot[0]['items']


@pytest.mark.parametrize("status", [
    "DESTINATION_CONFLICT", "SOURCE_CHANGED", "DEVICE_UNAVAILABLE", "BLOCKED_BY_DEPENDENCY",
])
def test_stuck_job_status_stops_the_batch_instead_of_polling_forever(tmp_path, monkeypatch, status):
    """Reproduces a real hang: a real DESTINATION_CONFLICT mid-batch left
    the sequential-install poll loop spinning every .5s forever, since that
    status (and these other "needs a user action" ones) wasn't in the set
    that makes this loop give up. Each of these must be detected
    on the FIRST poll -- proven here by only ever mocking a single
    list_jobs_by_batch call's worth of state, no retry/backoff involved.
    Detecting it defers the rest of THIS chain (item 2, same chain as the
    stuck item 1) rather than raising -- see
    test_blocked_item_defers_the_rest_of_its_own_chain_but_not_others for
    the "a different, independent chain keeps going" half."""
    monkeypatch.setattr(config, 'LOGS_DIR', tmp_path / 'logs')
    events = []

    # `confirm` mirrors the real create_and_confirm_jobs signature: the
    # caller now decides when a prepared job becomes visible to the worker
    # (see preparation._install_sequentially on preparing one item ahead).
    def prepare(conn, ids, target, progress=None, confirm=True):
        item = ids[0]
        events.append(('prepare', item))
        (tmp_path / str(item)).mkdir()
        return {'created': [{'job_id': item}], 'errors': [], 'batch_id': item}

    def jobs(conn, batch):
        events.append(('install', batch))
        return [{'id': batch, 'status': status, 'error': 'stuck'}]

    monkeypatch.setattr(services, 'create_and_confirm_jobs', prepare)
    monkeypatch.setattr(db, 'list_jobs_by_batch', jobs)
    # Two real DB calls the loop now makes directly: confirming a prepared
    # job when its turn actually comes, and looking an item up to decide
    # whether it may be prepared ahead at all. This fixture has no database
    # (conn is None), so both are stubbed -- and returning None from the
    # lookup also keeps these tests strictly one-at-a-time, which is exactly
    # the sequencing they assert about.
    monkeypatch.setattr(db, 'confirm_job', lambda conn, job_id: None)
    monkeypatch.setattr(db, 'get_library_item_by_id', lambda conn, item_id: None)
    monkeypatch.setattr(manifest, 'batch_work_dir', lambda batch: tmp_path / str(batch))

    queue = PreparationQueue(tmp_path / 'test.db')
    queue.states[status] = {'target': 'mock', 'items': {}}
    result = queue._install_sequentially(None, [1, 2], 'mock', lambda **kw: None, status, {'chain-a': [1, 2]})
    assert result['blocked'] == [1]
    # Exactly one poll of the stuck status -- never looped waiting for it
    # to change on its own -- and item 2 (same chain) was never attempted.
    assert events == [('prepare', 1), ('install', 1)]


# ---------------------------------------------------------------------------
# continue_chain: resuming a game's Update/Mod after its Base is resolved.
# By explicit request: an Update/DLC/Mod must never install ahead of its
# own still-unresolved Base -- _install_sequentially() defers them (tests
# above), and continue_chain() is what picks them back up once the user
# resolves the Base via the ordinary Override/Retry/Skip actions (a
# completely separate HTTP request -- see app.py's routes). It does NOT
# poll/wait for that to happen: submit()'s own run() holds self.serial for
# its whole call, so waiting here would block every OTHER preparation for
# as long as the user takes to click something.
# ---------------------------------------------------------------------------

def test_continue_chain_resumes_the_rest_of_the_same_game_after_resolution(tmp_path):
    queue = PreparationQueue(tmp_path / 'test.db')
    queue.states['batch-1'] = {
        'id': 'batch-1', 'phase': 'Failed', 'started': 0, 'target': 'mock-switch',
        'chains': {'family-a': [10, 20, 30], 'family-b': [40]},
        'items': {
            '10': {'name': 'base', 'phase': 'Failed', 'order': 0, 'job_ids': [999]},
            '20': {'name': 'update', 'phase': 'Waiting', 'order': 1, 'job_ids': []},
            '30': {'name': 'mod', 'phase': 'Waiting', 'order': 2, 'job_ids': []},
            '40': {'name': 'other game', 'phase': 'Ready', 'order': 3, 'job_ids': [998]},
        },
    }
    submitted = []
    queue.submit = lambda item_ids, target: submitted.append((item_ids, target))

    queue.continue_chain(999)

    # Only the SAME game's remaining items (update, mod), in order --
    # the independent "other game" chain is never touched.
    assert submitted == [([20, 30], 'mock-switch')]


def test_continue_chain_does_nothing_for_a_job_outside_any_known_chain(tmp_path):
    queue = PreparationQueue(tmp_path / 'test.db')
    submitted = []
    queue.submit = lambda item_ids, target: submitted.append((item_ids, target))

    queue.continue_chain(123456)  # not tracked by any preparation state

    assert submitted == []


def test_continue_chain_does_nothing_when_nothing_is_left_after_the_resolved_item(tmp_path):
    queue = PreparationQueue(tmp_path / 'test.db')
    queue.states['batch-1'] = {
        'id': 'batch-1', 'phase': 'Failed', 'started': 0, 'target': 'mock-switch',
        'chains': {'family-a': [10]},
        'items': {'10': {'name': 'base', 'phase': 'Failed', 'order': 0, 'job_ids': [999]}},
    }
    submitted = []
    queue.submit = lambda item_ids, target: submitted.append((item_ids, target))

    queue.continue_chain(999)

    assert submitted == []


# ---------------------------------------------------------------------------
# _group_by_title: Base must always come before Update/DLC before Mod,
# regardless of what order the items were actually submitted in -- the
# Library page's selection order is click order, not install order (see
# library.js's Array.from(selected.keys())), so this can never be assumed
# to already be correct.
# ---------------------------------------------------------------------------

def _seed_variant(conn, *, title_id, content_type='GAME_PACKAGE'):
    import uuid
    kwargs = dict(
        conn=conn, absolute_path=f'/lib/{uuid.uuid4().hex}.nsp', item_type='FILE', file_type='NSP',
        size=1, mtime=0.0, content_hash=None, title_id=title_id, title_id_source='filename',
        status='AVAILABLE', suggested_action='INSTALL_VIA_DBI', suggested_target='SD_INSTALL',
        content_type=content_type,
    )
    if content_type == 'ATMOSPHERE_MOD':
        kwargs.update(item_type='MOD_FOLDER', file_type='ATMOSPHERE_MOD',
                      suggested_action='COPY_MERGE', suggested_target='SD_CARD')
    return db.upsert_library_item(**kwargs)


def test_group_by_title_reorders_base_first_regardless_of_submission_order(tmp_path):
    queue = PreparationQueue(tmp_path / 'test.db')
    with db.open_db(queue.db_path) as conn:
        base_id = _seed_variant(conn, title_id='0100000000010000')
        update_id = _seed_variant(conn, title_id='0100000000010800')
        mod_id = _seed_variant(conn, title_id='0100000000010000', content_type='ATMOSPHERE_MOD')
        other_base_id = _seed_variant(conn, title_id='0100000000020000')

        # Deliberately submitted in the WRONG order (mod, update, base) --
        # exactly what a user clicking cards in visual/random order would
        # produce -- interleaved with an unrelated, independent game.
        chains = _group_by_title(conn, [mod_id, other_base_id, update_id, base_id])

    assert chains['0100000000010000'] == [base_id, update_id, mod_id]
    assert chains['0100000000020000'] == [other_base_id]


# ---------------------------------------------------------------------------
# Preparing one item ahead (2026-09-18). Extraction/staging/hashing is
# disk+CPU work with nothing to do with the USB cable, so it now runs while
# the previous item is still transferring -- except for archives, which stay
# strictly sequential (only one archive may occupy work/ at a time).
# ---------------------------------------------------------------------------


def _lookahead_sequence(tmp_path, monkeypatch, *, absolute_path):
    """Two items in one chain. Item 1's install only reports DONE once item 2
    has been prepared, so the run can only finish at all if preparation really
    did overlap the transfer -- and `late` records the case where it didn't."""
    import threading
    monkeypatch.setattr(config, 'LOGS_DIR', tmp_path / 'logs')
    events = []
    prepared_second = threading.Event()
    late = []

    def prepare(conn, ids, target, progress=None, confirm=True):
        item = ids[0]
        events.append(('prepare', item))
        (tmp_path / str(item)).mkdir(exist_ok=True)
        if item == 2:
            prepared_second.set()
        return {'created': [{'job_id': item}], 'errors': [], 'batch_id': item}

    def jobs(conn, batch):
        events.append(('install', batch))
        if batch == 1 and not prepared_second.is_set():
            if not prepared_second.wait(2):
                late.append(batch)  # nothing prepared item 2 while item 1 was busy
        return [{'id': batch, 'status': 'DONE_UNVERIFIED', 'error': None}]

    def cleanup(conn, batch):
        events.append(('cleanup', batch))
        path = tmp_path / str(batch)
        if path.exists():
            path.rmdir()

    monkeypatch.setattr(services, 'create_and_confirm_jobs', prepare)
    monkeypatch.setattr(db, 'list_jobs_by_batch', jobs)
    monkeypatch.setattr(db, 'confirm_job', lambda conn, job_id: None)
    monkeypatch.setattr(db, 'get_library_item_by_id',
                        lambda conn, item_id: {'absolute_path': absolute_path})
    monkeypatch.setattr(work_cleanup, 'cleanup_batch_if_all_done', cleanup)
    monkeypatch.setattr(manifest, 'batch_work_dir', lambda batch: tmp_path / str(batch))
    queue = PreparationQueue(tmp_path / 'test.db')
    queue.states['task'] = {'target': 'mock', 'items': {}}
    queue._install_sequentially(None, [1, 2], 'mock', lambda **kw: None, 'task', {'chain-a': [1, 2]})
    return events, late


def test_a_bare_package_is_prepared_while_the_previous_item_still_transfers(tmp_path, monkeypatch):
    events, late = _lookahead_sequence(
        tmp_path, monkeypatch, absolute_path=r"D:\lib\Game [0100000000010000][v0].nsp",
    )
    assert late == [], "item 2 was not prepared while item 1 was still installing"
    assert events.index(('prepare', 2)) < events.index(('cleanup', 1))
    # Order within the chain is unchanged: item 2 is still installed after
    # item 1 finished, no matter how early its bytes were ready.
    assert events.index(('install', 1)) < events.index(('install', 2))


def test_an_archive_is_never_extracted_ahead_of_the_previous_one_being_cleaned(tmp_path, monkeypatch):
    """The separately-tested guarantee this must not break: only one
    archive's payload may occupy work/ at a time (see
    test_each_payload_is_cleaned_before_next_and_logs_are_bounded and
    test_hotfix_reconciliation.py)."""
    events, late = _lookahead_sequence(tmp_path, monkeypatch, absolute_path=r"D:\lib\pack.zip")
    assert late == [1], "an archive must not be prepared ahead"
    assert events == [('prepare', 1), ('install', 1), ('cleanup', 1),
                      ('prepare', 2), ('install', 2), ('cleanup', 2)]
