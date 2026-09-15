"""Tests for switchagent/tray.py's pure message-routing logic.

_TrayWindow itself (real win32gui window creation + PumpMessages) is not
exercised here -- it needs a real interactive Windows desktop session,
which this test environment does not reliably have. Only the decision
functions it delegates to are tested, matching the module's own docstring
about what is/isn't covered.
"""

from __future__ import annotations

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


@pytest.mark.parametrize("selected", [tray.MENU_EXIT_ID, tray.MENU_OPEN_ID, 0])
def test_popup_uses_menu_handle_and_dispatches_selection_then_releases_menu(selected):
    window = object.__new__(tray._TrayWindow)
    window._hwnd = 42
    window._exiting = False
    window._on_open = Mock()
    window._win32api = SimpleNamespace(LOWORD=lambda value: value & 0xffff)
    window._win32con = SimpleNamespace(MF_STRING=0, TPM_LEFTALIGN=0, TPM_RIGHTBUTTON=2,
        TPM_RETURNCMD=256, TPM_NONOTIFY=128, WM_NULL=0, WM_COMMAND=273, WM_CLOSE=16)
    entries = []

    # Strict real API arity reproduces the old missing-hMenu TypeError.
    def append_menu(handle, flags, command, label):
        assert handle == 99
        entries.append((command, label))

    gui = Mock()
    gui.AppendMenu = append_menu
    gui.CreatePopupMenu.return_value = 99
    gui.GetCursorPos.return_value = (10, 20)
    gui.TrackPopupMenu.return_value = selected
    window._win32gui = gui
    window._show_menu()
    assert entries == [(tray.MENU_OPEN_ID, "Open SwitchAgent"),
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
    else:
        window._on_open.assert_not_called()
        gui.PostMessage.assert_called_once_with(42, 0, 0, 0)
