"""Tests for the mock MTP backend itself (switchagent/mtp/mock.py) --
everything the abstract interface (switchagent/mtp/base.py) promises,
exercised without any real Switch/USB involved. See tests/test_transfer.py
for the pipeline-level integration test.
"""

from __future__ import annotations

import pytest

from switchagent.mtp import (
    DestinationNotFoundError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    FileAlreadyExistsError,
    InvalidOperationError,
    MockMtpBackend,
    StorageNotFoundError,
    TransferStatus,
)


@pytest.fixture
def source_file(tmp_path):
    def _make(name="game.nsp", content=b"nsp bytes"):
        p = tmp_path / name
        p.write_bytes(content)
        return p
    return _make


# 1. Connecting -------------------------------------------------------------

def test_connect_succeeds_when_device_present():
    backend = MockMtpBackend()
    info = backend.connect()
    assert info.connected is True
    assert backend.is_connected is True


def test_connect_raises_device_not_found_when_absent():
    backend = MockMtpBackend(device_present=False)
    with pytest.raises(DeviceNotFoundError):
        backend.connect()
    assert backend.is_connected is False


def test_operations_before_connect_raise_device_disconnected():
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    with pytest.raises(DeviceDisconnectedError):
        backend.list_storages()


# 2. Storage ------------------------------------------------------------

def test_list_storages_returns_registered_storages():
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD", writable=True)
    backend.add_storage("SD_INSTALL", writable=True)
    backend.connect()
    names = {s.name for s in backend.list_storages()}
    assert names == {"SD_CARD", "SD_INSTALL"}


def test_list_storages_reports_raw_name_defaulting_to_logical_name():
    """UI-007: raw_name defaults to the same string as name (mock storages
    are always declared unambiguous), so every pre-existing test that
    calls add_storage() without raw_name keeps working unchanged."""
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    info = backend.list_storages()[0]
    assert info.name == "SD_CARD"
    assert info.raw_name == "SD_CARD"


def test_list_storages_reports_an_explicit_raw_name():
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL", raw_name="5: SD Card install")
    backend.connect()
    info = backend.list_storages()[0]
    assert info.name == "SD_INSTALL"
    assert info.raw_name == "5: SD Card install"


def test_set_storage_overrides_is_recorded_in_operation_log():
    """UI-007: proves the orchestration layer's call (WebContext.
    refresh_devices) is actually observable/inspectable via this mock --
    the mock deliberately does not change its own routing based on this
    (see MockStorage.raw_name's own docstring); RealMtpBackend is where
    override resolution actually changes behavior, covered by
    test_mtp_windows.py's dedicated resolve_storage_name() tests."""
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD", raw_name="9: Weird Vendor Name")
    backend.connect()

    backend.set_storage_overrides({"9: Weird Vendor Name": "SD_INSTALL"})

    calls = [e for e in backend.operation_log if e.operation == "SET_STORAGE_OVERRIDES"]
    assert len(calls) == 1
    assert calls[0].details["overrides"] == {"9: Weird Vendor Name": "SD_INSTALL"}
    # the mock's OWN storage lookup is unaffected -- still reachable by
    # its original logical name, exactly as before this call
    assert backend.get_storage("SD_CARD").name == "SD_CARD"


def test_get_storage_missing_raises(source_file):
    backend = MockMtpBackend()
    backend.connect()
    with pytest.raises(StorageNotFoundError):
        backend.get_storage("SD_CARD")


# 3. Destination / directories ------------------------------------------

def test_ensure_directory_creates_destination():
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    assert backend.exists("SD_CARD", "atmosphere/contents/0100000000010000") is False
    backend.ensure_directory("SD_CARD", "atmosphere/contents/0100000000010000")
    assert backend.exists("SD_CARD", "atmosphere/contents/0100000000010000") is True


def test_ensure_directory_is_idempotent():
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    backend.ensure_directory("SD_CARD", "atmosphere/contents/0100000000010000")
    backend.ensure_directory("SD_CARD", "atmosphere/contents/0100000000010000")  # must not raise


