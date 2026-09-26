"""emuiibo -- virtual amiibo on a Switch, and everything SwitchAgent knows
about it.

Studied 2026-09-25 against emuiibo's own source (XorTroll/emuiibo, master
at 1.1.3, released 2026-03-14) and a real release as it ships in the wild
(emuiibo-v0.6.3.zip + "ALL amiibo (flag_json).7z", 766 virtual amiibo):

What runs on the console
  - the sysmodule, `atmosphere/contents/0100000000000352/exefs.nsp`, started
    at boot by its `flags/boot2.flag`. It intercepts the games' nfp:user and
    nfp:sys services and answers them with the "active" virtual amiibo.
    Reads its files at boot, so anything copied over MTP needs a reboot
    before emuiibo sees it (said by emuiibo's own README).
  - the overlay, `switch/.overlays/emuiibo.ovl` -- the only way to pick the
    active amiibo, toggle emulation and connect/disconnect it in-game. It
    refuses to work with any other emuiibo version than its own, so the two
    always come from the same release. 1.x releases also carry its
    translations, `emuiibo/overlay/lang/*.json`.
  - the overlay needs an overlay menu: nx-ovlloader
    (`atmosphere/contents/420000000007E51A/exefs.nsp` + boot2.flag) and
    `switch/.overlays/ovlmenu.ovl` -- Ultrahand Overlay, whose release
    carries both (Tesla Menu, unmaintained since 2023, used the same file).
    Neither is part of emuiibo's release.

What lives on the SD card
  - `emuiibo/amiibo/` -- the library. A virtual amiibo is a FOLDER, at any
    depth (sub-folders are how the overlay groups them), holding
    `amiibo.json` AND `amiibo.flag`. No flag, no amiibo: removing the flag
    is emuiibo's own way to hide one.
  - on first use emuiibo writes into that folder itself: `areas.json`, a
    random mii as `mii-charinfo.bin` when the JSON names one that is not
    there, `use_random_uuid` added to amiibo.json, and `areas/0x<id>.bin` --
    per-game save data (Splatoon's gear, Zelda's once-a-day drops...).
    A folder with `areas/` holds somebody's progress.
  - raw NTAG215 dumps (`*.bin`) and 0.3-0.4-era folders placed directly in
    `emuiibo/amiibo/` (never deeper -- the converter only looks at the top
    level) are converted into the format above at boot.
  - `emuiibo/overlay/favorites.txt` (full sdmc: paths), `emuiibo/flags/
    status_on.flag` (emulation on after a reboot), `emuiibo/miis/` (the
    console's own miis, re-exported every boot).

What SwitchAgent does with that: it recognises a release and a collection in
the Library by their structure, never by name, copies them to exactly those
places over the existing DBI MTP backend, reads what is on the console back
the same way, and removes an amiibo folder when asked to. The overlay menu
is the Add-ons catalog's Ultrahand entry, installed with emuiibo when the
console lacks it (switchagent/addons.py).
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable, Iterable, Iterator, Optional

PROGRAM_ID = "0100000000000352"
SYSMODULE_DIR = f"atmosphere/contents/{PROGRAM_ID}"
OVERLAY_FILE = "switch/.overlays/emuiibo.ovl"
BASE_DIR = "emuiibo"
AMIIBO_DIR = "emuiibo/amiibo"
OVERLAY_DATA_DIR = "emuiibo/overlay"
FAVORITES_FILE = "emuiibo/overlay/favorites.txt"
STATUS_ON_FLAG = "emuiibo/flags/status_on.flag"

OVLLOADER_PROGRAM_ID = "420000000007E51A"
OVLLOADER_DIR = f"atmosphere/contents/{OVLLOADER_PROGRAM_ID}"
TESLA_MENU_FILE = "switch/.overlays/ovlmenu.ovl"

# The newest emuiibo known when this was written; a Library release older
# than this is said to be outdated. Where the current one always is:
LATEST_KNOWN_VERSION = "1.1.3"
RELEASES_URL = "https://github.com/XorTroll/emuiibo/releases/latest"
TESLA_MENU_URL = "https://github.com/ppkantorski/Ultrahand-Overlay/releases/latest"
OVLLOADER_URL = "https://github.com/ppkantorski/nx-ovlloader/releases/latest"

AMIIBO_JSON = "amiibo.json"
AMIIBO_FLAG = "amiibo.flag"
SAVE_DATA_DIR = "areas"

# NTAG215 dump sizes emuiibo's bin converter reads (540 is the full tag;
# 532 and 572 are the two other dump layouts tools produce).
RAW_DUMP_SIZES = frozenset({532, 540, 572})
# Where nfc tools put the 0xA5 marker emuiibo checks before converting.
_RAW_DUMP_MARKER_OFFSET = 0x10

# Files that say nothing about a folder's content -- a folder of amiibo with
# a Thumbs.db in it is still only amiibo.
_IGNORABLE_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

# Largest amiibo.json worth reading: the real ones are ~520 bytes.
AMIIBO_JSON_MAX_BYTES = 64 * 1024
# An overlay is an .nro of a few hundred KB (0.6.3: 363 KB).
OVERLAY_MAX_BYTES = 8 * 1024 * 1024

_VERSION_IN_NAME_RE = re.compile(r"(?<![0-9])v?(\d+\.\d+\.\d+)(?![0-9])", re.IGNORECASE)


# ---------------------------------------------------------------------------
# What emuiibo needs on the console
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Component:
    key: str
    name: str
    paths: tuple[str, ...]   # every one must exist on the SD card
    source: str              # where it comes from
    purpose: str
    in_emuiibo_release: bool


COMPONENTS: tuple[Component, ...] = (
    Component(
        "sysmodule", "emuiibo", (f"{SYSMODULE_DIR}/exefs.nsp", f"{SYSMODULE_DIR}/flags/boot2.flag"),
        RELEASES_URL, "answers games' amiibo requests with the active virtual amiibo", True,
    ),
    Component(
        "overlay", "emuiibo overlay", (OVERLAY_FILE,), RELEASES_URL,
        "picks the active amiibo in-game; must be the same version as the sysmodule", True,
    ),
    Component(
        "ovlloader", "nx-ovlloader", (f"{OVLLOADER_DIR}/exefs.nsp", f"{OVLLOADER_DIR}/flags/boot2.flag"),
        OVLLOADER_URL, "loads overlays in the background", False,
    ),
    Component(
        "tesla_menu", "Ultrahand Overlay", (TESLA_MENU_FILE,), TESLA_MENU_URL,
        "the overlay menu -- opens overlays with L + D-pad Down + R3", False,
    ),
)
COMPONENTS_BY_KEY = {c.key: c for c in COMPONENTS}


def version_tuple(version: Optional[str]) -> Optional[tuple[int, ...]]:
    if not version:
        return None
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def is_outdated(version: Optional[str]) -> bool:
    mine, latest = version_tuple(version), version_tuple(LATEST_KNOWN_VERSION)
    return mine is not None and latest is not None and mine < latest


def version_from_name(name: str) -> Optional[str]:
    match = _VERSION_IN_NAME_RE.search(name)
    return match.group(1) if match else None


def overlay_version(data: bytes) -> Optional[str]:
    """The version an emuiibo.ovl (an .nro) declares in its own NACP --
    exactly what the overlay compares against the sysmodule. None for
    anything that is not an .nro with an asset section."""
    try:
        if data[0x10:0x14] != b"NRO0":
            return None
        nro_size = struct.unpack_from("<I", data, 0x18)[0]
        if data[nro_size:nro_size + 4] != b"ASET":
            return None
        _ver, _icon_off, _icon_size, nacp_off, nacp_size = struct.unpack_from("<IQQQQ", data, nro_size + 4)
        nacp = data[nro_size + nacp_off:nro_size + nacp_off + nacp_size]
        raw = nacp[0x3060:0x3070].split(b"\0", 1)[0]
        version = raw.decode("ascii", "replace").strip()
    except (struct.error, IndexError):
        return None
    return version or None


# ---------------------------------------------------------------------------
# A virtual amiibo's own files
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AmiiboId:
    game_character_id: int
    character_variant: int
    figure_type: int
    model_number: int
    series: int

    @property
    def hex(self) -> str:
        """The figure's id as AmiiboAPI and every amiibo database write it
        (head + tail, 16 hex digits). emuiibo keeps game_character_id
        byte-swapped relative to that -- emuiigen swaps it back the same
        way when it imports from AmiiboAPI."""
        swapped = ((self.game_character_id & 0xFF) << 8) | (self.game_character_id >> 8)
        return (f"{swapped:04x}{self.character_variant:02x}{self.figure_type:02x}"
                f"{self.model_number:04x}{self.series:02x}02")


@dataclass(frozen=True)
class AmiiboInfo:
    name: str
    id: AmiiboId
    uuid: Optional[tuple[int, ...]]
    use_random_uuid: Optional[bool]
    mii_charinfo_file: str

    @property
    def uuid_hex(self) -> Optional[str]:
        return bytes(self.uuid).hex() if self.uuid else None

    def same_amiibo(self, other: "AmiiboInfo") -> bool:
        """The same virtual amiibo: same figure, same tag UUID. What emuiibo
        writes into an amiibo while it is used (name via System Settings,
        counters, dates, save data) does not make it a different one; a
        different figure or a different UUID does."""
        # A collection without UUIDs gets a random one from emuiibo on first
        # load, so a missing UUID on either side cannot tell two apart.
        return self.id == other.id and (self.uuid is None or other.uuid is None or self.uuid == other.uuid)


def _int(value, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(value)
    return value


def parse_amiibo_json(data: bytes) -> Optional[AmiiboInfo]:
    """amiibo.json -> AmiiboInfo, or None when emuiibo itself would refuse
    it. The required fields are exactly the ones emuiibo's own deserializer
    (fmt.rs, VirtualAmiiboInfoOptional) cannot do without; `uuid` and
    `use_random_uuid` may be missing -- emuiibo fills them in on first load,
    which is how 0.6-era collections like the real 766-amiibo pack ship."""
    try:
        doc = json.loads(data.decode("utf-8-sig"))
        if not isinstance(doc, dict):
            return None
        raw_id = doc["id"]
        amiibo_id = AmiiboId(
            game_character_id=_int(raw_id["game_character_id"], 0, 0xFFFF),
            character_variant=_int(raw_id["character_variant"], 0, 0xFF),
            figure_type=_int(raw_id["figure_type"], 0, 0xFF),
            model_number=_int(raw_id["model_number"], 0, 0xFFFF),
            series=_int(raw_id["series"], 0, 0xFF),
        )
        for date_key in ("first_write_date", "last_write_date"):
            date = doc[date_key]
            _int(date["y"], 0, 0xFFFF), _int(date["m"], 0, 0xFF), _int(date["d"], 0, 0xFF)
        _int(doc["version"], 0, 0xFF)
        _int(doc["write_counter"], 0, 0xFFFF)
        name = doc["name"]
        mii = doc["mii_charinfo_file"]
        if not isinstance(name, str) or not isinstance(mii, str):
            return None
        uuid = doc.get("uuid")
        if uuid is not None:
            uuid = tuple(_int(b, 0, 0xFF) for b in uuid)
        random_uuid = doc.get("use_random_uuid")
        if random_uuid is not None and not isinstance(random_uuid, bool):
            return None
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
    return AmiiboInfo(name=name, id=amiibo_id, uuid=uuid, use_random_uuid=random_uuid, mii_charinfo_file=mii)


def looks_like_raw_dump(name: str, size: int, head: Optional[bytes] = None) -> bool:
    """A raw NTAG215 amiibo dump emuiibo converts at boot. By name and size
    alone when that is all there is (an archive listing); when the bytes are
    at hand, also the 0xA5 marker emuiibo itself checks."""
    if not name.lower().endswith(".bin") or size not in RAW_DUMP_SIZES:
        return False
    if head is not None:
        return len(head) > _RAW_DUMP_MARKER_OFFSET and head[_RAW_DUMP_MARKER_OFFSET] == 0xA5
    return True


# ---------------------------------------------------------------------------
# Trees: a release or a collection, found from a list of file paths
# ---------------------------------------------------------------------------
#
# Everything below works on (posix path, size) pairs relative to some root --
# an archive's own listing, or a folder walked on disk -- so the same rules
# apply to both, and nothing needs extracting to classify.

def _parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]


def _parent(path: str) -> str:
    return "/".join(_parts(path)[:-1])


def _is_under(path: str, folder: str) -> bool:
    return folder == "" or path == folder or path.startswith(folder + "/")


def release_root(files: Iterable[tuple[str, int]]) -> Optional[str]:
    """Where the SD card's root is inside an emuiibo release ("SdOut" for
    both 0.6.3 and 1.1.3, "" when it was unpacked without it), or None when
    this is not one. Recognised by the sysmodule itself --
    atmosphere/contents/0100000000000352/exefs.nsp -- never by a name."""
    roots = set()
    for path, _size in files:
        parts = _parts(path)
        lowered = [p.lower() for p in parts]
        for i in range(len(parts) - 3):
            if (lowered[i:i + 4] == ["atmosphere", "contents", PROGRAM_ID.lower(), "exefs.nsp"]
                    and i + 4 == len(parts)):
                roots.add("/".join(parts[:i]))
    return roots.pop() if len(roots) == 1 else None


def _release_destination(rel: str) -> Optional[str]:
    """SD-card path for one file of a release (relative to its SD root), or
    None when it is not emuiibo's -- a release is copied by what emuiibo is
    made of, not by whatever else happens to sit in the archive."""
    parts = _parts(rel)
    lowered = [p.lower() for p in parts]
    if lowered[:3] == ["atmosphere", "contents", PROGRAM_ID.lower()] and len(parts) > 3:
        return "/".join([SYSMODULE_DIR, *parts[3:]])
    if lowered == ["switch", ".overlays", "emuiibo.ovl"]:
        return OVERLAY_FILE
    if lowered[:2] == ["emuiibo", "overlay"] and len(parts) > 2 and lowered[2] != "favorites.txt":
        return "/".join([OVERLAY_DATA_DIR, *parts[2:]])
    return None


@dataclass(frozen=True)
class ReleasePlan:
    root: str
    files: tuple[tuple[str, str, int], ...]   # (source path in the tree, SD destination, size)
    overlay_source: Optional[str]              # the tree path of emuiibo.ovl, for its version

    def to_dict(self) -> dict:
        return {"files": len(self.files), "size": sum(size for _s, _d, size in self.files),
                "has_overlay": self.overlay_source is not None}


def plan_release(files: Iterable[tuple[str, int]]) -> Optional[ReleasePlan]:
    files = list(files)
    root = release_root(files)
    if root is None:
        return None
    planned = []
    overlay = None
    for path, size in files:
        if not _is_under(path, root) or path == root:
            continue
        rel = path[len(root) + 1:] if root else path
        dest = _release_destination(rel)
        if dest is None:
            continue
        planned.append((path, dest, size))
        if dest == OVERLAY_FILE:
            overlay = path
    return ReleasePlan(root=root, files=tuple(sorted(planned, key=lambda f: f[1])), overlay_source=overlay)


@dataclass(frozen=True)
class AmiiboSource:
    """One virtual amiibo in a collection, and where it goes."""
    source: str                               # its folder (or dump file) inside the tree
    dest: str                                 # its path under emuiibo/amiibo/ on the SD card
    files: tuple[tuple[str, int], ...]        # (path relative to `source`, size); ("", size) for a dump
    enabled: bool                             # has amiibo.flag -- emuiibo ignores it otherwise
    kind: str                                 # "virtual" | "dump"

    @property
    def group(self) -> str:
        """The folder it is listed under in the overlay ("" at the top)."""
        return _parent(self.dest)

    @property
    def label(self) -> str:
        return _parts(self.dest)[-1]

    def destinations(self) -> list[tuple[str, str, int]]:
        """(path in the tree, SD-card path, size) for every file of it."""
        if self.kind == "dump":
            size = self.files[0][1] if self.files else 0
            return [(self.source, f"{AMIIBO_DIR}/{self.dest}", size)]
        out = []
        for rel, size in self.files:
            src = f"{self.source}/{rel}" if self.source else rel
            out.append((src, f"{AMIIBO_DIR}/{self.dest}/{rel}", size))
        return out


@dataclass(frozen=True)
class Collection:
    amiibo: tuple[AmiiboSource, ...]

    def summary(self) -> dict:
        groups: dict[str, int] = {}
        for a in self.amiibo:
            top = _parts(a.dest)[0] if a.group else ""
            groups[top] = groups.get(top, 0) + 1
        return {
            "count": len(self.amiibo),
            "disabled": sum(1 for a in self.amiibo if not a.enabled),
            "dumps": sum(1 for a in self.amiibo if a.kind == "dump"),
            "groups": dict(sorted(groups.items())),
            "files": sum(len(a.files) for a in self.amiibo),
            "size": sum(size for a in self.amiibo for _rel, size in a.files),
        }


def _amiibo_folders(paths: list[str]) -> list[str]:
    """Outermost folders holding an amiibo.json. One inside another belongs
    to the outer one -- emuiibo keeps a converted amiibo's old files in a
    `v2/` or `v3/` folder inside it, amiibo.json included."""
    candidates = sorted({_parent(p) for p in paths if PurePosixPath(p).name.lower() == AMIIBO_JSON},
                        key=lambda f: (len(_parts(f)), f))
    outer: list[str] = []
    seen: set[str] = set()
    for folder in candidates:
        parts = _parts(folder)
        if "" in seen or any("/".join(parts[:i]) in seen for i in range(1, len(parts) + 1)):
            continue
        outer.append(folder)
        seen.add(folder)
    return outer


def find_collection(
    files: Iterable[tuple[str, int]], *, root_name: str = "", wrapper_name: Optional[str] = None,
) -> Optional[Collection]:
    """Every virtual amiibo in a tree, with its destination worked out, or
    None when there is none.

    Destinations keep the collection's own grouping (the real pack's
    `Animal Crossing/Isabelle` stays exactly that under emuiibo/amiibo/ --
    the overlay shows it as a folder) and drop only what is the collection
    itself rather than a group inside it: a folder named like the archive
    it was packed in (`wrapper_name`, "ALL amiibo (flag_json)/"), or a path
    that already spells the SD layout ("emuiibo/amiibo/..."). A raw dump
    always goes to the top of emuiibo/amiibo/, the only place emuiibo
    converts one. `root_name` names the tree's own root when the tree IS one
    amiibo's folder."""
    files = [(p, s) for p, s in files if _parts(p)]
    paths = [p for p, _s in files]
    folders = _amiibo_folders(paths)

    folder_set = set(folders)

    def in_amiibo(path: str) -> bool:
        # Ancestors looked up in a set: linear in the number of files, where
        # checking every file against every folder took a second for the
        # real 766-amiibo pack.
        parts = _parts(path)
        return "" in folder_set or any("/".join(parts[:i]) in folder_set for i in range(1, len(parts) + 1))
    dumps = [(p, s) for p, s in files if not in_amiibo(p) and looks_like_raw_dump(p, s)]
    dump_paths = {p for p, _s in dumps}
    if not folders and not dumps:
        return None

    foreign = [p for p in paths if not in_amiibo(p) and p not in dump_paths
               and PurePosixPath(p).name.lower() not in _IGNORABLE_FILES]
    impure: set[str] = {""} if foreign else set()
    for p in foreign:
        parts = _parts(p)
        for i in range(1, len(parts)):
            impure.add("/".join(parts[:i]))

    def collection_base(item: str) -> str:
        """The folder `item`'s destination is measured from: the folder
        after an `emuiibo/amiibo` the source already spells out, otherwise
        the topmost folder above it that holds nothing but amiibo. When
        even its own parent holds other things, that parent -- a
        collection of one."""
        parts = _parts(item)
        lowered = [p.lower() for p in parts]
        for i in range(len(parts) - 1, 0, -1):
            if lowered[i - 1:i + 1] == ["emuiibo", "amiibo"]:
                return "/".join(parts[:i + 1])
        base = _parent(item)
        if base in impure:
            return base
        while base and _parent(base) not in impure:
            base = _parent(base)
        return base

    def relative(item: str, base: str) -> str:
        return "/".join(_parts(item)[len(_parts(base)):])

    items = [f for f in folders if f] + [p for p, _s in dumps]
    bases = {item: collection_base(item) for item in items}

    def strip_wrapper(base: str) -> str:
        """A collection packed as one folder ("ALL amiibo (flag_json).7z"
        holding "ALL amiibo (flag_json)/") is that folder, not a group of
        it. Only a folder named like the archive is taken for that wrapper:
        a single "Animal Crossing/" inside "ac.zip" is a group, and stays."""
        if base or not wrapper_name:
            return base
        members = [i for i, b in bases.items() if b == base]
        firsts = {_parts(relative(i, base))[0] for i in members}
        if len(firsts) == 1:
            only = next(iter(firsts))
            if only.lower() == wrapper_name.lower() and only not in members:
                return only
        return base

    distinct = set(bases.values())
    stripped = {b: strip_wrapper(b) for b in distinct}
    prefix_with_base = len(distinct) > 1

    files_by_folder: dict[str, list[tuple[str, int]]] = {}
    for path, size in files:
        parts = _parts(path)
        owner = "" if "" in folder_set else next(
            ("/".join(parts[:i]) for i in range(1, len(parts)) if "/".join(parts[:i]) in folder_set), None)
        if owner is not None:
            files_by_folder.setdefault(owner, []).append(("/".join(parts[len(_parts(owner)):]), size))

    out: list[AmiiboSource] = []
    used: set[str] = set()
    for folder in folders:
        if folder:
            base = stripped[bases[folder]]
            dest = relative(folder, base)
            if prefix_with_base and base:
                dest = f"{_parts(base)[-1]}/{dest}"
        else:
            dest = root_name  # the tree is this one amiibo's own folder
        if not dest:
            continue
        dest = _unique(dest, used)
        own = tuple(sorted(files_by_folder.get(folder, [])))
        enabled = any(PurePosixPath(rel).name.lower() == AMIIBO_FLAG and "/" not in rel for rel, _s in own)
        out.append(AmiiboSource(source=folder, dest=dest, files=own, enabled=enabled, kind="virtual"))
    for path, size in dumps:
        dest = _unique(PurePosixPath(path).name, used)
        out.append(AmiiboSource(source=path, dest=dest, files=(("", size),), enabled=True, kind="dump"))
    if not out:
        return None
    return Collection(amiibo=tuple(sorted(out, key=lambda a: a.dest.lower())))


