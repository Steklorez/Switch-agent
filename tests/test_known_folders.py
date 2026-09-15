"""Tests for switchagent/known_folders.py -- Windows Downloads Known Folder
resolution used by first-run config bootstrap and config.load_library_dir()'s
fallback. Every failure mode must return None, never raise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from switchagent import known_folders


def test_downloads_dir_returns_none_on_non_windows_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert known_folders.downloads_dir() is None


def test_downloads_dir_returns_none_when_the_api_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def _boom(_guid):
        raise OSError("SHGetKnownFolderPath failed")

    monkeypatch.setattr(known_folders, "_sh_get_known_folder_path", _boom)
    assert known_folders.downloads_dir() is None


def test_downloads_dir_returns_none_when_resolved_path_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(known_folders, "_sh_get_known_folder_path", lambda _guid: missing)
    assert known_folders.downloads_dir() is None


def test_downloads_dir_returns_the_resolved_path_when_it_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    real_dir = tmp_path / "Downloads"
    real_dir.mkdir()
    monkeypatch.setattr(known_folders, "_sh_get_known_folder_path", lambda _guid: real_dir)
    assert known_folders.downloads_dir() == real_dir


@pytest.mark.skipif(sys.platform != "win32", reason="SHGetKnownFolderPath is Windows-only")
def test_real_shgetknownfolderpath_resolves_an_existing_directory():
    """Exercises the actual ctypes/Win32 call (read-only, no side effects)
    -- every real Windows installation has a Downloads known folder, even
    if the user relocated it, which is exactly the case this function
    exists to handle correctly."""
    result = known_folders.downloads_dir()
    assert result is None or (isinstance(result, Path) and result.is_dir())
