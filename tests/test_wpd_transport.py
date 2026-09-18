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


# ---------------------------------------------------------------------------
# A device that is still finalising is not a failed transfer (2026-09-19).
#
# Field regression: a 388 MiB .nsz whose console screen reported the install
# finished in 35s made IStream::Commit sit for 82s and then return
# ERROR_SEM_TIMEOUT. That was treated as a transport fault, so the Shell
# fallback re-sent the whole file -- the console installed the same game
# twice and the job took 192s instead of ~15s.
# ---------------------------------------------------------------------------


class _FakeSession:
    """Just enough WpdSession for send_file's own decision-making."""

    def __init__(self, timing):
        self.timing = timing
        self.sent = 0
        self.sizes = {}

    def navigate(self, _root, _path, *, create_missing):
        return "parent"

    def child_id(self, _parent, name):
        return name if name in self.sizes else None

    def object_size(self, object_id):
        return self.sizes[object_id]

    def send_file(self, _parent, filename, source_path, *, progress=None):
        self.sent += 1
        if progress is not None:
            progress(self.timing.bytes_written, self.timing.bytes_written)
        if not self.timing.finalise_timed_out:
            self.sizes[filename] = source_path.stat().st_size
        return self.timing


def _backend_with(session, monkeypatch, storage_object_id="s10005"):
    from switchagent.mtp.windows import RealMtpBackend

    backend = RealMtpBackend("::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\dev")
    monkeypatch.setattr(backend, "_require_connected", lambda: None)
    monkeypatch.setattr(backend, "_wpd_session", lambda: session)
    monkeypatch.setattr(backend, "_wpd_storage_id", lambda _s, _storage: storage_object_id)

    def _no_shell(*_args, **_kwargs):
        raise AssertionError("fell back to the Shell copy engine -- that re-sends the whole file")

    monkeypatch.setattr(backend, "_send_via_shell", _no_shell)
    return backend


def test_a_finalise_timeout_reports_unverified_and_never_re_sends(tmp_path, monkeypatch):
    from switchagent.mtp.base import TransferStatus

    source = tmp_path / "Stardew Valley [0100E65002BB8000][v0] (0.87 GB).nsz"
    source.write_bytes(b"x" * 4096)
    session = _FakeSession(wpd.TransferTiming(0.01, 11.0, 82.0, 4096, finalise_timed_out=True))
    backend = _backend_with(session, monkeypatch)

    result = backend.send_file("SD_INSTALL", source.name, source)

    assert result.status is TransferStatus.UNVERIFIED
    assert result.bytes_sent == 4096
    assert session.sent == 1, "the file was sent more than once"
    assert "did not answer the end of the transfer" in result.error
    # The connection must stay on WPD: one slow console does not make the
    # transport broken, and demoting it sent every later file of the same
    # batch back through the Shell with no progress reporting at all.
    assert backend._wpd_unavailable is False


def test_a_clean_install_transfer_is_unverified_with_the_measured_rate(tmp_path, monkeypatch):
    from switchagent.mtp.base import TransferStatus

    source = tmp_path / "Game [0100000000010000][v0].nsp"
    source.write_bytes(b"y" * 8192)
    session = _FakeSession(wpd.TransferTiming(0.0, 1.0, 2.69, 8192))
    backend = _backend_with(session, monkeypatch)

    result = backend.send_file("SD_INSTALL", source.name, source)

    assert result.status is TransferStatus.UNVERIFIED
    assert result.bytes_sent == 8192
    assert "MB/s" in result.error


def test_a_mod_file_is_completed_only_after_reading_its_size_back(tmp_path, monkeypatch):
    from switchagent.mtp.base import TransferStatus

    source = tmp_path / "text.xml"
    source.write_bytes(b"z" * 321)
    session = _FakeSession(wpd.TransferTiming(0.0, 0.1, 0.05, 321))
    backend = _backend_with(session, monkeypatch, storage_object_id="s10001")

    result = backend.send_file("SD_CARD", "atmosphere/contents/0100/romfs/text.xml", source)

    assert result.status is TransferStatus.COMPLETED
    assert result.bytes_sent == 321


def test_a_short_write_is_failed_not_silently_accepted(tmp_path, monkeypatch):
    from switchagent.mtp.base import TransferStatus

    source = tmp_path / "Game [0100000000010000][v0].nsp"
    source.write_bytes(b"y" * 8192)
    session = _FakeSession(wpd.TransferTiming(0.0, 1.0, 0.1, 4096))  # device took half
    backend = _backend_with(session, monkeypatch)

    result = backend.send_file("SD_INSTALL", source.name, source)

    assert result.status is TransferStatus.FAILED
    assert "4096 of 8192" in result.error
