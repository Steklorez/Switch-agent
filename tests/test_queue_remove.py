"""Queue page: "Remove from queue" on a game's card, human status words, and
Pause being visible where the queue is shown."""

from __future__ import annotations

from switchagent import db
from switchagent.web import services
from switchagent.web.preparation import REMOVED_PHASE

from tests.test_sequential_preparation import setup_sequence
from tests.test_web_api import _seed_four_items, client, web_ctx  # noqa: F401 -- fixtures


def _run(queue, task_id, items, chains):
    queue._install_sequentially(None, items, 'mock', lambda **kw: None, task_id, chains)


def _state(items):
    return {'target': 'mock', 'phase': 'Preparing', 'started': 0.0,
            'items': {str(i): {'phase': 'Waiting', 'job_ids': []} for i in items}}


# -- preparation ----------------------------------------------------------------

def test_a_removed_item_is_skipped_when_its_turn_comes(tmp_path, monkeypatch):
    queue, events = setup_sequence(tmp_path, monkeypatch)
    queue.states['t'] = _state([1, 2, 3])

    assert queue.remove_items([2]) == [2]
    _run(queue, 't', [1, 2, 3], {'a': [1], 'b': [2], 'c': [3]})

    assert ('prepare', 2) not in events and ('install', 2) not in events
    assert ('install', 1) in events and ('install', 3) in events


def test_an_item_removed_while_being_prepared_is_released_not_confirmed(tmp_path, monkeypatch):
    queue, events = setup_sequence(tmp_path, monkeypatch)
    queue.states['t'] = _state([1, 2])
    confirmed, released = [], []
    monkeypatch.setattr(db, 'confirm_job', lambda conn, job_id: confirmed.append(job_id))
    original = services.create_and_confirm_jobs

    def prepare(conn, ids, target, progress=None, confirm=True):
        part = original(conn, ids, target, progress=progress, confirm=confirm)
        if ids[0] == 1:
            queue.remove_items([1])  # pressed while its archive was unpacking
        return part

    monkeypatch.setattr(services, 'create_and_confirm_jobs', prepare)

    class Conn:
        def execute(self, sql, params):
            released.append(params)

        def commit(self):
            pass

    queue._install_sequentially(Conn(), [1, 2], 'mock', lambda **kw: None, 't', {'a': [1], 'b': [2]})

    assert confirmed == [2]
    assert ('install', 1) not in events and ('install', 2) in events
    assert released and released[0][0] == 'Removed from the queue'


def test_a_removed_item_stays_removed_and_leaves_the_display(tmp_path):
    from switchagent.web.preparation import PreparationQueue

    queue = PreparationQueue(tmp_path / 'x.db')
    queue.states['t'] = _state([1, 2])
    queue.remove_items([1])
    assert queue.states['t']['items']['1']['phase'] == REMOVED_PHASE
    assert [i['library_item_id'] for i in queue.dock_items()] == [2]


def test_items_of_a_finished_run_or_already_sending_are_not_removed(tmp_path):
    from switchagent.web.preparation import PreparationQueue

    queue = PreparationQueue(tmp_path / 'x.db')
    queue.states['done'] = dict(_state([1]), phase='Ready')
    queue.states['live'] = _state([2, 3])
    queue.states['live']['items']['3']['phase'] = 'Cleaning'
    assert queue.remove_items([1, 2, 3]) == [2]


# -- the endpoint ---------------------------------------------------------------

def test_remove_cancels_every_part_of_the_game_that_has_not_started(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    created = client.post("/api/jobs", json={
        "library_item_ids": item_ids, "target_device_id": "mock-switch-parent",
    }).json()["created"]
    bread = [c["job_id"] for c in created if c["library_item_id"] in item_ids[:2]]

    res = client.post("/api/queue/remove", json={"job_ids": bread, "library_item_ids": []})
    assert res.status_code == 200
    assert sorted(res.json()["cancelled_job_ids"]) == sorted(bread)
    names = [g["name"] for g in client.get("/api/queue/dock").json()["games"]]
    assert names == ["Quake II", "Sacred 2"]


def test_remove_is_refused_while_a_part_is_being_sent(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    created = client.post("/api/jobs", json={
        "library_item_ids": item_ids[:2], "target_device_id": "mock-switch-parent",
    }).json()["created"]
    job_ids = [c["job_id"] for c in created]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_ids[0], "RUNNING")

    res = client.post("/api/queue/remove", json={"job_ids": job_ids, "library_item_ids": []})
    assert res.status_code == 409
    assert "Abort" in res.json()["detail"]
    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_job(conn, job_ids[1])["status"] == "CONFIRMED"  # nothing changed


def test_remove_drops_preparation_items(client, web_ctx):
    web_ctx.preparations.states['t'] = _state([7])
    res = client.post("/api/queue/remove", json={"job_ids": [], "library_item_ids": [7]})
    assert res.json()["removed_item_ids"] == [7]


# -- words and pause -------------------------------------------------------------

def test_queue_rows_say_words_not_status_enums(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": item_ids[:1], "target_device_id": "mock-switch-parent"})

    [job] = [j for g in client.get("/api/queue/grouped").json() for j in g["jobs"]]
    assert job["status_label"] == "Up next"
    html = client.get("/queue").text
    assert ">Up next<" in html and ">CONFIRMED<" not in html
    assert 'data-job-action="cancel"' not in html  # the card's Remove replaced it


def test_paused_shows_on_the_page_the_rows_and_the_dock(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": item_ids[:1], "target_device_id": "mock-switch-parent"})
    client.post("/api/worker/pause")

    html = client.get("/queue").text
    assert 'id="paused-banner"' in html and 'role="status" hidden' not in html
    assert "status-paused\">Paused<" in html
    dock = client.get("/api/queue/dock").json()
    assert dock["paused"] is True and dock["games"][0]["status_label"] == "Paused"

    client.post("/api/worker/resume")
    assert 'role="status" hidden' in client.get("/queue").text
