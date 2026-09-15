"""Regression for the packaged first-start worker/HTTP migration race."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from switchagent import db


def test_concurrent_first_start_does_not_add_a_column_twice(tmp_path):
    first_alter = threading.Event()
    second_started = threading.Event()
    second_alter = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    attempts = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("ALTER TABLE jobs ADD COLUMN last_progress_at"):
                with counter_lock:
                    attempts.append(sql)
                    first = len(attempts) == 1
                if first:
                    first_alter.set()
                    assert release.wait(5), "test did not release first migration"
                else:
                    second_alter.set()
            return super().execute(sql, *args, **kwargs)

    path = tmp_path / "fresh.db"

    def initialize(second=False):
        conn = sqlite3.connect(path, factory=Connection, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            if second:
                second_started.set()
            db.init_db(conn)
            return {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(initialize)
        try:
            assert first_alter.wait(5)
            second = pool.submit(initialize, True)
            assert second_started.wait(5)
            # Without serialization, the second connection sees the missing
            # column and adds it while the first holds its stale snapshot.
            second_alter.wait(.2)
        finally:
            release.set()
        columns = first.result(timeout=5)
        assert second.result(timeout=5) == columns
    assert len(attempts) == 1
    assert {"last_progress_at", "payload_batch_id", "retry_of_job_id", "abandoned"} <= columns
