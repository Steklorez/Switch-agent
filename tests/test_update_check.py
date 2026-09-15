"""Tests for switchagent/update_check.py -- W3-008. Never a real network
call: _fetch_latest_release is always monkeypatched. Covers caching,
the min-interval throttle, force=True bypassing it, version comparison,
and the required silent/non-fatal error handling.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from switchagent import update_check


def test_first_check_ever_hits_the_network_and_caches_the_result(tmp_path, monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest_release", lambda repo_slug: ("0.2.0", "https://example.invalid/releases/0.2.0"))

    result = update_check.check_for_update("0.1.0", app_data_root=tmp_path)

    assert result.latest_version == "0.2.0"
    assert result.release_url == "https://example.invalid/releases/0.2.0"
    assert result.update_available is True
    assert result.error is None
    assert result.checked_at is not None

    cache_path = tmp_path / "update_check_cache.json"
    assert cache_path.is_file()
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["latest_version"] == "0.2.0"


def test_no_update_available_when_current_is_already_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest_release", lambda repo_slug: ("0.1.0", "https://example.invalid/releases/0.1.0"))

    result = update_check.check_for_update("0.1.0", app_data_root=tmp_path)
    assert result.update_available is False


def test_fresh_cache_within_interval_never_touches_the_network(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(update_check, "_fetch_latest_release", lambda repo_slug: calls.append(1) or ("0.2.0", "url"))

    update_check.check_for_update("0.1.0", app_data_root=tmp_path)
    assert len(calls) == 1

    result = update_check.check_for_update("0.1.0", app_data_root=tmp_path)
    assert len(calls) == 1  # second call within the interval must not hit the network again
    assert result.latest_version == "0.2.0"  # still returns the cached result correctly


def test_force_bypasses_the_interval_even_immediately_after_a_check(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(update_check, "_fetch_latest_release", lambda repo_slug: calls.append(1) or ("0.2.0", "url"))

    update_check.check_for_update("0.1.0", app_data_root=tmp_path)
    update_check.check_for_update("0.1.0", app_data_root=tmp_path, force=True)
    assert len(calls) == 2


def test_stale_cache_past_the_interval_triggers_a_fresh_check(tmp_path, monkeypatch):
    old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    (tmp_path / "update_check_cache.json").write_text(
        json.dumps({"checked_at": old_timestamp, "latest_version": "0.1.5", "release_url": "old-url"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(update_check, "_fetch_latest_release", lambda repo_slug: ("0.3.0", "new-url"))

    result = update_check.check_for_update("0.1.0", app_data_root=tmp_path, min_interval_hours=24.0)
    assert result.latest_version == "0.3.0"


def test_network_failure_is_silent_and_falls_back_to_stale_cache(tmp_path, monkeypatch):
    old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    (tmp_path / "update_check_cache.json").write_text(
        json.dumps({"checked_at": old_timestamp, "latest_version": "0.1.5", "release_url": "old-url"}),
        encoding="utf-8",
    )

    def _raise(repo_slug):
        raise OSError("network is down")

    monkeypatch.setattr(update_check, "_fetch_latest_release", _raise)

    result = update_check.check_for_update("0.1.0", app_data_root=tmp_path, min_interval_hours=24.0)
    assert result.error == "network is down"
    assert result.latest_version == "0.1.5"  # falls back to the last known-good value, not a crash
    assert result.update_available is True  # 0.1.5 > 0.1.0, still correctly derived from the stale cache


def test_network_failure_with_no_cache_at_all_returns_an_inert_result(tmp_path, monkeypatch):
    def _raise(repo_slug):
        raise OSError("network is down")

    monkeypatch.setattr(update_check, "_fetch_latest_release", _raise)

    result = update_check.check_for_update("0.1.0", app_data_root=tmp_path)
    assert result.update_available is False
    assert result.latest_version is None
    assert result.error == "network is down"


def test_malformed_github_response_is_also_silent_and_non_fatal(tmp_path, monkeypatch):
    def _raise(repo_slug):
        raise KeyError("tag_name")

    monkeypatch.setattr(update_check, "_fetch_latest_release", _raise)

    result = update_check.check_for_update("0.1.0", app_data_root=tmp_path)
    assert result.update_available is False
    assert result.error is not None


@pytest.mark.parametrize("latest,current,expected", [
    ("0.2.0", "0.1.0", True),
    ("0.10.0", "0.9.0", True),   # numeric segment comparison, not lexicographic
    ("0.1.0", "0.1.0", False),
    ("0.1.0", "0.2.0", False),
    ("v0.2.0", "0.1.0", True),   # leading v is stripped
])
def test_version_comparison(latest, current, expected):
    assert update_check._is_newer(latest, current) is expected


def test_corrupted_cache_file_is_treated_as_no_cache_not_a_crash(tmp_path, monkeypatch):
    (tmp_path / "update_check_cache.json").write_text("not valid json{{{", encoding="utf-8")
    monkeypatch.setattr(update_check, "_fetch_latest_release", lambda repo_slug: ("0.2.0", "url"))

    result = update_check.check_for_update("0.1.0", app_data_root=tmp_path)
    assert result.latest_version == "0.2.0"  # recovered by just doing a fresh check
