"""emuiibo and virtual amiibo (switchagent/emuiibo.py and everything built
on it).

The layouts below are the real ones, file for file (2026-09-25):

  Animal Crossing New Horizons [NSP]/Amiibo JSON for Emuiibo/
    ALL amiibo (flag_json).7z   ALL amiibo (flag_json)/<Series>/<Name>/{amiibo.json, amiibo.flag}
                                -- 766 of them, in 25 series folders, one of them nested
                                   (Animal Crossing/Spork/Crackle)
    emuiibo-v0.6.3.zip          SdOut/atmosphere/contents/0100000000000352/{exefs.nsp, flags/boot2.flag,
                                toolbox.json}, SdOut/switch/.overlays/emuiibo.ovl
    emutool-v0.6.3.zip          Release/emutool.exe + its DLLs -- a PC app

emuiibo 1.1.3 (the current release) ships the same SdOut/ plus
emuiibo/overlay/lang/*.json.

Before this, Library showed all three as NEEDS REVIEW -- the emuiibo release
as a "game package" (its sysmodule's exefs.nsp), the amiibo pack and the PC
tool as unknown archives -- and nothing could put an amiibo on a console.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, emuiibo, extractor, known_folders, manifest as manifest_mod
from switchagent import preview, queue_worker, scanner
from switchagent.model import ContentType
from switchagent.mtp import MockMtpBackend
from switchagent.web import amiibo_views, services
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context, mock_overlay_bytes
from switchagent.web.emuiibo_service import EmuiiboService, RemovalRefused

from .conftest import build_7z, build_zip

PACK = "ALL amiibo (flag_json)"


def amiibo_json(name, gcid=13058, variant=0, ftype=1, model=774, series=5, uuid=(91, 187, 15, 225, 251, 84, 216, 0, 0, 0),
                **extra) -> bytes:
    """An amiibo.json exactly as the real pack has it (Animal Crossing/Admiral
    by default): no use_random_uuid -- 0.6-era emutool did not write one."""
    doc = {
        "name": name, "write_counter": 0, "version": 0, "mii_charinfo_file": "mii-charinfo.bin",
        "first_write_date": {"y": 2021, "m": 11, "d": 5}, "last_write_date": {"y": 2021, "m": 11, "d": 5},
        "id": {"game_character_id": gcid, "character_variant": variant, "figure_type": ftype,
               "series": series, "model_number": model},
        "uuid": list(uuid) if uuid is not None else None,
        **extra,
    }
    if uuid is None:
        del doc["uuid"]
    return json.dumps(doc, indent=2).encode()


def pack_entries(prefix=f"{PACK}/") -> dict[str, bytes]:
    return {
        f"{prefix}Animal Crossing/Admiral/amiibo.json": amiibo_json("Admiral"),
        f"{prefix}Animal Crossing/Admiral/amiibo.flag": b"",
        f"{prefix}Animal Crossing/Isabelle/amiibo.json": amiibo_json("Isabelle", gcid=0x0180, uuid=(1,) * 7 + (0,) * 3),
        f"{prefix}Animal Crossing/Isabelle/amiibo.flag": b"",
        f"{prefix}Animal Crossing/Spork/Crackle/amiibo.json": amiibo_json("Spork/Crackle", gcid=0x0181),
        f"{prefix}Animal Crossing/Spork/Crackle/amiibo.flag": b"",
        f"{prefix}8-bit Mario/8-Bit Mario Classic Color/amiibo.json": amiibo_json("8-Bit Mario Classic Color", gcid=0),
        f"{prefix}8-bit Mario/8-Bit Mario Classic Color/amiibo.flag": b"",
    }


def release_entries(version="0.6.3", *, lang=False) -> dict[str, bytes]:
    entries = {
        "SdOut/atmosphere/contents/0100000000000352/exefs.nsp": b"PFS0 sysmodule",
        "SdOut/atmosphere/contents/0100000000000352/flags/boot2.flag": b"",
        "SdOut/atmosphere/contents/0100000000000352/toolbox.json": b'{"name": "emuiibo"}',
        "SdOut/switch/.overlays/emuiibo.ovl": mock_overlay_bytes(version),
    }
    if lang:
        entries["SdOut/emuiibo/overlay/lang/en.json"] = b"{}"
        entries["SdOut/emuiibo/overlay/lang/ru.json"] = b"{}"
    return entries


def files_of(entries: dict[str, bytes]) -> list[tuple[str, int]]:
    return [(name, len(data)) for name, data in entries.items()]


@pytest.fixture(autouse=True)
def _no_stability_wait(monkeypatch):
    monkeypatch.setattr(scanner, "is_file_stable", lambda *_a, **_k: True)


# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------

def test_the_real_packs_amiibo_json_parses_and_its_id_reads_as_amiiboapi_writes_it():
    info = emuiibo.parse_amiibo_json(amiibo_json("Admiral"))
    assert info.name == "Admiral"
    assert info.use_random_uuid is None  # emuiibo adds it on first load
    # AmiiboAPI's Admiral card: head 02330001, tail 03060502 -- emuiibo keeps
    # game_character_id byte-swapped (13058 == 0x3302).
    assert info.id.hex == "0233000103060502"
    assert info.uuid_hex == "5bbb0fe1fb54d8000000"


@pytest.mark.parametrize("broken", [
    b"not json",
    b"[]",
    json.dumps({"name": "x"}).encode(),  # no id, dates, version...
    amiibo_json("x").replace(b'"version": 0', b'"version": "zero"'),
    amiibo_json("x").replace(b'"mii_charinfo_file": "mii-charinfo.bin"', b'"mii_charinfo_file": 7'),
])
def test_what_emuiibo_itself_would_refuse_is_not_an_amiibo(broken):
    assert emuiibo.parse_amiibo_json(broken) is None


def test_a_missing_uuid_is_fine_and_does_not_make_two_amiibo_different():
    without = emuiibo.parse_amiibo_json(amiibo_json("Admiral", uuid=None))
    assert without is not None and without.uuid is None
    # emuiibo gives it a random UUID on first load; that is still the same amiibo.
    with_random = emuiibo.parse_amiibo_json(amiibo_json("Admiral", uuid=(9,) * 7 + (0,) * 3))
    assert without.same_amiibo(with_random)
    other_figure = emuiibo.parse_amiibo_json(amiibo_json("Admiral", gcid=1))
    assert not with_random.same_amiibo(other_figure)


def test_the_overlay_says_its_own_version():
    assert emuiibo.overlay_version(mock_overlay_bytes("1.1.3")) == "1.1.3"
    assert emuiibo.overlay_version(b"not an nro at all") is None
    assert emuiibo.is_outdated("0.6.3") and not emuiibo.is_outdated("1.1.3") and not emuiibo.is_outdated(None)


# ---------------------------------------------------------------------------
# A release: recognised by its sysmodule, copied by what emuiibo is made of
# ---------------------------------------------------------------------------

def test_a_release_is_its_sysmodule_overlay_and_overlay_files_nothing_else():
    entries = release_entries("1.1.3", lang=True)
    entries["SdOut/README.md"] = b"readme"
    entries["SdOut/switch/other-homebrew.nro"] = b"not emuiibo's"
    plan = emuiibo.plan_release(files_of(entries))
    assert plan.root == "SdOut"
    assert sorted(dest for _src, dest, _size in plan.files) == [
        "atmosphere/contents/0100000000000352/exefs.nsp",
        "atmosphere/contents/0100000000000352/flags/boot2.flag",
        "atmosphere/contents/0100000000000352/toolbox.json",
        "emuiibo/overlay/lang/en.json",
        "emuiibo/overlay/lang/ru.json",
        "switch/.overlays/emuiibo.ovl",
    ]


def test_another_sysmodule_is_not_emuiibo():
    assert emuiibo.plan_release(files_of({"atmosphere/contents/420000000007E51A/exefs.nsp": b"x"})) is None


def test_a_sysmodules_exefs_nsp_is_not_a_package_to_install():
    from switchagent.model import ArchiveEntry

    cls = extractor.classify_entries([
        ArchiveEntry(name="atmosphere/contents/420000000007E51A/exefs.nsp", size=1, is_dir=False, is_symlink=False),
        ArchiveEntry(name="atmosphere/contents/420000000007E51A/flags/boot2.flag", size=0, is_dir=False,
                     is_symlink=False),
    ])
    assert cls.content_type is ContentType.ATMOSPHERE_MOD
    assert cls.package_entries == []


# ---------------------------------------------------------------------------
# A collection: every amiibo, and where it goes
# ---------------------------------------------------------------------------

def test_the_packs_own_wrapper_folder_is_dropped_and_its_series_are_kept():
    collection = emuiibo.find_collection(files_of(pack_entries()), wrapper_name=PACK)
    assert [a.dest for a in collection.amiibo] == [
        "8-bit Mario/8-Bit Mario Classic Color",
        "Animal Crossing/Admiral",
        "Animal Crossing/Isabelle",
        "Animal Crossing/Spork/Crackle",   # a name with "/" in it stays a sub-folder, as emuiibo shows it
    ]
    assert all(a.enabled and a.kind == "virtual" for a in collection.amiibo)
    assert collection.summary()["groups"] == {"8-bit Mario": 1, "Animal Crossing": 3}
    admiral = next(a for a in collection.amiibo if a.label == "Admiral")
    assert admiral.destinations() == [
        (f"{PACK}/Animal Crossing/Admiral/amiibo.flag", "emuiibo/amiibo/Animal Crossing/Admiral/amiibo.flag", 0),
        (f"{PACK}/Animal Crossing/Admiral/amiibo.json", "emuiibo/amiibo/Animal Crossing/Admiral/amiibo.json",
         len(amiibo_json("Admiral"))),
    ]


def test_a_single_series_folder_in_a_differently_named_archive_is_a_group_not_a_wrapper():
    entries = {"Animal Crossing/Admiral/amiibo.json": amiibo_json("Admiral"),
               "Animal Crossing/Admiral/amiibo.flag": b""}
    collection = emuiibo.find_collection(files_of(entries), wrapper_name="ac amiibo")
    assert [a.dest for a in collection.amiibo] == ["Animal Crossing/Admiral"]


def test_a_source_that_already_spells_the_sd_layout_keeps_what_is_below_it():
    entries = {"SD/emuiibo/amiibo/SSBU/Mario/amiibo.json": amiibo_json("Mario"),
               "SD/emuiibo/amiibo/SSBU/Mario/amiibo.flag": b"", "SD/readme.txt": b"hi"}
    collection = emuiibo.find_collection(files_of(entries))
    assert [a.dest for a in collection.amiibo] == ["SSBU/Mario"]


def test_an_amiibo_emuiibo_already_converted_keeps_its_old_files_inside_it():
    entries = {"Mario/amiibo.json": amiibo_json("Mario"), "Mario/amiibo.flag": b"",
               "Mario/v2/amiibo.json": b"{}", "Mario/v2/amiibo.bin": b"\0" * 540}
    collection = emuiibo.find_collection(files_of(entries))
    assert [a.dest for a in collection.amiibo] == ["Mario"]
    assert len(collection.amiibo[0].files) == 4


def test_a_missing_flag_is_said_and_raw_dumps_go_to_the_top_where_emuiibo_converts_them():
    entries = {"Pack/Kirby/amiibo.json": amiibo_json("Kirby"),     # no amiibo.flag
               "Pack/bins/Link.bin": b"\0" * 540, "Pack/bins/notes.bin": b"\0" * 12}
    collection = emuiibo.find_collection(files_of(entries), wrapper_name="Pack")
    by_label = {a.label: a for a in collection.amiibo}
    assert not by_label["Kirby"].enabled
    assert by_label["Link.bin"].kind == "dump" and by_label["Link.bin"].dest == "Link.bin"
    assert "notes.bin" not in by_label  # not an NTAG215 size
    assert by_label["Link.bin"].destinations() == [("Pack/bins/Link.bin", "emuiibo/amiibo/Link.bin", 540)]


def test_nothing_amiibo_is_no_collection():
    assert emuiibo.find_collection(files_of({"game/readme.txt": b"x"})) is None


def test_select_takes_amiibo_or_whole_folders():
    collection = emuiibo.find_collection(files_of(pack_entries()), wrapper_name=PACK)
    assert [a.dest for a in emuiibo.select(collection, ["Animal Crossing/Spork"])] == ["Animal Crossing/Spork/Crackle"]
    assert len(emuiibo.select(collection, ["animal crossing"])) == 3
    assert len(emuiibo.select(collection, None)) == 4
    assert emuiibo.select(collection, ["Nope"]) == []


def test_a_job_decides_per_amiibo_folder_never_per_file():
    units = emuiibo.manifest_units([
        "emuiibo/amiibo/AC/Admiral/amiibo.json", "emuiibo/amiibo/AC/Admiral/amiibo.flag",
        "emuiibo/amiibo/AC/Admiral/v2/amiibo.json", "emuiibo/amiibo/Link.bin",
    ])
    assert units == {
        "emuiibo/amiibo/AC/Admiral": ["emuiibo/amiibo/AC/Admiral/amiibo.json", "emuiibo/amiibo/AC/Admiral/amiibo.flag",
                                      "emuiibo/amiibo/AC/Admiral/v2/amiibo.json"],
        "emuiibo/amiibo/Link.bin": ["emuiibo/amiibo/Link.bin"],
    }


@pytest.mark.parametrize("path,ok", [
    ("emuiibo/amiibo/AC/Admiral", True),
    ("emuiibo/amiibo/AC/Admiral/amiibo.json", True),
    ("emuiibo/amiibo", False),              # the library itself, never
    ("emuiibo/amiibo/../miis", False),
    ("emuiibo/amiibo/./x", False),
    ("emuiibo/overlay/favorites.txt", False),
    ("atmosphere/contents/0100000000000352", False),
    ("/emuiibo/amiibo/x", False),
    ("emuiibo\\amiibo\\x", False),
])
def test_removal_never_leaves_emuiibo_amiibo(path, ok):
    assert emuiibo.is_inside_amiibo_dir(path) is ok


def test_favorites_lose_only_what_was_removed():
    data = b"sdmc:/emuiibo/amiibo/Animal Crossing/Isabelle\nsdmc:/emuiibo/amiibo/SSBU/Mario\n"
    assert emuiibo.parse_favorites(data) == ["Animal Crossing/Isabelle", "SSBU/Mario"]
    assert emuiibo.favorites_without(data, ["Animal Crossing"]) == b"sdmc:/emuiibo/amiibo/SSBU/Mario\n"
    assert emuiibo.favorites_without(data, ["Animal Crossing/Isa"]) is None  # a prefix of a name is not the folder


def test_emuiibos_pc_tools_are_recognised():
    assert emuiibo.pc_tool(files_of({"Release/emutool.exe": b"MZ"})) == "emutool"
    assert emuiibo.pc_tool(files_of({"emuiigen.jar": b"PK"})) == "emuiigen"
    assert emuiibo.pc_tool(files_of({"game.exe": b"MZ"})) is None


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

def _console(*, emuiibo_version="1.1.3", tesla=True):
    backend = MockMtpBackend(device_id="mock-switch-parent", device_name="Parent's Switch")
    backend.add_storage("SD_CARD")
    backend.add_storage("SD_INSTALL")
    backend.connect()
    sd = backend.storage_tree("SD_CARD")

    def put(path, data=b""):
        sd.ensure_directory(path.rpartition("/")[0])
        sd.write_file(path, data)

    if emuiibo_version:
        put(f"{emuiibo.SYSMODULE_DIR}/exefs.nsp", b"x")
        put(f"{emuiibo.SYSMODULE_DIR}/flags/boot2.flag")
        put(emuiibo.OVERLAY_FILE, mock_overlay_bytes(emuiibo_version))
    if tesla:
        put(f"{emuiibo.OVLLOADER_DIR}/exefs.nsp", b"x")
        put(f"{emuiibo.OVLLOADER_DIR}/flags/boot2.flag")
        put(emuiibo.TESLA_MENU_FILE, b"x")
    return backend, put


def _read(backend, known=None):
    state = None
    for state in emuiibo.read_device(backend, "SD_CARD", known=known):
        pass
    return state


def test_reading_a_console_finds_every_part_and_every_amiibo():
    backend, put = _console()
    put("emuiibo/amiibo/AC/Isabelle/amiibo.json", amiibo_json("Isabelle"))
    put("emuiibo/amiibo/AC/Isabelle/amiibo.flag")
    put("emuiibo/amiibo/AC/Isabelle/areas/0x38600500.bin", b"\0" * 216)
    put("emuiibo/amiibo/Hidden/amiibo.json", amiibo_json("Hidden"))       # no flag
    put("emuiibo/amiibo/Broken/amiibo.json", b"{ nope")
    put("emuiibo/amiibo/Link.bin", b"\0" * 0x10 + b"\xa5" + b"\0" * (540 - 0x11))
    put("emuiibo/flags/status_on.flag")
    put(emuiibo.FAVORITES_FILE, b"sdmc:/emuiibo/amiibo/AC/Isabelle\n")

    state = _read(backend)
    assert state.complete and state.installed
    assert state.components == {"sysmodule": True, "overlay": True, "ovlloader": True, "tesla_menu": True}
    assert state.overlay_version == "1.1.3"
    assert state.emulation_on is True
    assert state.favorites == ["AC/Isabelle"]
    by_path = {a.path: a for a in state.amiibo}
    assert set(by_path) == {"AC/Isabelle", "Hidden", "Broken", "Link.bin"}
    assert by_path["AC/Isabelle"].save_data and by_path["AC/Isabelle"].enabled
    assert by_path["AC/Isabelle"].info.name == "Isabelle"
    assert not by_path["Hidden"].enabled
    assert by_path["Broken"].info is None and not by_path["Broken"].valid
    assert by_path["Link.bin"].kind == "dump"


def test_a_console_without_emuiibo_says_exactly_what_is_missing():
    backend, _put = _console(emuiibo_version=None, tesla=False)
    state = _read(backend)
    assert state.complete and not state.installed and not state.library_exists
    assert [c.key for c in state.missing] == ["sysmodule", "overlay", "ovlloader", "tesla_menu"]


def test_a_sysmodule_without_its_boot_flag_does_not_count():
    backend, _put = _console()
    backend.storage_tree("SD_CARD").delete(f"{emuiibo.SYSMODULE_DIR}/flags/boot2.flag")
    assert _read(backend).components["sysmodule"] is False


def test_a_second_read_does_not_read_amiibo_json_again_when_its_size_is_unchanged():
    backend, put = _console()
    for i in range(5):
        put(f"emuiibo/amiibo/AC/A{i}/amiibo.json", amiibo_json(f"A{i}"))
        put(f"emuiibo/amiibo/AC/A{i}/amiibo.flag")
    first = _read(backend)
    reads_before = sum(1 for e in backend.operation_log if e.operation == "READ")
    second = _read(backend, known={a.path.lower(): a for a in first.amiibo})
    reads = sum(1 for e in backend.operation_log if e.operation == "READ") - reads_before
    assert reads == 1  # the overlay's version only
    assert [a.info.name for a in second.amiibo] == [a.info.name for a in first.amiibo]


# ---------------------------------------------------------------------------
# The Library: scanning
# ---------------------------------------------------------------------------

def _rows(conn):
    return {row["absolute_path"]: row for row in db.list_library_items(conn)}


def _release_folder(library):
    folder = library / "Animal Crossing New Horizons [NSP]" / "Amiibo JSON for Emuiibo"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_the_real_release_folder_scans_as_a_collection_a_release_and_a_pc_tool(isolated_db):
    conn, _ = isolated_db
    folder = _release_folder(config.LIBRARY_DIR)
    pack = build_7z(folder / f"{PACK}.7z", pack_entries())
    release = build_zip(folder / "emuiibo-v0.6.3.zip", release_entries("0.6.3"))
    tool = build_zip(folder / "emutool-v0.6.3.zip", {"Release/emutool.exe": b"MZ", "Release/FluentFTP.dll": b"MZ"})
    scanner.scan_library_once(conn)
    rows = _rows(conn)

    assert rows[str(pack)]["content_type"] == ContentType.AMIIBO.value
    assert rows[str(pack)]["status"] == "AVAILABLE"
    assert json.loads(rows[str(pack)]["details_json"])["amiibo"]["count"] == 4

    assert rows[str(release)]["content_type"] == ContentType.EMUIIBO.value
    assert rows[str(release)]["status"] == "AVAILABLE"
    # Read from the overlay's own NACP, not the file name.
    assert json.loads(rows[str(release)]["details_json"])["emuiibo"]["version"] == "0.6.3"

    assert rows[str(tool)]["status"] == scanner.NOT_FOR_SWITCH
    assert "PC" in rows[str(tool)]["note"]


def test_the_version_comes_from_the_overlay_even_when_the_name_has_none(isolated_db):
    conn, _ = isolated_db
    release = build_zip(config.LIBRARY_DIR / "emuiibo.zip", release_entries("1.1.3", lang=True))
    scanner.scan_library_once(conn)
    assert json.loads(_rows(conn)[str(release)]["details_json"])["emuiibo"]["version"] == "1.1.3"


def test_an_unpacked_collection_is_the_folder_holding_only_amiibo(isolated_db):
    conn, _ = isolated_db
    folder = _release_folder(config.LIBRARY_DIR)
    build_zip(folder / "emutool-v0.6.3.zip", {"Release/emutool.exe": b"MZ"})
    for name, data in pack_entries().items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (folder / PACK / "Thumbs.db").write_bytes(b"x")  # says nothing about the folder
    scanner.scan_library_once(conn)
    amiibo_rows = [r for r in db.list_library_items(conn) if r["content_type"] == ContentType.AMIIBO.value]
    # Not "Amiibo JSON for Emuiibo" (it also holds an archive), not each series.
    assert [r["absolute_path"] for r in amiibo_rows] == [str(folder / PACK)]
    report = preview.preview_path(folder / PACK)
    assert report.content_type is ContentType.AMIIBO
    assert sorted(d for _s, d in report.copy_plan)[0] == "emuiibo/amiibo/8-bit Mario/8-Bit Mario Classic Color/amiibo.flag"


def test_an_unpacked_release_is_one_item_not_a_mod_and_sd_files(isolated_db):
    conn, _ = isolated_db
    root = config.LIBRARY_DIR / "emuiibo 1.1.3"
    for name, data in release_entries("1.1.3", lang=True).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    scanner.scan_library_once(conn)
    rows = [r for r in db.list_library_items(conn) if r["status"] != db.LIBRARY_ITEM_RETIRED]
    assert [(r["absolute_path"], r["content_type"]) for r in rows] == [(str(root / "SdOut"), ContentType.EMUIIBO.value)]
    assert json.loads(rows[0]["details_json"])["emuiibo"]["version"] == "1.1.3"


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def _backend_registry(backend):
    registry = queue_worker.DeviceRegistry()
    registry.register(backend.device_id, backend)
    return registry


def _run_worker(conn, registry, limit=20):
    for _ in range(limit):
        if queue_worker.run_worker_once(conn, registry) is None:
            return


def _scan_pack(conn):
    pack = build_7z(config.LIBRARY_DIR / f"{PACK}.7z", pack_entries())
    scanner.scan_library_once(conn)
    return _rows(conn)[str(pack)]


def test_installing_a_collection_puts_every_amiibo_in_emuiibo_amiibo_and_nowhere_else(isolated_db):
    conn, _ = isolated_db
    row = _scan_pack(conn)
    backend, _put = _console()
    registry = _backend_registry(backend)
    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    assert result["errors"] == []
    job = db.get_job(conn, result["created"][0]["job_id"])
    assert job["target_storage"] == "SD_CARD"
    assert manifest_mod.load_manifest(job["id"]).content_type == ContentType.AMIIBO.value
    _run_worker(conn, registry)
    assert db.get_job(conn, job["id"])["status"] == "DONE"
    written = [p for p in backend.storage_tree("SD_CARD").list_files() if p.startswith("emuiibo/")]
    assert sorted(written) == sorted(dest for _src, dest in preview.amiibo_copy_plan(
        emuiibo.find_collection(files_of(pack_entries()), wrapper_name=PACK)))
    assert "emuiibo/amiibo/Animal Crossing/Spork/Crackle/amiibo.flag" in written
    # And the console, read back, has them.
    assert {a.path for a in _read(backend).amiibo} == {
        "8-bit Mario/8-Bit Mario Classic Color", "Animal Crossing/Admiral",
        "Animal Crossing/Isabelle", "Animal Crossing/Spork/Crackle"}


def test_a_selection_installs_only_those_amiibo(isolated_db):
    conn, _ = isolated_db
    row = _scan_pack(conn)
    backend, _put = _console()
    result = services.create_and_confirm_jobs(
        conn, [row["id"]], backend.device_id,
        amiibo_selection={str(row["id"]): ["Animal Crossing/Spork", "8-bit Mario/8-Bit Mario Classic Color"]},
    )
    assert result["errors"] == []
    _run_worker(conn, _backend_registry(backend))
    assert {a.path for a in _read(backend).amiibo} == {
        "Animal Crossing/Spork/Crackle", "8-bit Mario/8-Bit Mario Classic Color"}


def test_a_selection_naming_nothing_in_the_collection_is_an_error_not_an_empty_job(isolated_db):
    conn, _ = isolated_db
    row = _scan_pack(conn)
    backend, _put = _console()
    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id,
                                              amiibo_selection={str(row["id"]): ["Zelda"]})
    assert result["created"] == [] and "none of the selected amiibo" in result["errors"][0]["error"]


def test_an_amiibo_already_used_by_emuiibo_is_left_exactly_as_it_is(isolated_db):
    """emuiibo rewrites an amiibo as soon as it is used: amiibo.json gains
    use_random_uuid and a new write counter, areas.json and a mii appear,
    and save data under areas/. The same figure with the same UUID at the
    same place is the same amiibo -- skipped, its save data untouched, and
    the rest of the collection still installed."""
    conn, _ = isolated_db
    row = _scan_pack(conn)
    backend, put = _console()
    used = "emuiibo/amiibo/Animal Crossing/Admiral"
    put(f"{used}/amiibo.json", amiibo_json("Admiral", use_random_uuid=False, write_counter=12))
    put(f"{used}/amiibo.flag")
    put(f"{used}/areas.json", b'{"areas": []}')
    put(f"{used}/areas/0x38600500.bin", b"SAVE" * 54)

    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    job_id = result["created"][0]["job_id"]
    _run_worker(conn, _backend_registry(backend))
    job = db.get_job(conn, job_id)
    assert job["status"] == "DONE"
    assert "1 already on the console" in job["error"]
    sd = backend.storage_tree("SD_CARD")
    assert sd.read_file(f"{used}/areas/0x38600500.bin") == b"SAVE" * 54
    assert b'"write_counter": 12' in sd.read_file(f"{used}/amiibo.json")
    assert sd.exists("emuiibo/amiibo/Animal Crossing/Isabelle/amiibo.json")
    history = db.list_install_history(conn)
    assert history[0]["outcome"] == "DONE" and "already on the console" in history[0]["error"]


def test_a_converted_amiibo_is_compared_by_its_own_json_not_the_old_one_inside_it(isolated_db):
    """A collection taken off a console carries each converted amiibo's old
    files in a v2/ folder inside it -- amiibo.json included, in the old
    format. Identity is the amiibo's own amiibo.json."""
    conn, _ = isolated_db
    pack = build_7z(config.LIBRARY_DIR / "backup.7z", {
        "Mario/amiibo.json": amiibo_json("Mario"), "Mario/amiibo.flag": b"",
        "Mario/v2/amiibo.json": b'{"name": "old format"}',
    })
    scanner.scan_library_once(conn)
    row = _rows(conn)[str(pack)]
    backend, put = _console()
    put("emuiibo/amiibo/Mario/amiibo.json", amiibo_json("Mario", use_random_uuid=False))
    put("emuiibo/amiibo/Mario/areas/0x34F80200.bin", b"smash")
    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    _run_worker(conn, _backend_registry(backend))
    job = db.get_job(conn, result["created"][0]["job_id"])
    assert job["status"] == "DONE" and "1 already on the console" in job["error"]
    assert not backend.storage_tree("SD_CARD").exists("emuiibo/amiibo/Mario/v2/amiibo.json")


