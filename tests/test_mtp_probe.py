"""Unit tests for the pure helper functions in tools/mtp_probe.py --
mask_serial() and device_fingerprint(). Everything else in that script
talks to real Windows Shell COM and is a diagnostic tool, not part of the
switchagent package -- these two functions are the only parts sensible to
unit test without a real (or mocked) COM environment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


def _load_probe_module():
    spec = importlib.util.spec_from_file_location("mtp_probe", TOOLS_DIR / "mtp_probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mtp_probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe_module()


def test_mask_serial_redacts_only_the_serial_segment():
    path = r"::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    masked = probe.mask_serial(path)

    assert "xtj10229424075" not in masked
    assert "[REDACTED]" in masked
    # everything else must survive untouched
    assert "vid_057e&pid_201d" in masked
    assert "6ac27878-a6fa-4155-ba85-f98f491d4f33" in masked
    assert "20D04FE0-3AEA-1069-A2D8-08002B30309D" in masked


def test_mask_serial_handles_none_and_empty():
    assert probe.mask_serial(None) is None
    assert probe.mask_serial("") == ""


def test_mask_serial_leaves_non_device_paths_unchanged():
    assert probe.mask_serial("C:\\") == "C:\\"
    assert probe.mask_serial("some random string") == "some random string"


def test_mask_serial_handles_storage_child_paths():
    """Storage nodes append \\SID-{...} after the device path -- the serial
    segment must still be found and redacted even with that suffix."""
    path = r"::{GUID}\\\?\usb#vid_057e&pid_201d#SECRETSERIAL#{6ac27878-a6fa-4155-ba85-f98f491d4f33}\SID-{10001,,512647692288}"
    masked = probe.mask_serial(path)
    assert "SECRETSERIAL" not in masked
    assert "SID-{10001,,512647692288}" in masked


def test_device_fingerprint_is_deterministic():
    path = "usb#vid_057e&pid_201d#abc123#{guid}"
    assert probe.device_fingerprint(path) == probe.device_fingerprint(path)


def test_device_fingerprint_differs_for_different_devices():
    path_a = "usb#vid_057e&pid_201d#serialA#{guid}"
    path_b = "usb#vid_057e&pid_201d#serialB#{guid}"
    assert probe.device_fingerprint(path_a) != probe.device_fingerprint(path_b)


def test_device_fingerprint_never_contains_the_raw_serial():
    path = "usb#vid_057e&pid_201d#totallysecretserial#{guid}"
    fp = probe.device_fingerprint(path)
    assert "totallysecretserial" not in fp
    assert len(fp) == 16  # truncated sha256 hex digest
