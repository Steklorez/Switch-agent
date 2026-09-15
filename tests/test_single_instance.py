"""Tests for switchagent/single_instance.py.

acquire_mutex()/release_mutex() do real Win32 calls -- exercised for real
(this project only ever runs on Windows) rather than mocked. Everything
else (runtime-info file, health probe, retry loop) is pure Python and
fully isolated from any real network/process.
"""

from __future__ import annotations

import json

from switchagent import single_instance


# ---------------------------------------------------------------------------
# runtime-info file
# ---------------------------------------------------------------------------

def test_write_then_read_runtime_info_round_trips(tmp_path):
    path = tmp_path / "runtime.json"
    single_instance.write_runtime_info(path, port=8765)

    info = single_instance.read_runtime_info(path)

    assert info is not None
    assert info.port == 8765
    assert info.pid > 0


def test_read_runtime_info_returns_none_when_file_is_absent(tmp_path):
    assert single_instance.read_runtime_info(tmp_path / "missing.json") is None


def test_read_runtime_info_returns_none_for_corrupt_json(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert single_instance.read_runtime_info(path) is None


def test_read_runtime_info_returns_none_when_a_required_field_is_missing(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"pid": 123}), encoding="utf-8")  # no "port"
    assert single_instance.read_runtime_info(path) is None


def test_clear_runtime_info_is_a_no_op_when_the_file_does_not_exist(tmp_path):
    single_instance.clear_runtime_info(tmp_path / "missing.json")  # must not raise


def test_clear_runtime_info_removes_the_file(tmp_path):
    path = tmp_path / "runtime.json"
    single_instance.write_runtime_info(path, port=1)
    single_instance.clear_runtime_info(path)
    assert not path.exists()


def test_write_runtime_info_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "runtime.json"
    single_instance.write_runtime_info(path, port=1)
    assert path.exists()


# ---------------------------------------------------------------------------
# probe_health -- never trusts anything without a live, correct response
# ---------------------------------------------------------------------------

def test_probe_health_true_for_a_correct_switchagent_response():
    def fake_get(url, timeout):
        return 200, json.dumps({"app": "SwitchAgent", "version": "0.1.0", "status": "ok"}).encode()

    assert single_instance.probe_health(8765, http_get=fake_get) is True


def test_probe_health_false_when_connection_fails():
    def fake_get(url, timeout):
        raise OSError("connection refused")

    assert single_instance.probe_health(8765, http_get=fake_get) is False


def test_probe_health_false_for_non_200_status():
    def fake_get(url, timeout):
        return 404, b"not found"

    assert single_instance.probe_health(8765, http_get=fake_get) is False


def test_probe_health_false_for_malformed_json():
    def fake_get(url, timeout):
        return 200, b"not json at all"

    assert single_instance.probe_health(8765, http_get=fake_get) is False


def test_probe_health_false_when_a_different_app_answers_on_that_port():
    """Guards against mistaking some unrelated service that happens to be
    listening on the recorded port for a genuine SwitchAgent instance."""
    def fake_get(url, timeout):
        return 200, json.dumps({"app": "SomethingElse", "status": "ok"}).encode()

    assert single_instance.probe_health(8765, http_get=fake_get) is False


def test_probe_health_requests_the_expected_url_and_port():
    seen = {}

    def fake_get(url, timeout):
        seen["url"] = url
        return 200, json.dumps({"app": "SwitchAgent", "status": "ok"}).encode()

    single_instance.probe_health(9999, http_get=fake_get)
    assert seen["url"] == "http://127.0.0.1:9999/api/health"


# ---------------------------------------------------------------------------
# wait_for_existing_instance -- bounded retry over probe_health
# ---------------------------------------------------------------------------

def test_wait_for_existing_instance_returns_true_immediately_when_already_alive():
    sleeps = []
    assert single_instance.wait_for_existing_instance(
        8765, probe=lambda port: True, sleep_fn=sleeps.append,
    ) is True
    assert sleeps == []


def test_wait_for_existing_instance_retries_before_giving_up():
    calls = {"n": 0}

    def flaky_probe(port):
        calls["n"] += 1
        return calls["n"] >= 3

    sleeps = []
    result = single_instance.wait_for_existing_instance(
        8765, attempts=5, probe=flaky_probe, sleep_fn=sleeps.append,
    )

    assert result is True
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept between attempts 1->2 and 2->3, not after success


def test_wait_for_existing_instance_returns_false_after_exhausting_attempts():
    sleeps = []
    result = single_instance.wait_for_existing_instance(
        8765, attempts=3, probe=lambda port: False, sleep_fn=sleeps.append,
    )

    assert result is False
    assert len(sleeps) == 2  # never sleeps after the final attempt


# ---------------------------------------------------------------------------
# acquire_mutex / release_mutex -- real Win32 calls
# ---------------------------------------------------------------------------

def test_acquire_mutex_reports_already_running_on_a_second_acquire():
    handle_a, already_running_a = single_instance.acquire_mutex()
    try:
        assert already_running_a is False

        handle_b, already_running_b = single_instance.acquire_mutex()
        try:
            assert already_running_b is True
        finally:
            single_instance.release_mutex(handle_b)
    finally:
        single_instance.release_mutex(handle_a)


def test_mutex_is_free_again_after_both_handles_are_released():
    handle_a, _ = single_instance.acquire_mutex()
    single_instance.release_mutex(handle_a)

    handle_b, already_running_b = single_instance.acquire_mutex()
    try:
        assert already_running_b is False
    finally:
        single_instance.release_mutex(handle_b)


def test_release_mutex_tolerates_none():
    single_instance.release_mutex(None)  # must not raise