def test_a_different_amiibo_in_the_same_folder_is_not_touched_under_the_skip_policy(isolated_db):
    conn, _ = isolated_db
    row = _scan_pack(conn)
    backend, put = _console()
    taken = "emuiibo/amiibo/Animal Crossing/Admiral"
    put(f"{taken}/amiibo.json", amiibo_json("NotAdmiral", gcid=0x1234))
    put(f"{taken}/amiibo.flag")
    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    job_id = result["created"][0]["job_id"]
    _run_worker(conn, _backend_registry(backend))
    job = db.get_job(conn, job_id)
    assert job["status"] == "DONE"  # the other three still went
    assert "different amiibo already uses that folder" in job["error"]
    sd = backend.storage_tree("SD_CARD")
    assert b"NotAdmiral" in sd.read_file(f"{taken}/amiibo.json")
    assert sd.exists("emuiibo/amiibo/Animal Crossing/Isabelle/amiibo.json")


def test_under_the_override_policy_a_different_amiibo_is_replaced_whole(isolated_db, monkeypatch):
    conn, _ = isolated_db
    row = _scan_pack(conn)
    backend, put = _console()
    taken = "emuiibo/amiibo/Animal Crossing/Admiral"
    put(f"{taken}/amiibo.json", amiibo_json("NotAdmiral", gcid=0x1234))
    put(f"{taken}/amiibo.flag")
    put(f"{taken}/areas/0x10162B00.bin", b"old save")
    monkeypatch.setattr(config, "load_conflict_policy", lambda: "override")
    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    _run_worker(conn, _backend_registry(backend))
    job = db.get_job(conn, result["created"][0]["job_id"])
    assert job["status"] == "DONE" and "replaced a different amiibo" in job["error"]
    sd = backend.storage_tree("SD_CARD")
    assert emuiibo.parse_amiibo_json(sd.read_file(f"{taken}/amiibo.json")).name == "Admiral"
    # Nothing of the old amiibo is mixed into the new one.
    assert not sd.exists(f"{taken}/areas/0x10162B00.bin")