def _unique(dest: str, used: set[str]) -> str:
    """emuiibo's own rule for a name already taken: `<name>_1`, `<name>_2`."""
    candidate, index = dest, 0
    while candidate.lower() in used:
        index += 1
        stem, dot, ext = dest.rpartition(".") if dest.lower().endswith(".bin") else (dest, "", "")
        candidate = f"{stem}_{index}{dot}{ext}"
    used.add(candidate.lower())
    return candidate


# emuiibo's own PC tools: emutool (C#, up to 1.0) and emuiigen (Java, 1.1+),
# which make virtual amiibo from AmiiboAPI's list. They run on a PC.
_PC_TOOLS = {"emutool.exe": "emutool", "emuiigen.jar": "emuiigen"}


def pc_tool(files: Iterable[tuple[str, int]]) -> Optional[str]:
    """'emutool' / 'emuiigen' when a tree is one of emuiibo's PC tools,
    else None."""
    for path, _size in files:
        name = PurePosixPath(path).name.lower()
        if name in _PC_TOOLS:
            return _PC_TOOLS[name]
    return None


def select(collection: Collection, wanted: Optional[Iterable[str]]) -> list[AmiiboSource]:
    """The amiibo of `collection` a selection names: each entry is an amiibo's
    destination, or a group (folder) whose every amiibo is meant. None means
    all of them."""
    if wanted is None:
        return list(collection.amiibo)
    keys = {w.strip("/").lower() for w in wanted if w and w.strip("/")}
    return [a for a in collection.amiibo
            if any(a.dest.lower() == k or a.dest.lower().startswith(k + "/") for k in keys)]


