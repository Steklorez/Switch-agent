"""Unit tests for switchagent/mtp/windows.py.

COM is never touched here. Two layers are tested separately:
  1. Pure functions (logical_storage_name, split_dest_path,
     _match_device_by_id, verify_transfer_completion) -- exercised
     directly, with scripted/injected inputs.
  2. RealMtpBackend's own logic (device matching, storage resolution,
     path navigation, state transitions, error mapping) -- exercised
     against small duck-typed fake Shell objects that mimic the handful of
     win32com properties/methods this module actually uses (.Name, .Path,
     .IsFolder, .IsFileSystem, .GetFolder, .Items(), .ExtendedProperty(),
     .NewFolder()), monkeypatching RealMtpBackend._shell() to return them.
"""

from __future__ import annotations

import pytest

from switchagent.mtp import windows as mtpw
from switchagent.mtp.base import TransferStatus
from switchagent.mtp.errors import (
    DestinationNotFoundError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    FileAlreadyExistsError,
    InvalidOperationError,
    StorageNotFoundError,
)


# ---------------------------------------------------------------------------
# Pure: logical_storage_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("display_name,expected", [
    ("1: SD Card", "SD_CARD"),
    ("5: SD Card install", "SD_INSTALL"),
    ("5: SD install", "SD_INSTALL"),  # the other real-world variant seen across DBI versions
    ("2: Nand USER", "NAND_USER"),
    ("3: Nand SYSTEM", "NAND_SYSTEM"),
    ("6: NAND install", "NAND_INSTALL"),
    ("7: Saves", "SAVES"),
    ("8: Album", "ALBUM"),
    ("4: Installed games", "INSTALLED_GAMES"),
])
def test_logical_storage_name_matches_known_real_names(display_name, expected):
    assert mtpw.logical_storage_name(display_name) == expected


def test_logical_storage_name_sd_card_does_not_match_sd_card_install():
    """The exact collision this function exists to avoid -- 'SD Card
    install' contains 'SD Card' as a substring, so order/specificity
    matters."""
    assert mtpw.logical_storage_name("1: SD Card") == "SD_CARD"
    assert mtpw.logical_storage_name("5: SD Card install") != "SD_CARD"


def test_logical_storage_name_unrecognized_falls_back_to_generated_name():
    assert mtpw.logical_storage_name("9: Something New") == "SOMETHING_NEW"


def test_logical_storage_name_never_uses_the_leading_index():
    """Two devices can number the same logical storage differently (one
    real console had 7 nodes, another had 8, with different numbering) --
    the leading 'N:' must never be part of identity."""
    assert mtpw.logical_storage_name("1: SD Card") == mtpw.logical_storage_name("3: SD Card")


# ---------------------------------------------------------------------------
# Pure: resolve_storage_name (UI-007 -- manual-override-aware counterpart
# to logical_storage_name() above)
# ---------------------------------------------------------------------------

def test_resolve_storage_name_falls_back_to_auto_when_no_override_given():
    assert mtpw.resolve_storage_name("1: SD Card", None) == "SD_CARD"
    assert mtpw.resolve_storage_name("1: SD Card", {}) == "SD_CARD"


def test_resolve_storage_name_falls_back_to_auto_when_this_name_has_no_entry():
    overrides = {"9: Weird Vendor Name": "SD_INSTALL"}
    assert mtpw.resolve_storage_name("1: SD Card", overrides) == "SD_CARD"


def test_resolve_storage_name_override_wins_for_an_unrecognized_raw_name():
    """The exact real-world case this feature exists for: a raw DBI
    display name logical_storage_name()'s patterns don't recognize, so it
    would otherwise fall back to a useless generated identifier."""
    raw = "9: Weird Vendor Name"
    assert mtpw.logical_storage_name(raw) == "WEIRD_VENDOR_NAME"  # the unhelpful AUTO fallback
    overrides = {raw: "SD_INSTALL"}
    assert mtpw.resolve_storage_name(raw, overrides) == "SD_INSTALL"


def test_resolve_storage_name_override_can_also_correct_a_recognized_name():
    """A user is allowed to override even an already-AUTO-recognized name
    (e.g. if DBI's own naming is misleading on their particular unit) --
    the override always wins, not just for unrecognized names."""
    overrides = {"1: SD Card": "SD_INSTALL"}
    assert mtpw.resolve_storage_name("1: SD Card", overrides) == "SD_INSTALL"