def test_installing_a_release_replaces_emuiibos_own_files_and_touches_nothing_else(isolated_db):
    conn, _ = isolated_db
    release = build_zip(config.LIBRARY_DIR / "emuiibo.zip", release_entries("1.1.3", lang=True))
    scanner.scan_library_once(conn)
    row = _rows(conn)[str(release)]
    backend, put = _console(emuiibo_version="0.6.3")
    put("emuiibo/amiibo/Mario/amiibo.json", amiibo_json("Mario"))
    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    assert result["errors"] == []
    _run_worker(conn, _backend_registry(backend))
    assert db.get_job(conn, result["created"][0]["job_id"])["status"] == "DONE"
    state = _read(backend)
    assert state.overlay_version == "1.1.3"
    assert backend.storage_tree("SD_CARD").exists("emuiibo/overlay/lang/ru.json")
    assert [a.path for a in state.amiibo] == ["Mario"]


def test_an_amiibo_job_can_never_write_outside_emuiibo_amiibo(tmp_path):
    report = preview.PreviewReport(content_type=ContentType.AMIIBO, source=str(tmp_path),
                                   copy_root=tmp_path, copy_plan=[("x", "atmosphere/contents/x/exefs.nsp")])
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod._build_copy_plan_files(report, tmp_path)
    report.copy_plan = [("x", "emuiibo/amiibo/../../boot.dat")]
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod._build_copy_plan_files(report, tmp_path)


