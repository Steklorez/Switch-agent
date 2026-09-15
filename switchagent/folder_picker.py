"""Native Windows folder selection, isolated from the MTP COM thread."""
import os
import threading

_lock = threading.Lock()


def _bring_dialog_to_front(hwnd, msg, lp, data) -> int:
    """SHBrowseForFolder callback (BrowseCallbackProc) -- forces the dialog
    to the foreground the instant it's created (BFFM_INITIALIZED).

    Without this, the dialog opens with no owner window (hwndOwner=0 below
    -- this process has no window of its own to use as one) and Windows'
    foreground-lock timeout then leaves it buried behind whatever window
    currently has input focus (typically the browser tab the "Choose and
    add a folder..." click came from) -- invisible, not merely inactive,
    since nothing raised it above that window either. A plain
    SetForegroundWindow() from a background process is refused outright by
    that same foreground-lock mechanism; toggling HWND_TOPMOST on and then
    immediately back off first is the standard workaround -- it only
    reorders Z-order (no special permission needed for a window your own
    process owns) and is enough to raise this dialog above the currently
    focused window, at which point SetForegroundWindow succeeds too."""
    import win32con
    import win32gui
    from win32com.shell import shellcon
    if msg == shellcon.BFFM_INITIALIZED:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetForegroundWindow(hwnd)
    return 0


def pick_folder() -> str | None:
    if os.name != "nt":
        raise RuntimeError("Folder picking is only available on Windows. Enter the path manually.")
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A folder picker dialog is already open")
    try:
        import pythoncom
        from win32com.shell import shell, shellcon
        pythoncom.CoInitialize()
        try:
            result = shell.SHBrowseForFolder(
                0, None, "Choose a folder with games or mods",
                shellcon.BIF_RETURNONLYFSDIRS | 0x0040,  # BIF_NEWDIALOGSTYLE is absent in pywin32 shellcon
                _bring_dialog_to_front, None,
            )
            if not result or not result[0]:
                return None
            return shell.SHGetPathFromIDListW(result[0])
        finally:
            pythoncom.CoUninitialize()
    finally:
        _lock.release()