def test_resolve_storage_name_match_is_exact_not_fuzzy():
    overrides = {"1: SD Card": "SD_INSTALL"}
    # A near-miss (different case/whitespace) must NOT match -- exact
    # raw-string equality only, never fuzzy/normalized comparison.
    assert mtpw.resolve_storage_name("1: sd card", overrides) == "SD_CARD"  # falls back to AUTO


# ---------------------------------------------------------------------------
# Pure: split_dest_path
# ---------------------------------------------------------------------------

def test_split_dest_path_flat_filename():
    assert mtpw.split_dest_path("game.nsp") == ("", "game.nsp")


def test_split_dest_path_nested():
    assert mtpw.split_dest_path("atmosphere/contents/ID/romfs/x.bin") == ("atmosphere/contents/ID/romfs", "x.bin")


# ---------------------------------------------------------------------------
# Pure: _match_device_by_id
# ---------------------------------------------------------------------------

class _FakePathOnly:
    def __init__(self, path):
        self.Path = path


def test_match_device_by_id_finds_exact_match():
    candidates = [_FakePathOnly("device-a"), _FakePathOnly("device-b")]
    match = mtpw._match_device_by_id(candidates, "device-b")
    assert match is candidates[1]


def test_match_device_by_id_returns_none_when_absent():
    candidates = [_FakePathOnly("device-a")]
    assert mtpw._match_device_by_id(candidates, "device-x") is None


def test_match_device_by_id_never_picks_by_position_when_multiple_present():
    candidates = [_FakePathOnly("device-a"), _FakePathOnly("device-b"), _FakePathOnly("device-c")]
    # deliberately ask for the LAST one and confirm it's not just "first found"
    assert mtpw._match_device_by_id(candidates, "device-c") is candidates[2]
    assert mtpw._match_device_by_id(candidates, "device-a") is candidates[0]


# ---------------------------------------------------------------------------
# Pure-ish: verify_transfer_completion (fully injected, no real waiting)
# ---------------------------------------------------------------------------

def _scripted_poll(readings):
    it = iter(readings)
    def poll_fn():
        return next(it)
    return poll_fn


def _fake_clock():
    """Returns (clock_fn, sleep_fn) that advance a shared in-memory clock --
    so tests can run a scripted number of poll iterations deterministically
    without a real deadline racing against a no-op sleep (real time.monotonic
    barely moves between instant scripted calls, which would either exhaust
    the readings list too early or never hit the intended timeout)."""
    state = {"t": 0.0}
    def clock_fn():
        return state["t"]
    def sleep_fn(seconds):
        state["t"] += seconds
    return clock_fn, sleep_fn