def test_queue_and_history_name_it_amiibo(isolated_db):
    conn, _ = isolated_db
    row = _scan_pack(conn)
    backend, _put = _console()
    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    job = db.get_job(conn, result["created"][0]["job_id"])
    view = services._job_view(conn, job)
    assert view["variant_role"] == "amiibo"
    _run_worker(conn, _backend_registry(backend))
    rows = services.list_history_entries(conn)["rows"]
    assert rows[0]["role"] == "amiibo"
    assert db.list_install_history(conn)[0]["display_name"] == f"{PACK}.7z — Amiibo"


# ---------------------------------------------------------------------------
# The worker's emuiibo service: reading in slices, removing on request
# ---------------------------------------------------------------------------

def _service_with(conn, backend, *, slice_seconds=5.0):
    service = EmuiiboService(slice_seconds=slice_seconds)
    registry = _backend_registry(backend)
    service.on_devices({backend.device_id}, {backend.device_id})
    for _ in range(50):
        if not service.step(conn, registry):
            break
    return service, registry


def test_a_newly_connected_console_is_read_and_remembered(isolated_db):
    conn, _ = isolated_db
    backend, put = _console()
    put("emuiibo/amiibo/AC/Isabelle/amiibo.json", amiibo_json("Isabelle"))
    put("emuiibo/amiibo/AC/Isabelle/amiibo.flag")
    _service_with(conn, backend)
    state, amiibo, read_at = db.get_device_emuiibo(conn, backend.device_id)
    assert state["complete"] and state["components"]["sysmodule"]
    assert [a["path"] for a in amiibo] == ["AC/Isabelle"] and read_at