# ---------------------------------------------------------------------------
# The console: reading what is there
# ---------------------------------------------------------------------------

@dataclass
class DeviceAmiibo:
    path: str                        # relative to emuiibo/amiibo/
    enabled: bool                    # amiibo.flag present
    save_data: bool                  # an areas/ folder -- game progress lives in it
    json_size: Optional[int]
    info: Optional[AmiiboInfo] = None
    kind: str = "virtual"            # "virtual" | "dump" (waiting for emuiibo to convert it at boot)

    @property
    def valid(self) -> bool:
        return self.kind == "dump" or self.info is not None

    def to_dict(self) -> dict:
        info = self.info
        return {
            "path": self.path, "enabled": self.enabled, "save_data": self.save_data,
            "json_size": self.json_size, "kind": self.kind,
            "name": info.name if info else None,
            "amiibo_id": info.id.hex if info else None,
            "id": [info.id.game_character_id, info.id.character_variant, info.id.figure_type,
                   info.id.model_number, info.id.series] if info else None,
            "uuid": list(info.uuid) if info and info.uuid else None,
            "use_random_uuid": info.use_random_uuid if info else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceAmiibo":
        info = None
        if d.get("id") is not None and d.get("name") is not None:
            gc, var, ft, model, series = d["id"]
            info = AmiiboInfo(
                name=d["name"], id=AmiiboId(gc, var, ft, model, series),
                uuid=tuple(d["uuid"]) if d.get("uuid") else None,
                use_random_uuid=d.get("use_random_uuid"), mii_charinfo_file="",
            )
        return cls(path=d["path"], enabled=bool(d.get("enabled")), save_data=bool(d.get("save_data")),
                   json_size=d.get("json_size"), info=info, kind=d.get("kind") or "virtual")


@dataclass
class DeviceState:
    components: dict[str, bool] = field(default_factory=dict)
    overlay_version: Optional[str] = None
    emulation_on: Optional[bool] = None
    library_exists: bool = False
    amiibo: list[DeviceAmiibo] = field(default_factory=list)
    # The overlay's favorites, as paths relative to emuiibo/amiibo/.
    favorites: list[str] = field(default_factory=list)
    complete: bool = False
    error: Optional[str] = None

    @property
    def missing(self) -> list[Component]:
        return [c for c in COMPONENTS if not self.components.get(c.key)]

    @property
    def installed(self) -> bool:
        return bool(self.components.get("sysmodule"))

    def by_path(self) -> dict[str, DeviceAmiibo]:
        return {a.path.lower(): a for a in self.amiibo}

    def to_dict(self) -> dict:
        return {
            "components": dict(self.components), "overlay_version": self.overlay_version,
            "emulation_on": self.emulation_on, "library_exists": self.library_exists,
            "favorites": list(self.favorites), "complete": self.complete, "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict, amiibo: list[DeviceAmiibo]) -> "DeviceState":
        return cls(components=dict(d.get("components") or {}), overlay_version=d.get("overlay_version"),
                   emulation_on=d.get("emulation_on"), library_exists=bool(d.get("library_exists")),
                   amiibo=amiibo, favorites=list(d.get("favorites") or []),
                   complete=bool(d.get("complete")), error=d.get("error"))


def _entries(backend, storage: str, path: str) -> Optional[dict[str, object]]:
    listing = backend.list_directory(storage, path)
    if listing is None:
        return None
    return {entry.name.lower(): entry for entry in listing}


def read_device(
    backend, storage: str, *, known: Optional[dict[str, DeviceAmiibo]] = None,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> Iterator[DeviceState]:
    """Reads emuiibo's state off a console, one MTP request at a time.

    A generator: it yields the (partial) state after every request, so the
    caller -- the worker thread, the only one allowed to talk to the device
    -- can stop after a time budget and carry on with the SAME generator on
    a later pass, never holding up an install for a minute of amiibo
    listing. The last state yielded has complete=True.

    `known` is the previous read's amiibo by lower-cased path: one whose
    amiibo.json still has the same size is not read again (the listing
    already said so), which turns a re-read of 766 amiibo into 766 folder
    listings and next to no file reads."""
    state = DeviceState()
    known = known or {}

    contents = _entries(backend, storage, "atmosphere/contents") or {}
    yield state
    overlays = _entries(backend, storage, "switch/.overlays") or {}
    yield state
    flags = {}
    for program_dir, key in ((SYSMODULE_DIR, "sysmodule"), (OVLLOADER_DIR, "ovlloader")):
        program_id = program_dir.rsplit("/", 1)[-1].lower()
        if program_id in contents:
            own = _entries(backend, storage, program_dir) or {}
            flag_dir = _entries(backend, storage, f"{program_dir}/flags") if "flags" in own else {}
            flags[key] = "exefs.nsp" in own and "boot2.flag" in (flag_dir or {})
            yield state
        else:
            flags[key] = False
    state.components = {
        "sysmodule": flags["sysmodule"],
        "overlay": "emuiibo.ovl" in overlays,
        "ovlloader": flags["ovlloader"],
        "tesla_menu": "ovlmenu.ovl" in overlays,
    }
    if state.components["overlay"]:
        data = backend.read_file(storage, OVERLAY_FILE, max_bytes=OVERLAY_MAX_BYTES)
        state.overlay_version = overlay_version(data) if data else None
        yield state

    base = _entries(backend, storage, BASE_DIR)
    if base is not None:
        flags_dir = _entries(backend, storage, f"{BASE_DIR}/flags") if "flags" in base else None
        state.emulation_on = None if flags_dir is None else "status_on.flag" in flags_dir
    state.library_exists = base is not None and "amiibo" in base
    if base is not None and "overlay" in base:
        overlay_dir = _entries(backend, storage, OVERLAY_DATA_DIR) or {}
        if "favorites.txt" in overlay_dir:
            data = backend.read_file(storage, FAVORITES_FILE, max_bytes=FAVORITES_MAX_BYTES)
            state.favorites = parse_favorites(data or b"")
    yield state

    pending = [""] if state.library_exists else []
    seen = 0
    while pending:
        rel = pending.pop()
        here = _entries(backend, storage, f"{AMIIBO_DIR}/{rel}" if rel else AMIIBO_DIR) or {}
        seen += 1
        if progress is not None:
            progress(seen, None)
        if rel and AMIIBO_JSON in here:
            json_entry = here[AMIIBO_JSON]
            size = getattr(json_entry, "size", None)
            previous = known.get(rel.lower())
            if previous is not None and previous.json_size == size and previous.info is not None:
                info = previous.info
            else:
                data = backend.read_file(storage, f"{AMIIBO_DIR}/{rel}/{AMIIBO_JSON}",
                                         max_bytes=AMIIBO_JSON_MAX_BYTES)
                info = parse_amiibo_json(data) if data else None
            save = here.get(SAVE_DATA_DIR)
            state.amiibo.append(DeviceAmiibo(
                path=rel, enabled=AMIIBO_FLAG in here, json_size=size, info=info,
                save_data=bool(save is not None and getattr(save, "is_dir", False)),
            ))
            yield state
            continue
        for entry in here.values():
            child = f"{rel}/{entry.name}" if rel else entry.name
            if entry.is_dir:
                pending.append(child)
            elif not rel and looks_like_raw_dump(entry.name, entry.size or 0):
                state.amiibo.append(DeviceAmiibo(path=child, enabled=True, save_data=False,
                                                 json_size=None, kind="dump"))
        yield state
    state.amiibo.sort(key=lambda a: a.path.lower())
    state.complete = True
    yield state


_FAVORITE_PREFIX = f"sdmc:/{AMIIBO_DIR}/"
FAVORITES_MAX_BYTES = 1024 * 1024


def parse_favorites(data: bytes) -> list[str]:
    """favorites.txt -> amiibo paths relative to emuiibo/amiibo/. The overlay
    writes one full `sdmc:/emuiibo/amiibo/...` path per line."""
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.lower().startswith(_FAVORITE_PREFIX.lower()) and len(line) > len(_FAVORITE_PREFIX):
            out.append(line[len(_FAVORITE_PREFIX):])
    return out


def favorites_without(data: bytes, removed: Iterable[str]) -> Optional[bytes]:
    """favorites.txt with every line naming a removed amiibo (or anything
    inside a removed folder) dropped -- or None when nothing changes. Every
    other line is kept exactly as the overlay wrote it."""
    gone = [f"{_FAVORITE_PREFIX}{r}".lower() for r in removed]
    lines = data.decode("utf-8", "replace").splitlines()
    kept = [line for line in lines
            if not any(line.strip().lower() == g or line.strip().lower().startswith(g + "/") for g in gone)]
    if len(kept) == len(lines):
        return None
    return "".join(line + "\n" for line in kept).encode("utf-8")


def manifest_units(dests: Iterable[str]) -> dict[str, list[str]]:
    """An AMIIBO job's files grouped into the amiibo they make up: key is the
    amiibo's SD path (its folder, or a dump's own file), value every
    destination of it. A virtual amiibo is its folder -- the unit a job
    decides about -- never one file of it."""
    dests = list(dests)
    # The outermost folder, as in find_collection: a v2/ inside a converted
    # amiibo is part of it, not an amiibo of its own.
    folders = _amiibo_folders(dests)
    units: dict[str, list[str]] = {}
    for dest in dests:
        owner = next((f for f in folders if _is_under(dest, f) and dest != f), None)
        units.setdefault(owner or dest, []).append(dest)
    return units


def removal_order(backend, storage: str, root: str) -> list[str]:
    """Every file under the SD folder `root`, then every folder of it
    (itself last), deepest first -- the only order a tree can be removed in
    one object at a time over MTP."""
    if not is_inside_amiibo_dir(root):
        raise ValueError(f"refusing to remove anything outside {AMIIBO_DIR}/: {root!r}")
    out_files: list[str] = []
    out_dirs: list[str] = [root]
    pending = [root]
    while pending:
        current = pending.pop()
        for entry in backend.list_directory(storage, current) or []:
            child = f"{current}/{entry.name}"
            if entry.is_dir:
                out_dirs.append(child)
                pending.append(child)
            else:
                out_files.append(child)
    return out_files + sorted(out_dirs, key=lambda d: -len(_parts(d)))


def is_inside_amiibo_dir(sd_path: str) -> bool:
    """An SD path strictly INSIDE emuiibo/amiibo/ -- never the folder itself,
    never anything next to it. Every removal is checked against this before
    a single object is deleted."""
    if "\\" in sd_path or sd_path.startswith("/"):
        return False
    parts = sd_path.split("/")
    if any(p in ("", ".", "..") or ":" in p for p in parts):
        return False
    base = AMIIBO_DIR.split("/")
    return len(parts) > len(base) and [p.lower() for p in parts[:len(base)]] == base