def test_verify_completion_succeeds_when_size_stabilizes_at_expected():
    readings = [
        mtpw.PollReading(True, False, None),
        mtpw.PollReading(True, True, 4096),
        mtpw.PollReading(True, True, 4096),
        mtpw.PollReading(True, True, 4096),
    ]
    clock_fn, sleep_fn = _fake_clock()
    status, size, error = mtpw.verify_transfer_completion(
        _scripted_poll(readings), expected_size=4096,
        stable_reads_required=3, sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert status is TransferStatus.COMPLETED
    assert size == 4096
    assert error is None


def test_verify_completion_device_disconnected_mid_poll():
    readings = [
        mtpw.PollReading(True, True, 2048),
        mtpw.PollReading(False, False, None),
    ]
    clock_fn, sleep_fn = _fake_clock()
    status, size, error = mtpw.verify_transfer_completion(
        _scripted_poll(readings), expected_size=4096, sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert status is TransferStatus.DEVICE_DISCONNECTED
    assert "disconnected" in error


def test_verify_completion_size_mismatch_never_reports_completed():
    readings = [mtpw.PollReading(True, True, 2048)] * 5
    clock_fn, sleep_fn = _fake_clock()
    status, size, error = mtpw.verify_transfer_completion(
        _scripted_poll(readings), expected_size=4096,
        stable_reads_required=3, sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert status is TransferStatus.FAILED
    assert status is not TransferStatus.COMPLETED
    assert "2048" in error and "4096" in error


def test_verify_completion_times_out_never_observed_is_unverified_not_failed():
    """Real report, 2026-09-13: two SD_CARD mod files (one 126MB, one
    130KB) genuinely copied successfully within seconds on the console,
    yet were never once observed by poll_fn() within the 60s window
    (consistent with a just-created MTP folder's listing not reflecting a
    file added moments later). The transport call itself never raised, so
    this must be an honest UNVERIFIED -- never a false FAILED for
    something we simply never saw, exactly like SD_INSTALL's own
    unverifiable-virtual-node handling."""
    fake_time = {"t": 0.0}
    def clock_fn():
        return fake_time["t"]
    def sleep_fn(seconds):
        fake_time["t"] += seconds

    status, size, error = mtpw.verify_transfer_completion(
        lambda: mtpw.PollReading(True, False, None),  # file never once observed
        expected_size=4096, timeout_seconds=10.0, poll_interval_seconds=2.0,
        sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert status is TransferStatus.UNVERIFIED
    assert status is not TransferStatus.FAILED
    assert "never observed" in error


def test_verify_completion_times_out_after_observing_wrong_data_is_failed():
    """Distinct from the "never observed" case above: the file WAS seen,
    concretely, just never at a stable, matching size -- this stays a real
    FAILED, not a shrug."""
    fake_time = {"t": 0.0}
    def clock_fn():
        return fake_time["t"]
    def sleep_fn(seconds):
        fake_time["t"] += seconds
    # Flickers between two sizes forever -- never 3 consecutive matching
    # reads, but genuinely observed every time.
    readings = iter(
        mtpw.PollReading(True, True, 100) if i % 2 == 0 else mtpw.PollReading(True, True, 200)
        for i in range(100)
    )
    status, size, error = mtpw.verify_transfer_completion(
        lambda: next(readings),
        expected_size=4096, timeout_seconds=10.0, poll_interval_seconds=2.0,
        sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert status is TransferStatus.FAILED
    assert "timed out" in error


def test_verify_completion_flicker_resets_stability_count():
    """File appears, disappears, reappears -- must not count the earlier
    stable reads towards the final stabilization."""
    readings = [
        mtpw.PollReading(True, True, 4096),
        mtpw.PollReading(True, True, 4096),
        mtpw.PollReading(True, False, None),  # flicker -- resets
        mtpw.PollReading(True, True, 4096),
        mtpw.PollReading(True, True, 4096),
        mtpw.PollReading(True, True, 4096),
    ]
    status, size, error = mtpw.verify_transfer_completion(
        _scripted_poll(readings), expected_size=4096,
        stable_reads_required=3, sleep_fn=lambda s: None,
    )
    assert status is TransferStatus.COMPLETED


# ---------------------------------------------------------------------------
# RealMtpBackend against duck-typed fake Shell objects
# ---------------------------------------------------------------------------

class FakeItem:
    def __init__(self, name, path=None, is_folder=True, is_file_system=False, size=None, free=None, total=None):
        self.Name = name
        self.Path = path if path is not None else name
        self.IsFolder = is_folder
        self.IsFileSystem = is_file_system
        self.children: list[FakeItem] = []
        self._props = {"System.Size": size, "System.FreeSpace": free, "System.Capacity": total}

    @property
    def GetFolder(self):
        return FakeFolder(self)

    def ExtendedProperty(self, key):
        return self._props.get(key)


class FakeFolder:
    def __init__(self, item: FakeItem):
        self._item = item

    def Items(self):
        return list(self._item.children)

    def NewFolder(self, name):
        self._item.children.append(FakeItem(name, path=f"{self._item.Path}/{name}"))


class FakeThisPC:
    def __init__(self, items):
        self.items = items

    def Items(self):
        return list(self.items)


class FakeShell:
    def __init__(self, this_pc_items):
        self._this_pc = FakeThisPC(this_pc_items)

    def NameSpace(self, ns_id):
        return self._this_pc


def _install_fake_shell(monkeypatch, devices):
    monkeypatch.setattr(mtpw.RealMtpBackend, "_shell", staticmethod(lambda: FakeShell(devices)))


def _make_switch(device_id="dev-a", storages=None):
    dev = FakeItem("Switch", path=device_id, is_folder=True, is_file_system=False)
    for s in (storages or []):
        dev.children.append(s)
    return dev


# -- connect / disconnect / is_connected -------------------------------

def test_connect_finds_matching_device(monkeypatch):
    dev = _make_switch("dev-a")
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    info = backend.connect()
    assert info.device_id == "dev-a"
    assert info.connected is True


def test_connect_raises_device_not_found_when_absent(monkeypatch):
    _install_fake_shell(monkeypatch, [])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    with pytest.raises(DeviceNotFoundError):
        backend.connect()


def test_connect_never_picks_the_wrong_device_when_multiple_present(monkeypatch):
    dev_a = _make_switch("dev-a")
    dev_b = _make_switch("dev-b")
    _install_fake_shell(monkeypatch, [dev_a, dev_b])
    backend = mtpw.RealMtpBackend(device_id="dev-b")
    info = backend.connect()
    assert info.device_id == "dev-b"
    assert backend._device_item is dev_b


def test_is_connected_false_before_connect(monkeypatch):
    _install_fake_shell(monkeypatch, [_make_switch("dev-a")])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    assert backend.is_connected is False


def test_is_connected_true_after_connect(monkeypatch):
    _install_fake_shell(monkeypatch, [_make_switch("dev-a")])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    assert backend.is_connected is True


def test_is_connected_false_after_device_disappears_without_disconnect(monkeypatch):
    """The real-presence-check requirement: is_connected must not trust a
    stale internal flag once the device is physically gone."""
    dev = _make_switch("dev-a")
    shell = FakeShell([dev])
    monkeypatch.setattr(mtpw.RealMtpBackend, "_shell", staticmethod(lambda: shell))
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    assert backend.is_connected is True

    shell._this_pc.items.remove(dev)  # simulate physical unplug
    assert backend.is_connected is False


def test_disconnect_clears_state_and_is_connected_becomes_false(monkeypatch):
    _install_fake_shell(monkeypatch, [_make_switch("dev-a")])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    backend.disconnect()
    assert backend.is_connected is False
    assert backend._device_item is None


def test_operations_before_connect_raise_device_disconnected(monkeypatch):
    _install_fake_shell(monkeypatch, [_make_switch("dev-a")])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    with pytest.raises(DeviceDisconnectedError):
        backend.list_storages()


# -- storage resolution ---------------------------------------------------

def test_list_storages_maps_display_names_to_logical_names(monkeypatch):
    sd_card = FakeItem("1: SD Card", free=100, total=200)
    sd_install = FakeItem("5: SD Card install", free=100, total=200)
    dev = _make_switch("dev-a", storages=[sd_card, sd_install])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()

    names = {s.name for s in backend.list_storages()}
    assert names == {"SD_CARD", "SD_INSTALL"}


def test_get_storage_not_found_raises(monkeypatch):
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    with pytest.raises(StorageNotFoundError):
        backend.get_storage("SD_INSTALL")


def test_get_storage_for_one_device_never_returns_another_devices_storage(monkeypatch):
    """(device A, SD Card install) != (device B, SD Card install) --
    resolution always happens within the connected instance's own device
    subtree, never falls back to a different device."""
    dev_a = _make_switch("dev-a", storages=[FakeItem("1: SD Card", total=111)])
    dev_b = _make_switch("dev-b", storages=[FakeItem("1: SD Card", total=222)])
    _install_fake_shell(monkeypatch, [dev_a, dev_b])

    backend_a = mtpw.RealMtpBackend(device_id="dev-a")
    backend_a.connect()
    assert backend_a.get_storage("SD_CARD").total_bytes == 111


# -- InstalledApplications.csv (DBI's "Installed games" MTP node,
# real-hardware-confirmed 2026-09-15) -------------------------------------

_REAL_CSV_SAMPLE = (
    '0x03DB12780BD84000,0,"Homebrew menu"\n'
    '0x01004A4010FEA000,131072,"Bayonetta 3"\n'
    '0x010089A0197E4000,655360,"Vampire Survivors"\n'
    '0x010089A0197E5001,327680,"Vampire Survivors DLC 1"\n'
    '0x0100026016320000,458752,"River City Girls 2"\n'
    '0x0100026016321001,0,"River City Girls 2 DLC 1"\n'
)


def test_parse_installed_applications_csv_recovers_base_ids():
    """Real sample (2026-09-15 InstalledApplications.csv find): a plain
    base game row (Bayonetta 3), and two base+DLC pairs (Vampire
    Survivors, River City Girls 2) whose DLC row reduces to the same base
    id as its own base row -- the result set has one entry per game, not
    one per row. "Homebrew menu"'s own id still parses (it's a valid
    16-hex-char shape) and is included -- matching against the library is
    the caller's job, and an id nothing in the library has simply never
    matches."""
    base_ids = mtpw.parse_installed_applications_csv(_REAL_CSV_SAMPLE)
    assert base_ids == {
        "03DB12780BD84000", "01004A4010FEA000", "010089A0197E4000", "0100026016320000",
    }


def test_parse_installed_applications_csv_ignores_malformed_rows():
    assert mtpw.parse_installed_applications_csv("not,a,title,id\n\n0xZZZ,0,\"bad\"\n") == set()


def test_list_installed_title_ids_returns_none_when_node_absent(monkeypatch):
    """Not every console/DBI session exposes the node at all -- None (not
    an empty set) says "no information", so callers never mistake it for
    "nothing is installed"."""
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    assert backend.list_installed_title_ids() is None


def test_list_installed_title_ids_returns_none_when_csv_file_absent(monkeypatch):
    installed = FakeItem("4: Installed games")
    installed.children = [FakeItem("River City Girls 2", is_folder=True)]
    dev = _make_switch("dev-a", storages=[installed])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    assert backend.list_installed_title_ids() is None


def test_list_installed_title_ids_downloads_and_parses_the_csv(monkeypatch, tmp_path):
    """_download_file() is real COM (untested here, same as
    _copy_via_ifileoperation()'s own upload direction) -- faked out to
    just write the real sample bytes to the requested local path, so this
    test exercises the actual find-node -> find-file -> parse plumbing."""
    csv_item = FakeItem("InstalledApplications.csv", is_folder=False)
    installed = FakeItem("4: Installed games")
    installed.children = [csv_item]
    dev = _make_switch("dev-a", storages=[installed])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()

    def fake_download(source_item, dest_dir):
        (dest_dir / source_item.Name).write_text(_REAL_CSV_SAMPLE, encoding="utf-8")

    monkeypatch.setattr(backend, "_download_file", fake_download)
    assert backend.list_installed_title_ids() == {
        "03DB12780BD84000", "01004A4010FEA000", "010089A0197E4000", "0100026016320000",
    }


# -- exists / ensure_directory --------------------------------------------

def test_exists_true_for_present_file(monkeypatch):
    sd = FakeItem("1: SD Card")
    sd.children.append(FakeItem("boot.ini", is_folder=False))
    dev = _make_switch("dev-a", storages=[sd])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    assert backend.exists("SD_CARD", "boot.ini") is True


def test_exists_false_for_missing_file(monkeypatch):
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    assert backend.exists("SD_CARD", "nope.bin") is False


def test_exists_false_for_missing_parent_directory(monkeypatch):
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    assert backend.exists("SD_CARD", "atmosphere/contents/ID/x.bin") is False


def test_ensure_directory_creates_missing_segments(monkeypatch):
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    backend.ensure_directory("SD_CARD", "atmosphere/contents/ID")
    assert backend.exists("SD_CARD", "atmosphere") is True
    assert backend.exists("SD_CARD", "atmosphere/contents/ID") is True


def test_ensure_directory_is_idempotent(monkeypatch):
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    backend.ensure_directory("SD_CARD", "atmosphere")
    backend.ensure_directory("SD_CARD", "atmosphere")  # must not raise or duplicate
    sd = backend._get_storage_item("SD_CARD")
    assert len([c for c in sd.GetFolder.Items() if c.Name == "atmosphere"]) == 1


def test_ensure_directory_raises_if_path_is_a_file(monkeypatch):
    sd = FakeItem("1: SD Card")
    sd.children.append(FakeItem("atmosphere", is_folder=False))  # a FILE named "atmosphere"
    dev = _make_switch("dev-a", storages=[sd])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    with pytest.raises(DestinationNotFoundError):
        backend.ensure_directory("SD_CARD", "atmosphere/contents")


# -- send_file guard logic (COM parts monkeypatched out) -------------------

def test_send_file_raises_if_source_missing(monkeypatch, tmp_path):
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    with pytest.raises(InvalidOperationError):
        backend.send_file("SD_CARD", "game.nsp", tmp_path / "does_not_exist.nsp")


def test_send_file_raises_file_already_exists_without_overwrite(monkeypatch, tmp_path):
    sd = FakeItem("1: SD Card")
    sd.children.append(FakeItem("game.nsp", is_folder=False, size=10))
    dev = _make_switch("dev-a", storages=[sd])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()

    src = tmp_path / "game.nsp"
    src.write_bytes(b"x" * 10)
    with pytest.raises(FileAlreadyExistsError):
        backend.send_file("SD_CARD", "game.nsp", src)


def test_send_file_raises_destination_not_found_if_parent_missing(monkeypatch, tmp_path):
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()

    src = tmp_path / "x.bin"
    src.write_bytes(b"x" * 4)
    with pytest.raises(DestinationNotFoundError):
        backend.send_file("SD_CARD", "atmosphere/contents/ID/x.bin", src)


def test_send_file_completed_result_is_built_and_retrievable(monkeypatch, tmp_path):
    """COM-touching internals (_copy_via_ifileoperation, _verify_after_copy)
    are monkeypatched out here -- this test is about send_file's own
    bookkeeping (TransferResult construction, get_transfer_status storage),
    not about IFileOperation itself (covered by the real hardware smoke
    test, see docs/STAGE5B-REAL-MTP.md)."""
    dev = _make_switch("dev-a", storages=[FakeItem("1: SD Card")])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()

    src = tmp_path / "x.bin"
    src.write_bytes(b"x" * 4096)

    monkeypatch.setattr(backend, "_copy_via_ifileoperation", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_verify_after_copy", lambda *a, **k: (TransferStatus.COMPLETED, 4096, None))

    result = backend.send_file("SD_CARD", "x.bin", src)
    assert result.status is TransferStatus.COMPLETED
    assert result.bytes_sent == 4096
    assert result.bytes_total == 4096

    fetched = backend.get_transfer_status(result.operation_id)
    assert fetched == result


def test_get_transfer_status_unknown_id_raises(monkeypatch):
    _install_fake_shell(monkeypatch, [_make_switch("dev-a")])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()
    with pytest.raises(InvalidOperationError):
        backend.get_transfer_status("not-a-real-id")


# -- construction guard -----------------------------------------------------

def test_constructor_requires_device_id():
    with pytest.raises(ValueError):
        mtpw.RealMtpBackend(device_id="")


# -- mask_device_id / device_fingerprint ------------------------------------

def test_mask_device_id_redacts_serial():
    path = "usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    masked = mtpw.mask_device_id(path)
    assert "xtj10229424075" not in masked
    assert "vid_057e&pid_201d" in masked


def test_device_fingerprint_deterministic_and_short():
    fp1 = mtpw.device_fingerprint("some-device-id")
    fp2 = mtpw.device_fingerprint("some-device-id")
    assert fp1 == fp2
    assert len(fp1) == 16


# ---------------------------------------------------------------------------
# SD_INSTALL semantics fix -- regression coverage.
#
# Real-hardware finding this section guards against re-breaking (see
# docs/STAGE5B-REAL-MTP.md, "Install smoke-test"): a transfer to
# "5: SD Card install" that was LATER physically confirmed (on the
# console's own screen) to have installed correctly reported
# System.Size == 0 for its entire observable lifetime via MTP/Shell. The
# original verify_transfer_completion()-based check reported this as
# FAILED -- a proven false negative. verify_install_transport() and
# send_file()'s storage branch exist specifically to never repeat that.
# ---------------------------------------------------------------------------

def test_verify_install_transport_reports_unverified_for_the_historical_zero_byte_case():
    """Replays the exact An English Haunting shape: destination appears
    immediately, with size 0, and stays that way -- must be UNVERIFIED, not
    FAILED, and definitely not COMPLETED."""
    readings = [
        mtpw.PollReading(True, True, 0),
        mtpw.PollReading(True, True, 0),
        mtpw.PollReading(True, True, 0),
    ]
    status, size, error = mtpw.verify_install_transport(
        _scripted_poll(readings), sleep_fn=lambda s: None,
    )
    assert status is mtpw.TransferStatus.UNVERIFIED
    assert status is not mtpw.TransferStatus.FAILED
    assert status is not mtpw.TransferStatus.COMPLETED
    assert size == 0
    assert "cannot be verified" in error


def test_verify_install_transport_never_uses_size_as_a_pass_fail_signal():
    """Same presence-based outcome regardless of what size is observed --
    proves size plays no role at all in this function's verdict, unlike
    verify_transfer_completion()."""
    readings_nonzero = [mtpw.PollReading(True, True, 123456)] * 2
    status, _, _ = mtpw.verify_install_transport(_scripted_poll(readings_nonzero), sleep_fn=lambda s: None)
    assert status is mtpw.TransferStatus.UNVERIFIED

    readings_changing = [mtpw.PollReading(True, True, 10), mtpw.PollReading(True, True, 99999)]
    status2, _, _ = mtpw.verify_install_transport(_scripted_poll(readings_changing), sleep_fn=lambda s: None)
    assert status2 is mtpw.TransferStatus.UNVERIFIED


def test_verify_install_transport_reports_unverified_not_failed_if_name_never_appears():
    """Real-hardware finding (2026-09-12): a name that never appears at all
    within the polling budget does NOT reliably prove transport failure --
    reproduced twice on real hardware with near-identical timing, a genuine,
    physically-confirmed-successful install (DBI: 15s, user-confirmed on
    the console screen both times) reported this exact 'name never
    appeared' outcome, because PerformOperations()'s own blocking duration
    was close enough to DBI's whole receive+install cycle that the virtual
    placeholder this function polls for may have already been created AND
    removed by DBI entirely before this function's first poll ever ran --
    no timeout or poll interval can reliably catch a window that narrow.
    This function is only ever reached after PerformOperations() has
    already returned without an aborted-operations signal (see
    send_file()'s caller), so 'never observed' here means, at most, 'we
    could not confirm it', never 'the transport call itself failed' -- the
    same class of false-negative this function's OWN docstring already
    describes for System.Size == 0, now also true for presence timeouts.
    Was previously (incorrectly) FAILED -- fixed after this exact real
    incident, confirmed correct on a third real install using this fixed
    code (job reported DONE_UNVERIFIED, matching the console's own
    success)."""
    clock_fn, sleep_fn = _fake_clock()
    status, size, error = mtpw.verify_install_transport(
        lambda: mtpw.PollReading(True, False, None),
        timeout_seconds=6.0, poll_interval_seconds=2.0, sleep_fn=sleep_fn, clock_fn=clock_fn,
    )
    assert status is mtpw.TransferStatus.UNVERIFIED
    assert status is not mtpw.TransferStatus.FAILED
    assert "never observed" in error
    assert "cannot be verified" in error


def test_verify_install_transport_still_reports_device_disconnected_not_unverified():
    """The fix above narrows FAILED, not DEVICE_DISCONNECTED -- a genuine
    device disappearance mid-poll is a distinct, still-legitimate signal
    (queue_worker.py maps it to INTERRUPTED, never DONE_UNVERIFIED) and
    must not be swallowed into the new, more lenient timeout handling."""
    status, _, error = mtpw.verify_install_transport(
        lambda: mtpw.PollReading(False, False, None),
        timeout_seconds=6.0, poll_interval_seconds=2.0, sleep_fn=lambda s: None,
    )
    assert status is mtpw.TransferStatus.DEVICE_DISCONNECTED
    assert status is not mtpw.TransferStatus.UNVERIFIED
    assert "disconnected" in error


def test_verify_install_transport_unverified_message_distinguishes_flicker_from_never_seen():
    """Not load-bearing for status (both cases are UNVERIFIED) but the
    message should stay honest about what was actually observed -- useful
    for anyone reading job_log/history later, matching this project's
    'never overclaim, never underclaim' convention for free text too."""
    clock_fn1, sleep_fn1 = _fake_clock()
    _, _, never_seen_error = mtpw.verify_install_transport(
        lambda: mtpw.PollReading(True, False, None),
        timeout_seconds=4.0, poll_interval_seconds=2.0, sleep_fn=sleep_fn1, clock_fn=clock_fn1,
    )
    assert "name never observed" in never_seen_error

    # Seen once, then gone for good (a single flicker, never restabilizes)
    # -- distinct wording from "never observed at all". timeout=4s /
    # interval=2s means exactly 2 polls happen, matching this 2-item script.
    readings = [mtpw.PollReading(True, True, 0), mtpw.PollReading(True, False, None)]
    clock_fn2, sleep_fn2 = _fake_clock()
    _, _, flickered_error = mtpw.verify_install_transport(
        _scripted_poll(readings),
        timeout_seconds=4.0, poll_interval_seconds=2.0, sleep_fn=sleep_fn2, clock_fn=clock_fn2,
    )
    assert "observed but not stably present" in flickered_error


def test_verify_install_transport_device_disconnected_mid_poll():
    readings = [
        mtpw.PollReading(True, True, 0),
        mtpw.PollReading(False, False, None),
    ]
    status, _, error = mtpw.verify_install_transport(_scripted_poll(readings), sleep_fn=lambda s: None)
    assert status is mtpw.TransferStatus.DEVICE_DISCONNECTED
    assert "disconnected" in error


def test_verify_install_transport_flicker_resets_presence_count():
    readings = [
        mtpw.PollReading(True, True, 0),
        mtpw.PollReading(True, False, None),  # flicker -- resets
        mtpw.PollReading(True, True, 0),
        mtpw.PollReading(True, True, 0),
    ]
    status, _, _ = mtpw.verify_install_transport(
        _scripted_poll(readings), presence_reads_required=2, sleep_fn=lambda s: None,
    )
    assert status is mtpw.TransferStatus.UNVERIFIED


def test_size_verifiable_storages_excludes_install_nodes():
    assert "SD_CARD" in mtpw.SIZE_VERIFIABLE_STORAGES
    assert "SD_INSTALL" not in mtpw.SIZE_VERIFIABLE_STORAGES
    assert "NAND_INSTALL" not in mtpw.SIZE_VERIFIABLE_STORAGES


# -- send_file end-to-end: storage decides which verification path runs ----

def _prep_send_file_backend(monkeypatch, tmp_path, storage_display_name):
    """Common setup for the two tests below: a connected backend and a
    storage with the given display name. Callers still need to monkeypatch
    _copy_via_ifileoperation themselves to simulate "PerformOperations()
    returned with no exception and no abort" (result=None) plus whatever
    destination state they want send_file()'s post-copy verification to
    observe."""
    storage_item = FakeItem(storage_display_name)
    dev = _make_switch("dev-a", storages=[storage_item])
    _install_fake_shell(monkeypatch, [dev])
    backend = mtpw.RealMtpBackend(device_id="dev-a")
    backend.connect()

    src = tmp_path / "An English Haunting.nsz"
    src.write_bytes(b"x" * 4096)
    return backend, storage_item, src


def test_send_file_sd_card_zero_byte_destination_still_fails(monkeypatch, tmp_path):
    """Regression guard the OTHER direction: this fix must not weaken
    SD_CARD's proven-correct size verification. A 0-byte destination for a
    real filesystem-like storage is still, correctly, FAILED."""
    backend, storage_item, src = _prep_send_file_backend(monkeypatch, tmp_path, "1: SD Card")

    def fake_copy(*a, **k):
        storage_item.children.append(FakeItem(src.name, is_folder=False, size=0))
        return None
    monkeypatch.setattr(backend, "_copy_via_ifileoperation", fake_copy)

    result = backend.send_file("SD_CARD", src.name, src)
    assert result.status is TransferStatus.FAILED
    assert result.status is not TransferStatus.UNVERIFIED


def test_send_file_sd_install_zero_byte_destination_is_unverified_not_failed(monkeypatch, tmp_path):
    """THE regression test for the bug itself: the exact real-hardware
    shape (destination appears under SD Card install, size reads 0) must
    now resolve to UNVERIFIED for SD_INSTALL, never FAILED and never
    COMPLETED -- see docs/STAGE5B-REAL-MTP.md."""
    backend, storage_item, src = _prep_send_file_backend(monkeypatch, tmp_path, "5: SD Card install")

    def fake_copy(*a, **k):
        storage_item.children.append(FakeItem(src.name, is_folder=False, size=0))
        return None
    monkeypatch.setattr(backend, "_copy_via_ifileoperation", fake_copy)

    result = backend.send_file("SD_INSTALL", src.name, src)
    assert result.status is TransferStatus.UNVERIFIED
    assert result.status is not TransferStatus.FAILED
    assert result.status is not TransferStatus.COMPLETED
    assert result.bytes_total == 4096  # the known local source size, unaffected by the ambiguity