def test_a_read_in_tiny_slices_still_finishes_and_gives_the_worker_back_between_them(isolated_db):
    conn, _ = isolated_db
    backend, put = _console()
    for i in range(10):
        put(f"emuiibo/amiibo/G/A{i}/amiibo.json", amiibo_json(f"A{i}"))
        put(f"emuiibo/amiibo/G/A{i}/amiibo.flag")
    service = EmuiiboService(slice_seconds=0.0)
    registry = _backend_registry(backend)
    service.on_devices({backend.device_id}, {backend.device_id})
    passes = 0
    while service.step(conn, registry):
        passes += 1
        assert passes < 500
    assert passes > 5
    assert len(db.get_device_emuiibo(conn, backend.device_id)[1]) == 10


def test_removing_an_amiibo_deletes_its_folder_and_its_favorite(isolated_db):
    conn, _ = isolated_db
    backend, put = _console()
    for name in ("Isabelle", "Tom"):
        put(f"emuiibo/amiibo/AC/{name}/amiibo.json", amiibo_json(name))
        put(f"emuiibo/amiibo/AC/{name}/amiibo.flag")
        put(f"emuiibo/amiibo/AC/{name}/mii-charinfo.bin", b"\0" * 88)
    put(emuiibo.FAVORITES_FILE, b"sdmc:/emuiibo/amiibo/AC/Isabelle\nsdmc:/emuiibo/amiibo/AC/Tom\n")
    service, registry = _service_with(conn, backend)

    task = service.request_removal(conn, backend.device_id, ["AC/Isabelle"], confirm_save_data=False)
    assert task["amiibo"] == 1
    while service.step(conn, registry):
        pass
    sd = backend.storage_tree("SD_CARD")
    assert not any(p.startswith("emuiibo/amiibo/AC/Isabelle") for p in sd.nodes)
    assert sd.exists("emuiibo/amiibo/AC/Tom/amiibo.json")
    assert sd.read_file(emuiibo.FAVORITES_FILE) == b"sdmc:/emuiibo/amiibo/AC/Tom\n"
    assert [a["path"] for a in db.get_device_emuiibo(conn, backend.device_id)[1]] == ["AC/Tom"]
    finished = service.snapshot(backend.device_id)["finished_removals"][0]
    assert finished["state"] == "done" and finished["removed"] == ["AC/Isabelle"]


def test_save_data_is_never_removed_without_saying_so(isolated_db):
    conn, _ = isolated_db
    backend, put = _console()
    put("emuiibo/amiibo/Link/amiibo.json", amiibo_json("Link"))
    put("emuiibo/amiibo/Link/amiibo.flag")
    put("emuiibo/amiibo/Link/areas/0x1019C800.bin", b"zelda")
    service, registry = _service_with(conn, backend)
    with pytest.raises(RemovalRefused, match="save data"):
        service.request_removal(conn, backend.device_id, ["Link"], confirm_save_data=False)
    assert backend.storage_tree("SD_CARD").exists("emuiibo/amiibo/Link/areas/0x1019C800.bin")
    service.request_removal(conn, backend.device_id, ["Link"], confirm_save_data=True)
    while service.step(conn, registry):
        pass
    assert not backend.storage_tree("SD_CARD").exists("emuiibo/amiibo/Link")


@pytest.mark.parametrize("path", ["../overlay", "", "Nope"])
def test_a_removal_outside_the_library_or_of_nothing_is_refused(isolated_db, path):
    conn, _ = isolated_db
    backend, put = _console()
    put("emuiibo/amiibo/Link/amiibo.json", amiibo_json("Link"))
    service, _registry = _service_with(conn, backend)
    with pytest.raises(RemovalRefused):
        service.request_removal(conn, backend.device_id, [path], confirm_save_data=True)


def test_a_disconnected_console_is_not_changed(isolated_db):
    conn, _ = isolated_db
    backend, put = _console()
    put("emuiibo/amiibo/Link/amiibo.json", amiibo_json("Link"))
    service, _registry = _service_with(conn, backend)
    service.on_devices(set(), set())
    with pytest.raises(RemovalRefused, match="not connected"):
        service.request_removal(conn, backend.device_id, ["Link"], confirm_save_data=True)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    (tmp_path / "inbox").mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    db_path = tmp_path / "test.db"
    folder = _release_folder(library_dir)
    build_7z(folder / f"{PACK}.7z", pack_entries())
    build_zip(folder / "emuiibo-v0.6.3.zip", release_entries("0.6.3"))
    ctx = build_mock_context(db_path, seed_emuiibo=True)
    with db.open_db(db_path) as conn:
        scanner.scan_library_once(conn)
        ctx.refresh_devices(conn)
        while ctx.emuiibo.step(conn, ctx.registry):
            pass
    test_client = TestClient(create_app(ctx))
    test_client.ctx = ctx
    return test_client


def _parent_fp(client):
    return next(d["fingerprint"] for d in client.get("/api/amiibo").json()["devices"] if "Parent" in d["name"])


def test_the_page_prefers_the_connected_switch_that_has_emuiibo(client):
    assert "Amiibo" in client.get("/amiibo").text
    view = client.get("/api/amiibo").json()
    assert view["device"]["name"] == "Parent's Switch (mock)"
    console = view["console"]
    assert console["installed"] and console["overlay_version"] == "0.6.3"
    assert console["count"] == 3 and console["with_save_data"] == 1 and console["favorites"] == 1
    assert [c["name"] for c in view["collections"]] == [f"{PACK}.7z"]
    # The mock console's Isabelle is another figure than the pack's: the
    # page says so, and an install would leave hers alone.
    assert view["collections"][0]["counts"] == {"on_console": 0, "different": 1, "missing": 3}
    # The Library's release is the console's own, outdated one: what is
    # offered is the current emuiibo from GitHub, not the old one again.
    assert [a["kind"] for a in view["advice"]] == ["download_release"]


def test_a_bare_switch_is_told_what_to_install_and_where_it_comes_from(client):
    child = next(d["fingerprint"] for d in client.get("/api/amiibo").json()["devices"] if "Child" in d["name"])
    view = client.get(f"/api/amiibo?device={child}").json()
    kinds = [a["kind"] for a in view["advice"]]
    # emuiibo: the Library's 0.6.3 is outdated, so the current one from GitHub.
    assert kinds[0] == "download_release"
    assert [a["href"] for a in view["advice"] if a["kind"] == "link"] == [emuiibo.OVLLOADER_URL, emuiibo.TESLA_MENU_URL]