def test_ensure_directory_missing_storage_raises():
    backend = MockMtpBackend()
    backend.connect()
    with pytest.raises(StorageNotFoundError):
        backend.ensure_directory("SD_CARD", "anything")


# 4. Single file transfer -------------------------------------------------

def test_send_single_file_succeeds_and_is_readable_back(source_file):
    src = source_file(content=b"hello switch")
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    result = backend.send_file("SD_INSTALL", "game.nsp", src)
    assert result.status is TransferStatus.COMPLETED
    assert result.bytes_sent == len(b"hello switch")
    assert backend.exists("SD_INSTALL", "game.nsp") is True
    assert backend.storage_tree("SD_INSTALL").read_file("game.nsp") == b"hello switch"


# 5. Multiple files ---------------------------------------------------------

def test_send_multiple_files_all_land(tmp_path):
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    backend.ensure_directory("SD_CARD", "atmosphere/contents/0100000000010000/romfs")

    for i in range(3):
        f = tmp_path / f"asset{i}.bin"
        f.write_bytes(f"content-{i}".encode())
        r = backend.send_file("SD_CARD", f"atmosphere/contents/0100000000010000/romfs/asset{i}.bin", f)
        assert r.status is TransferStatus.COMPLETED

    files = backend.storage_tree("SD_CARD").list_files()
    assert len(files) == 3
    for i in range(3):
        assert f"atmosphere/contents/0100000000010000/romfs/asset{i}.bin" in files


# 6. Content verification after transfer --------------------------------

def test_content_matches_source_after_transfer(source_file):
    src = source_file(content=b"exact bytes 12345")
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    backend.send_file("SD_INSTALL", "game.nsp", src)
    assert backend.storage_tree("SD_INSTALL").read_file("game.nsp") == b"exact bytes 12345"


# 7. Re-sending an existing file -----------------------------------------

def test_resend_existing_file_without_overwrite_raises(source_file):
    src = source_file()
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    backend.send_file("SD_INSTALL", "game.nsp", src)
    with pytest.raises(FileAlreadyExistsError):
        backend.send_file("SD_INSTALL", "game.nsp", src)
    # original content survives an attempted (rejected) re-send
    assert backend.storage_tree("SD_INSTALL").read_file("game.nsp") == src.read_bytes()


def test_resend_existing_file_with_overwrite_replaces_it(tmp_path):
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    first = tmp_path / "v1.nsp"
    first.write_bytes(b"version one")
    second = tmp_path / "v2.nsp"
    second.write_bytes(b"version two, longer content")

    backend.send_file("SD_INSTALL", "game.nsp", first)
    result = backend.send_file("SD_INSTALL", "game.nsp", second, overwrite=True)
    assert result.status is TransferStatus.COMPLETED
    assert backend.storage_tree("SD_INSTALL").read_file("game.nsp") == b"version two, longer content"


# 8 / 9. Missing storage / missing destination ---------------------------

def test_send_file_missing_storage_raises(source_file):
    src = source_file()
    backend = MockMtpBackend()
    backend.connect()
    with pytest.raises(StorageNotFoundError):
        backend.send_file("SD_CARD", "game.nsp", src)


def test_send_file_missing_destination_parent_raises(source_file):
    src = source_file()
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    with pytest.raises(DestinationNotFoundError):
        backend.send_file("SD_CARD", "atmosphere/contents/0100000000010000/romfs/x.bin", src)


# 10. Transfer error (simulated) ------------------------------------------

def test_send_file_simulated_error_reports_failed_not_success(source_file):
    src = source_file()
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    backend.arm_failure("error", storage="SD_INSTALL", dest_path="game.nsp")
    result = backend.send_file("SD_INSTALL", "game.nsp", src)
    assert result.status is TransferStatus.FAILED
    assert result.error is not None
    assert backend.exists("SD_INSTALL", "game.nsp") is False, "a failed transfer must not leave a file behind"


