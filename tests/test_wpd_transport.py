"""Unit tests for switchagent/mtp/wpd.py's device-independent parts.

Everything here runs without a console attached: identity conversion, the
timing record, and the ctypes value types. The parts that need real hardware
(streaming, folder creation, property reads) are exercised by
tools/wpd_transfer_test.py against an actual device -- see docs/PERF-MTP.md
for the measurements that justified this transport existing at all.
"""

from __future__ import annotations

import pytest

from switchagent.mtp import wpd

# The exact shape switchagent stores for a real console (serial redacted) --
# see mtp/base.py on device identity.
REAL_DEVICE_ID = (
    "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\"
    "\\\\?\\usb#vid_057e&pid_201d#xxxxxxxxxxxxx#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
)


def test_pnp_id_is_the_device_id_minus_its_shell_namespace_prefix():
    pnp = wpd.pnp_id_from_device_id(REAL_DEVICE_ID)
    assert pnp == "\\\\?\\usb#vid_057e&pid_201d#xxxxxxxxxxxxx#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"


def test_device_id_round_trips_through_the_pnp_id():
    assert wpd.device_id_from_pnp_id(wpd.pnp_id_from_device_id(REAL_DEVICE_ID)) == REAL_DEVICE_ID


@pytest.mark.parametrize("device_id", [
    "",
    None,
    "mock-switch-parent",                                   # the mock backend's ids
    "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\",           # prefix but nothing after it
    "C:\\Users\\someone\\game.nsp",
])
def test_unconvertible_device_ids_yield_none_rather_than_a_guess(device_id):
    """None is what makes RealMtpBackend fall back to the Shell transport
    instead of inventing a WPD id for a device it cannot name -- this project
    never substitutes a different target than the one a job was created
    for."""
    assert wpd.pnp_id_from_device_id(device_id) is None


def test_transfer_timing_totals_and_rate():
    timing = wpd.TransferTiming(
        create_seconds=0.5, stream_seconds=4.0, commit_seconds=1.5,
        bytes_written=8 * 1024 * 1024,
    )
    assert timing.total_seconds == pytest.approx(6.0)
    assert timing.stream_mb_per_second == pytest.approx(2.0)
    assert "4.00s" in str(timing) and "MB/s" in str(timing)


def test_transfer_timing_reports_zero_rate_instead_of_dividing_by_zero():
    timing = wpd.TransferTiming(0.0, 0.0, 0.0, 0)
    assert timing.stream_mb_per_second == 0.0


def test_guid_parses_and_formats_back_to_the_same_text():
    text = "{27E2E392-A111-48E0-AB0C-E17705A05F85}"
    assert str(wpd.GUID(text)) == text


def test_property_keys_are_value_comparable():
    same = wpd.PROPERTYKEY("{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}", 4)
    assert same == wpd.WPD_OBJECT_NAME
    assert hash(same) == hash(wpd.WPD_OBJECT_NAME)
    assert same != wpd.WPD_OBJECT_SIZE


def test_object_name_prefers_the_original_file_name():
    """DBI reports both; the original file name is the one that matches what
    the Shell shows and what a destination path is built from."""
    props = {
        str(wpd.WPD_OBJECT_NAME): "display name",
        str(wpd.WPD_OBJECT_ORIGINAL_FILE_NAME): "Game [0100000000010000][v0].nsp",
    }
    assert wpd.object_name(props) == "Game [0100000000010000][v0].nsp"
    assert wpd.object_name({str(wpd.WPD_OBJECT_NAME): "display name"}) == "display name"
    assert wpd.object_name({}) is None