def test_removal_over_the_api(client):
    fp = _parent_fp(client)
    refused = client.post("/api/amiibo/remove", json={"device": fp, "paths": ["Animal Crossing/Isabelle"]})
    assert refused.status_code == 409 and "save data" in refused.json()["detail"]
    ok = client.post("/api/amiibo/remove", json={"device": fp, "paths": ["Super Smash Bros/Mario"]})
    assert ok.status_code == 200
    with db.open_db(client.ctx.db_path) as conn:
        while client.ctx.emuiibo.step(conn, client.ctx.registry):
            pass
    console = client.get(f"/api/amiibo?device={fp}").json()["console"]
    assert console["count"] == 2 and console["favorites"] == 0
    assert client.post("/api/amiibo/refresh", json={"device": fp}).status_code == 200
    assert client.get(f"/api/amiibo/activity?device={fp}").json()["reading"] is True


def test_a_selection_made_on_the_page_reaches_the_job_through_the_preparation_queue(client):
    import time

    fp = _parent_fp(client)
    collection = client.get(f"/api/amiibo?device={fp}").json()["collections"][0]
    response = client.post("/api/preparations", json={
        "library_item_ids": [collection["id"]], "target_device_id": fp,
        "amiibo_selection": {str(collection["id"]): ["Animal Crossing/Spork"]},
    })
    assert response.status_code == 202
    deadline = time.monotonic() + 20
    with db.open_db(client.ctx.db_path) as conn:
        while time.monotonic() < deadline:
            jobs = db.list_jobs(conn)
            if jobs and jobs[-1]["status"] not in ("PENDING_CONFIRM",):
                break
            time.sleep(0.05)
        job = db.list_jobs(conn)[-1]
    try:
        files = [f.dest_relative_path for f in manifest_mod.load_manifest(job["id"]).files]
        assert sorted(files) == ["emuiibo/amiibo/Animal Crossing/Spork/Crackle/amiibo.flag",
                                 "emuiibo/amiibo/Animal Crossing/Spork/Crackle/amiibo.json"]
    finally:
        # No worker runs here, so the preparation would wait for this job
        # forever -- and keep polling a database later tests patch.
        import threading

        client.ctx.preparations.abort_all()
        for thread in threading.enumerate():
            if thread.name.startswith("switchagent-preparation"):
                thread.join(10)


def test_emuiibo_and_amiibo_are_not_cards_among_the_games(client):
    """They are not games: the Library's grid leaves them out and says, in
    one line, where they are -- the Amiibo tab, which lists them all."""
    html = client.get("/").text
    assert f"{PACK}" not in html and "emuiibo-v0.6.3" not in html
    assert 'class="library-elsewhere" href="/amiibo"' in html
    assert "emuiibo and virtual amiibo from your Library (2) are on the Amiibo tab" in html
    view = client.get("/api/amiibo").json()
    assert [c["name"] for c in view["collections"]] == [f"{PACK}.7z"]
    assert [r["name"] for r in view["releases"]] == ["emuiibo-v0.6.3.zip"]


def test_emuiibos_pc_tool_is_listed_with_emuiibo_not_as_a_game(isolated_db):
    conn, _ = isolated_db
    build_zip(config.LIBRARY_DIR / "emutool-v0.6.3.zip", {"Release/emutool.exe": b"MZ"})
    build_zip(config.LIBRARY_DIR / "Game [0100000000010000].zip", {"Game [0100000000010000].nsp": b"PFS0"})
    scanner.scan_library_once(conn)
    view = services.list_library_view(conn, kind="games")
    assert [g["name"] for g in view["games"]] == ["Game [0100000000010000]"]
    assert view["on_amiibo_tab"] == 1
    assert [t["name"] for t in amiibo_views.library_pc_tools(conn)] == ["emutool-v0.6.3.zip"]


# ---------------------------------------------------------------------------
# emuiibo from GitHub
# ---------------------------------------------------------------------------

from switchagent import emuiibo_download  # noqa: E402

RELEASE_URL = "https://github.com/XorTroll/emuiibo/releases/download/1.1.3/emuiibo.zip"
ASSET_REDIRECT = "https://release-assets.githubusercontent.com/emuiibo.zip"


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _Response:
    def __init__(self, data: bytes, url: str):
        self._data = data
        self._pos = 0
        self._url = url

    def read(self, n=-1):
        chunk = self._data[self._pos:] if n is None or n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeGitHub:
    """GitHub's release API and its asset download, as they answer for
    emuiibo 1.1.3 (2026-09-25): asset `emuiibo.zip` with a sha256 digest,
    served after a redirect to release-assets.githubusercontent.com."""

    def __init__(self, asset: bytes, *, digest=None, url=RELEASE_URL, redirect=ASSET_REDIRECT, size=None):
        import hashlib

        self.asset = asset
        self.requests = []
        self.api = {
            "tag_name": "1.1.3", "draft": False, "prerelease": False,
            "html_url": "https://github.com/XorTroll/emuiibo/releases/tag/1.1.3",
            "assets": [
                {"name": "emuiigen.jar", "size": 10, "browser_download_url": "https://github.com/x/emuiigen.jar"},
                {"name": "emuiibo.zip", "size": len(asset) if size is None else size,
                 "digest": digest if digest is not None else "sha256:" + hashlib.sha256(asset).hexdigest(),
                 "browser_download_url": url},
            ],
        }
        self.redirect = redirect

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(url)
        if url == emuiibo_download.LATEST_RELEASE_API:
            return _Response(json.dumps(self.api).encode(), url)
        return _Response(self.asset, self.redirect)


def test_the_current_release_is_downloaded_checked_and_kept(tmp_path):
    asset = _zip_bytes(release_entries("1.1.3", lang=True))
    github = FakeGitHub(asset)
    release = emuiibo_download.latest_release(opener=github)
    assert (release.version, release.size) == ("1.1.3", len(asset))
    path = emuiibo_download.download(release, tmp_path, opener=github)
    assert path == tmp_path / "emuiibo-v1.1.3.zip" and path.read_bytes() == asset
    assert not list(tmp_path.glob("*.part"))
    # Already there with the published checksum: not fetched again.
    emuiibo_download.download(release, tmp_path, opener=github)
    assert github.requests.count(RELEASE_URL) == 1


@pytest.mark.parametrize("case,expected", [
    ("wrong checksum", "does not match the checksum"),
    ("not emuiibo", "not an emuiibo release"),
    ("bigger than declared", "larger than GitHub said"),
    ("redirected off GitHub", "not GitHub"),
])
def test_a_download_that_is_not_exactly_emuiibo_is_refused_and_leaves_nothing(tmp_path, case, expected):
    asset = _zip_bytes(release_entries("1.1.3"))
    github = {
        "wrong checksum": lambda: FakeGitHub(asset, digest="sha256:" + "0" * 64),
        "not emuiibo": lambda: FakeGitHub(_zip_bytes({"readme.txt": b"hello"})),
        "bigger than declared": lambda: FakeGitHub(asset, size=len(asset) - 1),
        "redirected off GitHub": lambda: FakeGitHub(asset, redirect="https://evil.example/emuiibo.zip"),
    }[case]()
    release = emuiibo_download.latest_release(opener=github)
    with pytest.raises(emuiibo_download.DownloadError, match=expected):
        emuiibo_download.download(release, tmp_path, opener=github)
    assert list(tmp_path.iterdir()) == []


def test_only_githubs_own_emuiibo_zip_is_ever_taken():
    github = FakeGitHub(_zip_bytes(release_entries("1.1.3")), url="https://evil.example/emuiibo.zip")
    with pytest.raises(emuiibo_download.DownloadError, match="not GitHub"):
        emuiibo_download.latest_release(opener=github)
    github = FakeGitHub(b"x")
    github.api["assets"] = [a for a in github.api["assets"] if a["name"] != "emuiibo.zip"]
    with pytest.raises(emuiibo_download.DownloadError, match="no emuiibo.zip"):
        emuiibo_download.latest_release(opener=github)