def test_send_file_corruption_is_detected_and_reported_as_failed(source_file):
    """Even if the backend's internal write path mangles bytes, send_file
    must verify what it actually wrote and refuse to call it a success."""
    src = source_file(content=b"important game data, must not be corrupted")
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    backend.arm_failure("corrupt", storage="SD_INSTALL", dest_path="game.nsp")
    result = backend.send_file("SD_INSTALL", "game.nsp", src)
    assert result.status is TransferStatus.FAILED
    assert backend.exists("SD_INSTALL", "game.nsp") is False


# 11. Disconnect during operation ------------------------------------------

def test_disconnect_during_transfer_reported_correctly(source_file):
    src = source_file()
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    backend.arm_failure("disconnect", storage="SD_CARD")
    result = backend.send_file("SD_CARD", "game.nsp", src)
    assert result.status is TransferStatus.DEVICE_DISCONNECTED
    assert backend.is_connected is False
    # exists() itself requires a connection (correctly, like every other
    # operation) -- inspect the storage directly instead, bypassing that
    # check, to confirm nothing was left behind by the failed transfer.
    assert backend.storage_tree("SD_CARD").exists("game.nsp") is False


def test_operations_after_disconnect_raise():
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    backend.simulate_disconnect()
    with pytest.raises(DeviceDisconnectedError):
        backend.list_storages()


# 12. Operation log ---------------------------------------------------------

def test_operation_log_records_expected_sequence(source_file):
    src = source_file()
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    backend.list_storages()
    backend.ensure_directory("SD_INSTALL", "")
    backend.send_file("SD_INSTALL", "game.nsp", src)
    backend.disconnect()

    ops = [e.operation for e in backend.operation_log]
    assert ops == ["CONNECT", "LIST_STORAGE", "CREATE_DIRECTORY", "SEND_FILE", "DISCONNECT"]


def test_operation_log_is_a_copy_not_a_live_reference():
    backend = MockMtpBackend()
    backend.connect()
    log_snapshot = backend.operation_log
    backend.disconnect()
    assert len(log_snapshot) == 1, "snapshot taken before disconnect() must not grow after it"


# 14. Failure must never look like success (transfer_status specifically) --

def test_get_transfer_status_reflects_true_outcome_not_optimism(source_file):
    src = source_file()
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    backend.arm_failure("error", storage="SD_INSTALL")
    result = backend.send_file("SD_INSTALL", "game.nsp", src)
    looked_up = backend.get_transfer_status(result.operation_id)
    assert looked_up.status is TransferStatus.FAILED
    assert looked_up.status is not TransferStatus.COMPLETED


def test_get_transfer_status_unknown_id_raises():
    backend = MockMtpBackend()
    backend.connect()
    with pytest.raises(InvalidOperationError):
        backend.get_transfer_status("not-a-real-id")


# 15. Partial transfer -------------------------------------------------

def test_partial_transfer_reports_partial_bytes_and_no_file_left_behind(source_file):
    src = source_file(content=b"x" * 1000)
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    backend.arm_failure("partial", storage="SD_CARD")
    result = backend.send_file("SD_CARD", "game.nsp", src)
    assert result.status is TransferStatus.PARTIAL
    assert 0 < result.bytes_sent < result.bytes_total
    assert backend.exists("SD_CARD", "game.nsp") is False


# -- misc path-safety on the mock's own in-memory tree ----------------------

def test_send_file_rejects_traversal_in_dest_path(source_file):
    src = source_file()
    backend = MockMtpBackend()
    backend.add_storage("SD_CARD")
    backend.connect()
    with pytest.raises(InvalidOperationError):
        backend.send_file("SD_CARD", "../../evil.nsp", src)


def test_send_file_missing_source_raises_invalid_operation(tmp_path):
    backend = MockMtpBackend()
    backend.add_storage("SD_INSTALL")
    backend.connect()
    with pytest.raises(InvalidOperationError):
        backend.send_file("SD_INSTALL", "game.nsp", tmp_path / "does_not_exist.nsp")
