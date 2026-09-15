"""Tests for switchagent/paths.py -- the dev/installed/portable runtime-mode
model that switchagent/config.py's writable-data paths are built on top of.

Frozen mode is simulated by monkeypatching sys.frozen/sys.executable/
sys._MEIPASS (raising=False since these normally don't exist as sys
attributes at all outside a real PyInstaller build) -- no actual
PyInstaller build is needed to exercise this logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

from switchagent import paths


# ---------------------------------------------------------------------------
# dev mode (the default outside any frozen build, i.e. every pytest run)
# ---------------------------------------------------------------------------

def test_dev_mode_is_not_frozen():
    assert paths.is_frozen() is False


def test_dev_mode_is_never_portable():
    assert paths.is_portable() is False


def test_dev_mode_runtime_mode_is_dev():
    assert paths.runtime_mode() == "dev"


def test_dev_mode_app_data_root_equals_resource_root():
    """The one invariant that makes "existing dev workflows keep working"
    true: before this module existed, every writable path AND every
    resource path were the same project root."""
    assert paths.app_data_root() == paths.resource_root()


def test_dev_mode_resource_root_is_the_checked_out_project_root():
    # switchagent/paths.py -> switchagent/ -> project root
    expected = Path(__file__).resolve().parent.parent
    assert paths.resource_root() == expected


# ---------------------------------------------------------------------------
# frozen: installed mode (no portable.flag next to the executable)
# ---------------------------------------------------------------------------

def test_frozen_without_marker_is_installed_mode(tmp_path, monkeypatch):
    fake_exe = tmp_path / "SwitchAgent.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert paths.is_frozen() is True
    assert paths.is_portable() is False
    assert paths.runtime_mode() == "installed"


def test_installed_mode_app_data_root_is_under_localappdata(tmp_path, monkeypatch):
    fake_exe = tmp_path / "SwitchAgent.exe"
    fake_local_appdata = tmp_path / "LocalAppData"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local_appdata))

    assert paths.app_data_root() == fake_local_appdata / "SwitchAgent"


def test_installed_mode_never_hardcodes_a_username(tmp_path, monkeypatch):
    """The task's explicit requirement: resolve LOCALAPPDATA via the
    environment, never bake in a specific username."""
    fake_exe = tmp_path / "SwitchAgent.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    root = paths.app_data_root()
    assert root.name == "SwitchAgent"
    assert "Local" in root.parts
    assert "AppData" in root.parts


def test_frozen_resource_root_uses_meipass_when_set(tmp_path, monkeypatch):
    fake_exe = tmp_path / "SwitchAgent.exe"
    fake_bundle = tmp_path / "_internal"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_bundle), raising=False)

    assert paths.resource_root() == fake_bundle


def test_frozen_resource_root_falls_back_to_executable_dir_without_meipass(tmp_path, monkeypatch):
    fake_exe = tmp_path / "SwitchAgent.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert paths.resource_root() == tmp_path


# ---------------------------------------------------------------------------
# frozen: portable mode (portable.flag present next to the executable)
# ---------------------------------------------------------------------------

def test_frozen_with_marker_is_portable_mode(tmp_path, monkeypatch):
    fake_exe = tmp_path / "SwitchAgent.exe"
    (tmp_path / "portable.flag").write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert paths.is_portable() is True
    assert paths.runtime_mode() == "portable"


def test_portable_mode_app_data_root_is_next_to_the_executable(tmp_path, monkeypatch):
    fake_exe = tmp_path / "SwitchAgent.exe"
    (tmp_path / "portable.flag").write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert paths.app_data_root() == tmp_path


def test_portable_mode_never_touches_localappdata(tmp_path, monkeypatch):
    """Regression guard: a portable build must stay entirely self-
    contained even if LOCALAPPDATA happens to be set in the environment
    (it always is, on a real Windows machine) -- the marker file's
    presence must take priority."""
    fake_exe = tmp_path / "SwitchAgent.exe"
    (tmp_path / "portable.flag").write_text("")
    other_appdata = tmp_path / "should-not-be-used"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setenv("LOCALAPPDATA", str(other_appdata))

    assert paths.app_data_root() == tmp_path
    assert not other_appdata.exists()


def test_missing_marker_does_not_accidentally_trigger_portable_mode(tmp_path, monkeypatch):
    fake_exe = tmp_path / "SwitchAgent.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert not (tmp_path / "portable.flag").exists()
    assert paths.is_portable() is False