class _RecordingPreparations:
    def __init__(self):
        self.submitted = []

    def submit(self, item_ids, target, **_kwargs):
        self.submitted.append((list(item_ids), target))
        return "task"


def _run_downloader(downloader, device_id):
    import time

    downloader.start(device_id)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = downloader.snapshot()
        if state["state"] in ("done", "failed"):
            return state
        time.sleep(0.02)
    raise AssertionError("the download never finished")


def test_download_and_install_puts_the_release_in_the_library_and_queues_it(isolated_db):
    from switchagent.web.emuiibo_service import ReleaseDownloader

    conn, _ = isolated_db
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    github = FakeGitHub(_zip_bytes(release_entries("1.1.3", lang=True)))
    preparations = _RecordingPreparations()
    state = _run_downloader(ReleaseDownloader(db_path, preparations, opener=github), "mock-switch-parent")
    assert state["state"] == "done", state["error"]
    assert state["verified"] and not state["reused"] and state["queued"]
    path = config.LIBRARY_DIR / emuiibo_download.DOWNLOADS_FOLDER / "emuiibo-v1.1.3.zip"
    row = _rows(conn)[str(path)]
    assert row["content_type"] == ContentType.EMUIIBO.value and row["status"] == "AVAILABLE"
    assert json.loads(row["details_json"])["emuiibo"]["version"] == "1.1.3"
    assert preparations.submitted == [([row["id"]], "mock-switch-parent")]
    # The next full scan finds it exactly as it was indexed.
    assert scanner.scan_library_once(conn)["new"] == 0


def test_a_current_release_already_in_the_library_is_installed_not_downloaded_again(isolated_db):
    from switchagent.web.emuiibo_service import ReleaseDownloader

    conn, _ = isolated_db
    build_zip(config.LIBRARY_DIR / "emuiibo.zip", release_entries("1.1.3"))
    scanner.scan_library_once(conn)
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    github = FakeGitHub(_zip_bytes(release_entries("1.1.3")))
    preparations = _RecordingPreparations()
    state = _run_downloader(ReleaseDownloader(db_path, preparations, opener=github), "mock-switch-parent")
    assert state["state"] == "done" and state["reused"]
    assert github.requests == [emuiibo_download.LATEST_RELEASE_API]
    assert preparations.submitted == [([_rows(conn)[str(config.LIBRARY_DIR / "emuiibo.zip")]["id"]],
                                       "mock-switch-parent")]


def test_a_switch_without_emuiibo_is_offered_the_download_when_the_library_has_only_an_old_one(client):
    child = next(d["fingerprint"] for d in client.get("/api/amiibo").json()["devices"] if "Child" in d["name"])
    advice = client.get(f"/api/amiibo?device={child}").json()["advice"]
    assert advice[0]["kind"] == "download_release"
    assert "0.6.3, which is outdated" in advice[0]["text"]


