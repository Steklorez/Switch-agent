"""Tests for switchagent/tray.py's pure message-routing logic.

_TrayWindow itself (real win32gui window creation + PumpMessages) is not
exercised here -- it needs a real interactive Windows desktop session,
which this test environment does not reliably have. Only the decision
functions it delegates to are tested, matching the module's own docstring
about what is/isn't covered.
"""

from __future__ import annotations

import sys

from switchagent import tray
from types import SimpleNamespace
from unittest.mock import Mock
import pytest


def test_classify_tray_click_left_click_means_open():
    assert tray._classify_tray_click(
        501, wm_lbuttonup=501, wm_lbuttondblclk=515, wm_rbuttonup=517,
    ) == "open"


def test_classify_tray_click_double_click_means_open():
    assert tray._classify_tray_click(
        515, wm_lbuttonup=501, wm_lbuttondblclk=515, wm_rbuttonup=517,
    ) == "open"


def test_classify_tray_click_right_click_means_menu():
    assert tray._classify_tray_click(
        517, wm_lbuttonup=501, wm_lbuttondblclk=515, wm_rbuttonup=517,
    ) == "menu"


def test_classify_tray_click_unrelated_message_is_ignored():
    assert tray._classify_tray_click(
        999, wm_lbuttonup=501, wm_lbuttondblclk=515, wm_rbuttonup=517,
    ) == "ignore"


def test_classify_menu_command_open_id():
    assert tray._classify_menu_command(tray.MENU_OPEN_ID) == "open"


def test_classify_menu_command_exit_id():
    assert tray._classify_menu_command(tray.MENU_EXIT_ID) == "exit"


def test_classify_menu_command_unknown_id_is_ignored():
    assert tray._classify_menu_command(99999) == "ignore"


def test_menu_ids_are_distinct():
    assert tray.MENU_OPEN_ID != tray.MENU_EXIT_ID


def test_default_icon_path_returns_none_when_no_icon_asset_is_bundled():
    """Dev checkout has no switchagent/web/static/app.ico committed yet
    (see Stage 7) -- must degrade to None, not raise."""
    result = tray._default_icon_path()
    assert result is None or result.is_file()


def test_classify_menu_command_folder_ids():
    assert tray._classify_menu_command(tray.MENU_FOLDER_BASE_ID, 2) == "folder:0"
    assert tray._classify_menu_command(tray.MENU_FOLDER_BASE_ID + 1, 2) == "folder:1"
    assert tray._classify_menu_command(tray.MENU_FOLDER_BASE_ID + 2, 2) == "ignore"


def test_folder_entries_installed_build_lists_program_data_and_logs(tmp_path):
    program, data = tmp_path / "Program Files" / "SwitchAgent", tmp_path / "LocalAppData" / "SwitchAgent"
    entries = tray._folder_entries(program_dir=program, data_dir=data, logs_dir=data / "logs")
    assert entries == [("Open program folder", program), ("Open data folder", data),
                       ("Open logs folder", data / "logs")]


def test_folder_entries_portable_build_does_not_repeat_the_same_folder(tmp_path):
    entries = tray._folder_entries(program_dir=tmp_path, data_dir=tmp_path, logs_dir=tmp_path / "logs")
    assert entries == [("Open program folder", tmp_path), ("Open logs folder", tmp_path / "logs")]


@pytest.mark.parametrize("selected", [tray.MENU_EXIT_ID, tray.MENU_OPEN_ID, tray.MENU_FOLDER_BASE_ID, 0])
def test_popup_uses_menu_handle_and_dispatches_selection_then_releases_menu(selected, tmp_path, monkeypatch):
    missing = tmp_path / "logs"  # not created -- must not be offered
    monkeypatch.setattr(tray, "_current_folder_entries",
                        lambda: [("Open program folder", tmp_path), ("Open logs folder", missing)])
    opened = []
    monkeypatch.setattr(tray.os, "startfile", opened.append, raising=False)
    window = object.__new__(tray._TrayWindow)
    window._hwnd = 42
    window._exiting = False
    window._on_open = Mock()
    window._win32api = SimpleNamespace(LOWORD=lambda value: value & 0xffff)
    window._win32con = SimpleNamespace(MF_STRING=0, MF_SEPARATOR=0x800, TPM_LEFTALIGN=0, TPM_RIGHTBUTTON=2,
        TPM_RETURNCMD=256, TPM_NONOTIFY=128, WM_NULL=0, WM_COMMAND=273, WM_CLOSE=16)
    entries = []

    # Strict real API arity reproduces the old missing-hMenu TypeError.
    def append_menu(handle, flags, command, label):
        assert handle == 99
        # pywin32 refuses None here (TypeError) -- a separator needs "".
        assert isinstance(label, str)
        entries.append((command, label))

    gui = Mock()
    gui.AppendMenu = append_menu
    gui.CreatePopupMenu.return_value = 99
    gui.GetCursorPos.return_value = (10, 20)
    gui.TrackPopupMenu.return_value = selected
    window._win32gui = gui
    window._show_menu()
    assert entries == [(tray.MENU_OPEN_ID, "Open SwitchAgent"), (0, ""),
                       (tray.MENU_FOLDER_BASE_ID, "Open program folder"), (0, ""),
                       (tray.MENU_EXIT_ID, "Exit")]
    gui.DestroyMenu.assert_called_once_with(99)
    if selected == tray.MENU_EXIT_ID:
        gui.PostMessage.assert_any_call(42, 16, 0, 0)
        window._on_close(42, 16, 0, 0)
        gui.DestroyWindow.assert_called_once_with(42)
        window._on_destroy(42, 2, 0, 0)
        gui.PostQuitMessage.assert_called_once_with(0)
        assert window._exiting
    elif selected == tray.MENU_OPEN_ID:
        window._on_open.assert_called_once()
    elif selected == tray.MENU_FOLDER_BASE_ID:
        assert opened == [str(tmp_path)]
        window._on_open.assert_not_called()
    else:
        window._on_open.assert_not_called()
        gui.PostMessage.assert_called_once_with(42, 0, 0, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="real pywin32 menu")
def test_menu_builds_with_the_real_win32_api(tmp_path, monkeypatch):
    """The mocked test above cannot catch an argument pywin32 itself
    rejects -- None as a separator's label passed it and broke the real
    menu. This builds the menu through the real AppendMenu and only stubs
    out showing it."""
    import win32con
    import win32gui

    monkeypatch.setattr(tray, "_current_folder_entries", lambda: [("Open program folder", tmp_path)])
    window = object.__new__(tray._TrayWindow)
    window._hwnd = 0
    window._exiting = False
    window._on_open = Mock()
    window._win32api = SimpleNamespace(LOWORD=lambda value: value & 0xffff)
    window._win32con = win32con
    built = {}

    class RealMenuGui:
        def __getattr__(self, name):
            return getattr(win32gui, name)

        def TrackPopupMenu(self, menu, *args):
            built["count"] = win32gui.GetMenuItemCount(menu)
            return 0

        def GetCursorPos(self):
            return (0, 0)  # needs an interactive desktop, not what's tested

        def SetForegroundWindow(self, hwnd):
            pass

        def PostMessage(self, *args):
            pass

    window._win32gui = RealMenuGui()
    window._show_menu()
    assert built["count"] == 5  # Open, separator, 1 folder, separator, Exit