def test_the_download_is_started_over_the_api(client, monkeypatch):
    import time

    github = FakeGitHub(_zip_bytes(release_entries("1.1.3", lang=True)))
    monkeypatch.setattr(client.ctx.emuiibo_downloads, "_opener", github)
    submitted = []
    monkeypatch.setattr(client.ctx.preparations, "submit", lambda ids, target, **_k: submitted.append(ids))
    fp = _parent_fp(client)
    assert client.post("/api/amiibo/emuiibo/download", json={"device": fp}).status_code == 202
    deadline = time.monotonic() + 20
    download = None
    while time.monotonic() < deadline:
        download = client.get(f"/api/amiibo/activity?device={fp}").json()["download"]
        if download["state"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert download["state"] == "done", download["error"]
    assert download["version"] == "1.1.3" and download["queued"] and submitted
    view = client.get(f"/api/amiibo?device={fp}").json()
    assert [r["version"] for r in view["releases"]] == ["1.1.3", "0.6.3"]


def test_a_current_release_in_the_library_is_installed_from_there():
    console = {"components": [
        {"key": "sysmodule", "name": "emuiibo", "present": False, "in_release": True},
        {"key": "overlay", "name": "emuiibo overlay", "present": False, "in_release": True},
        {"key": "ovlloader", "name": "nx-ovlloader", "present": True, "in_release": False},
        {"key": "tesla_menu", "name": "Tesla Menu", "present": True, "in_release": False},
    ], "overlay_version": None, "overlay_outdated": False}
    releases = [{"id": 7, "name": "emuiibo.zip", "version": "1.1.3", "outdated": False, "can_install": True},
                {"id": 3, "name": "emuiibo-v0.6.3.zip", "version": "0.6.3", "outdated": True, "can_install": True}]
    advice = amiibo_views._advice(console, releases, True)
    assert [(a["kind"], a.get("item_id")) for a in advice] == [("install_release", 7)]


# ---------------------------------------------------------------------------
# A collection that came with a game is part of that game's card
# ---------------------------------------------------------------------------

AC_BASE = "01006F8002326000"
AC_DIR = "Animal Crossing New Horizons [NSP]"


def _animal_crossing_release(library, *, second_game_at_top=False):
    """The real release's layout: the game, its update and a DLC at the top,
    the Island Transfer Tool (a game of its own) in a sub-folder, the amiibo
    pack in "Amiibo JSON for Emuiibo"."""
    folder = library / AC_DIR
    (folder / "Official Transfer Tool").mkdir(parents=True, exist_ok=True)
    (folder / "Amiibo JSON for Emuiibo").mkdir(parents=True, exist_ok=True)
    (folder / f"Animal Crossing New Horizons [{AC_BASE}][v0].nsp").write_bytes(b"PFS0 base")
    (folder / "Animal Crossing New Horizons [01006F8002326800][v2228224].nsp").write_bytes(b"PFS0 upd")
    (folder / "Animal Crossing New Horizons [DLC Happy Home Paradise] [01006F80023273E8][v0].nsp").write_bytes(b"PFS0")
    tool_dir = folder if second_game_at_top else folder / "Official Transfer Tool"
    (tool_dir / "Island Transfer Tool [0100F38011CFE000][v0].nsp").write_bytes(b"PFS0 tool")
    pack = build_7z(folder / "Amiibo JSON for Emuiibo" / f"{PACK}.7z", pack_entries())
    return pack


def test_the_amiibo_of_a_release_belong_to_the_game_at_its_top_not_to_the_tool_below(isolated_db):
    conn, _ = isolated_db
    pack = _animal_crossing_release(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)
    row = _rows(conn)[str(pack)]
    assert (row["title_id"], row["title_id_source"]) == (AC_BASE, "folder")

    view = services.list_library_view(conn, kind="games")
    game = next(g for g in view["games"] if g["base_title_id"] == AC_BASE)
    assert [a["id"] for a in game["amiibo"]] == [row["id"]] and game["amiibo_count"] == 4
    assert view["on_amiibo_tab"] == 0  # it is on the game's card now
    assert [g["base_title_id"] for g in services.list_library_view(conn, kind="games", group_filter="amiibo")["games"]] \
        == [AC_BASE]


def test_two_games_at_the_top_of_one_folder_means_nobody_is_guessed(isolated_db):
    conn, _ = isolated_db
    pack = _animal_crossing_release(config.LIBRARY_DIR, second_game_at_top=True)
    scanner.scan_library_once(conn)
    assert _rows(conn)[str(pack)]["title_id"] is None
    assert services.list_library_view(conn, kind="games")["on_amiibo_tab"] == 1


def test_a_title_id_in_the_collections_own_name_decides(isolated_db):
    conn, _ = isolated_db
    pack = build_7z(config.LIBRARY_DIR / "Splatoon amiibo [0100C2500FC20000].7z", pack_entries())
    scanner.scan_library_once(conn)
    assert (_rows(conn)[str(pack)]["title_id"], _rows(conn)[str(pack)]["title_id_source"]) == \
        ("0100C2500FC20000", "filename")


def test_selecting_the_game_installs_its_amiibo_after_it(isolated_db):
    from switchagent.web.preparation import _group_by_title

    conn, _ = isolated_db
    pack = _animal_crossing_release(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)
    rows = _rows(conn)
    base = next(r for r in rows.values() if r["title_id"] == AC_BASE and r["content_type"] == "GAME_PACKAGE")
    amiibo_row = rows[str(pack)]
    # Clicked in the "wrong" order: the amiibo still go after the game.
    chains = _group_by_title(conn, [amiibo_row["id"], base["id"]])
    assert chains == {AC_BASE: [base["id"], amiibo_row["id"]]}

    backend, _put = _console()
    result = services.create_and_confirm_jobs(conn, [base["id"], amiibo_row["id"]], backend.device_id)
    assert result["errors"] == []
    amiibo_job = next(db.get_job(conn, c["job_id"]) for c in result["created"]
                      if db.get_job(conn, c["job_id"])["library_item_id"] == amiibo_row["id"])
    # Recorded under the game, so Queue and History show it with it.
    assert manifest_mod.load_manifest(amiibo_job["id"]).title_id == AC_BASE
    assert services._job_view(conn, amiibo_job)["variant_role"] == "amiibo"


def test_the_game_card_carries_the_amiibo_tag(tmp_path, monkeypatch):
    (tmp_path / "library").mkdir()
    (tmp_path / "inbox").mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    _animal_crossing_release(tmp_path / "library")
    db_path = tmp_path / "test.db"
    with db.open_db(db_path) as conn:
        scanner.scan_library_once(conn)
    client = TestClient(create_app(build_mock_context(db_path)))
    html = client.get("/").text
    assert 'class="extras-pill extras-pill-amiibo"' in html and ">4 amiibo<" in html
    assert f'data-family="{AC_BASE}" data-role="amiibo"' in html
    assert "Amiibo — " + PACK + ".7z" in html
    assert 'class="library-elsewhere"' not in html
    detail = client.get(f"/games/{AC_BASE}").text
    assert "Amiibo: <strong>4</strong>" in detail and "4 virtual amiibo for emuiibo" in detail


# ---------------------------------------------------------------------------
# No Amiibo tab before emuiibo is on a console; emuiibo offered with amiibo
# ---------------------------------------------------------------------------

def test_the_amiibo_tab_comes_with_emuiibo_and_goes_with_it(isolated_db):
    conn, _ = isolated_db
    assert not amiibo_views.amiibo_tab_visible(conn)
    release = build_zip(config.LIBRARY_DIR / "emuiibo.zip", release_entries("1.1.3", lang=True))
    scanner.scan_library_once(conn)
    backend, _put = _console(emuiibo_version=None)
    bare = _read(backend)
    db.set_device_emuiibo(conn, backend.device_id, bare.to_dict(), [], read_at="2026-01-01T00:00:00+00:00")
    assert amiibo_views.emuiibo_on_device(conn, backend.device_id) == "missing"
    assert not amiibo_views.amiibo_tab_visible(conn)

    result = services.create_and_confirm_jobs(conn, [_rows(conn)[str(release)]["id"]], backend.device_id)
    assert amiibo_views.emuiibo_on_device(conn, backend.device_id) == "queued"
    assert not amiibo_views.amiibo_tab_visible(conn)
    _run_worker(conn, _backend_registry(backend))
    assert db.get_job(conn, result["created"][0]["job_id"])["status"] == "DONE"
    # Delivered: the tab is there before the console is read again...
    assert amiibo_views.emuiibo_on_device(conn, backend.device_id) == "installed"
    assert amiibo_views.amiibo_tab_visible(conn)
    installed = _read(backend)
    db.set_device_emuiibo(conn, backend.device_id, installed.to_dict(), [])
    assert amiibo_views.amiibo_tab_visible(conn)
    # ...and a read that no longer finds it is what counts after that.
    db.set_device_emuiibo(conn, backend.device_id, bare.to_dict(), [], read_at="2099-01-01T00:00:00+00:00")
    assert not amiibo_views.amiibo_tab_visible(conn)


def _ac_client(tmp_path, monkeypatch, *, read_consoles=False):
    library = tmp_path / "library"
    library.mkdir()
    (tmp_path / "inbox").mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    _animal_crossing_release(library)
    db_path = tmp_path / "test.db"
    ctx = build_mock_context(db_path)
    with db.open_db(db_path) as conn:
        scanner.scan_library_once(conn)
        if read_consoles:
            ctx.refresh_devices(conn)
            while ctx.emuiibo.step(conn, ctx.registry):
                pass
    test_client = TestClient(create_app(ctx))
    test_client.ctx = ctx
    return test_client


def test_without_emuiibo_anywhere_there_is_no_amiibo_tab(tmp_path, monkeypatch):
    client = _ac_client(tmp_path, monkeypatch, read_consoles=True)
    build_zip(config.LIBRARY_DIR / "emuiibo.zip", release_entries("1.1.3"))
    with db.open_db(client.ctx.db_path) as conn:
        scanner.scan_library_once(conn)
    html = client.get("/").text
    assert 'href="/amiibo"' not in html
    # What is off the grid says where emuiibo comes from, not a missing tab.
    assert 'class="library-elsewhere" href="/addons#addon-emuiibo"' in html
    moved = client.get("/amiibo", follow_redirects=False)
    assert (moved.status_code, moved.headers["location"]) == (303, "/addons#addon-emuiibo")
    addons = client.get("/addons").text
    assert 'href="/amiibo"' not in addons and 'data-install="emuiibo"' in addons
    assert "needs emuiibo →" in client.get(f"/games/{AC_BASE}").text


def test_the_tab_is_there_once_a_console_has_emuiibo(client):
    html = client.get("/").text
    assert 'href="/amiibo"' in html
    assert client.get("/amiibo", follow_redirects=False).status_code == 200


def _fp(client, name):
    return next(d["fingerprint"] for d in client.get("/api/amiibo").json()["devices"] if name in d["name"])


def test_amiibo_for_a_switch_without_emuiibo_come_with_the_offer_to_install_it(client):
    child = client.get(f"/api/amiibo/emuiibo/offer?device={_fp(client, 'Child')}").json()
    assert (child["state"], child["offer"]) == ("missing", True)
    assert child["tesla_missing"] == ["Tesla Menu", "nx-ovlloader"]
    parent = client.get(f"/api/amiibo/emuiibo/offer?device={_fp(client, 'Parent')}").json()
    assert (parent["state"], parent["offer"], parent["tesla_missing"]) == ("installed", False, [])
    assert client.get("/api/amiibo/emuiibo/offer?device=nobody").status_code == 404


def test_no_offer_while_emuiibo_is_already_on_its_way(client):
    with db.open_db(client.ctx.db_path) as conn:
        release = next(r for r in db.list_library_items(conn) if r["content_type"] == ContentType.EMUIIBO.value)
        assert services.create_and_confirm_jobs(conn, [release["id"]], "mock-switch-child")["errors"] == []
    offer = client.get(f"/api/amiibo/emuiibo/offer?device={_fp(client, 'Child')}").json()
    assert (offer["state"], offer["offer"]) == ("queued", False)


def test_a_switch_never_read_is_offered_emuiibo_without_assuming_it_lacks_it(tmp_path, monkeypatch):
    client = _ac_client(tmp_path, monkeypatch)
    with db.open_db(client.ctx.db_path) as conn:
        client.ctx.refresh_devices(conn)  # connected, not read yet
    fp = next(d["device_fingerprint"] for d in client.get("/api/devices").json() if "Parent" in d["display_name"])
    offer = client.get(f"/api/amiibo/emuiibo/offer?device={fp}").json()
    assert (offer["state"], offer["offer"], offer["tesla_missing"]) == ("unknown", True, [])


def test_the_confirmation_has_room_for_the_offer(tmp_path, monkeypatch):
    html = _ac_client(tmp_path, monkeypatch).get("/").text
    assert 'id="confirm-emuiibo"' in html and 'id="confirm-emuiibo-check"' in html
