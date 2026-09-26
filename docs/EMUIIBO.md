# emuiibo and virtual amiibo

What SwitchAgent knows about emuiibo, where that knowledge came from, and
what it does with it. The code is `switchagent/emuiibo.py` (the knowledge),
`web/emuiibo_service.py` (reading and changing a console, on the worker
thread) and `web/amiibo_views.py` + the **Amiibo** page (showing it).

## Sources

Studied 2026-09-25:

- emuiibo's own source, XorTroll/emuiibo `master` at **1.1.3** (released
  2026-03-14): `emuiibo/src/amiibo/fmt.rs` (the virtual amiibo format),
  `compat.rs` + `v1/v2/v3.rs` (old formats it converts), `fsext.rs` and
  `emu.rs` (its SD folders and flags), `overlay/source/main.cpp` (browsing,
  favorites, the version check), the `Makefile` (what a release contains).
- A real release as it is passed around, `Animal Crossing New Horizons [NSP]
  / Amiibo JSON for Emuiibo/`: `emuiibo-v0.6.3.zip`, `emutool-v0.6.3.zip`,
  `ALL amiibo (flag_json).7z` (766 virtual amiibo, all 766 valid by emuiibo's
  own rules, 207 with names longer than the 10 characters games show).

## What has to be on the console

| Part | SD card | From | Why |
|---|---|---|---|
| emuiibo sysmodule | `atmosphere/contents/0100000000000352/exefs.nsp` + `flags/boot2.flag` (+ `toolbox.json`) | emuiibo release | answers games' amiibo requests with the active virtual amiibo |
| emuiibo overlay | `switch/.overlays/emuiibo.ovl` (1.x also `emuiibo/overlay/lang/*.json`) | the **same** release | the only way to pick the active amiibo; refuses any other emuiibo version than its own |
| nx-ovlloader | `atmosphere/contents/420000000007E51A/exefs.nsp` + `flags/boot2.flag` | ppkantorski/nx-ovlloader, inside Ultrahand's release | loads overlays |
| overlay menu | `switch/.overlays/ovlmenu.ovl` | Ultrahand Overlay (Tesla Menu, unmaintained since 2023, used the same file) | opens overlays: L + D-pad Down + R3 |

Atmosphère is assumed -- DBI's MTP responder is running on it. emuiibo
reads everything at boot: after copying anything, the console has to be
restarted before emuiibo sees it.

SwitchAgent installs emuiibo itself when an emuiibo release is in the
Library (recognised by its sysmodule, never by name -- `emuiibo.zip` of
1.x has no version in its name; the version is read from the overlay's
own NACP). When the Library holds no **current** release, the Amiibo page
offers "Download and install" instead (`switchagent/emuiibo_download.py`,
`web/emuiibo_service.ReleaseDownloader`):

- one plain GET to GitHub's API for the latest release of
  `XorTroll/emuiibo`; only its asset named exactly `emuiibo.zip` (never
  `emuiigen.jar`, a PC app), only from GitHub's own hosts, redirects
  included;
- no bigger than 32 MB and exactly the size the API declared, the SHA-256
  GitHub publishes for the asset checked (1.1.3 has one), and it has to be
  an emuiibo release by structure -- or it is refused and nothing is kept;
- written as `.part` and renamed only once verified, into
  `<first Library folder>/SwitchAgent downloads/emuiibo-v<version>.zip`,
  indexed at once like any Library file and handed to the ordinary queue
  for the chosen console. A release of that version already in the
  Library is installed as it is, not downloaded again.

Nothing fetched is ever run on this PC. When the console lacks an overlay
menu, Ultrahand (which carries nx-ovlloader) is installed first, in the same
click; a Tesla Menu already there does the same job and is left alone.

An emuiibo release is installed by what emuiibo is made of -- the three
places above -- and nothing else from the archive. Its own program files
are replaced whatever is there: updating emuiibo means exactly that.
Amiibo and their save data are not touched.

## What lives on the SD card

- `emuiibo/amiibo/` -- the library. A virtual amiibo is a **folder**, at any
  depth (sub-folders are the overlay's own grouping), that holds
  `amiibo.json` **and** `amiibo.flag`. Without the flag emuiibo ignores it
  -- that is emuiibo's own way to hide one.
- `amiibo.json`: `name`, `id` (`game_character_id` byte-swapped relative to
  AmiiboAPI's head/tail, `character_variant`, `figure_type`, `model_number`,
  `series`), `uuid` (10 bytes), `use_random_uuid`, `mii_charinfo_file`,
  dates, `write_counter`, `version`. `uuid` and `use_random_uuid` may be
  missing (0.6-era packs); emuiibo fills them in on first load.
- On first use emuiibo writes into the folder itself: `areas.json`, a random
  mii as `mii-charinfo.bin`, the missing JSON fields, and per-game save data
  as `areas/0x<access id>.bin`. **A folder with `areas/` holds somebody's
  progress.**
- Raw NTAG215 dumps (`*.bin`, 532/540/572 bytes) and 0.3-0.4-era folders
  placed **directly** in `emuiibo/amiibo/` (never deeper) are converted at
  boot into the format above; the dump is moved inside the new folder.
- `emuiibo/overlay/favorites.txt` -- one full `sdmc:/emuiibo/amiibo/...`
  path per line. `emuiibo/flags/status_on.flag` -- emulation on after a
  restart. `emuiibo/miis/` -- the console's miis, re-exported every boot.

## A collection in the Library

An archive or a folder holding virtual amiibo is one Library item
(`ContentType.AMIIBO`), found by structure: folders with an `amiibo.json`
(outermost -- a converted amiibo keeps its old `v2/amiibo.json` inside
itself) and raw dumps. An unpacked collection is the topmost folder that
holds nothing but amiibo -- `ALL amiibo (flag_json)`, not the release folder
it sits in next to two archives.

Where each amiibo goes, under `emuiibo/amiibo/`: its path inside the
collection, keeping the collection's own grouping (`Animal Crossing/Isabelle`
stays exactly that, and so does `Animal Crossing/Spork/Crackle`). Dropped:
a folder named like the archive it was packed in (`ALL amiibo
(flag_json).7z` → `ALL amiibo (flag_json)/`), and anything up to an
`emuiibo/amiibo/` the source already spells out. A raw dump always goes to
the top, the only place emuiibo converts one.

**Which game it came with** (`scanner.amiibo_owners`): a `[TITLE_ID]` in the
collection's own name; otherwise its release folder -- the nearest folder
above it holding any game. One game there owns it; several, the one whose
package lies directly in that folder (Animal Crossing's release keeps the
game, its update and a DLC at the top and the Island Transfer Tool in a
sub-folder). Still several: nobody. An owned collection is a part of its
game's card -- an "N amiibo" tag next to updates/DLC/mods, a row among its
parts, a section on its game page -- and selecting the game installs it
after the game. One without a game is not shown among the games at all: it
is on the Amiibo tab, and Library says in one line how many there are
(before there is an Amiibo tab, that line points to where emuiibo is
installed from). emuiibo's own release files are not shown anywhere: the
Amiibo page offers one button when emuiibo needs installing or updating.

emuiibo's PC tools -- emutool (C#, up to 1.0) and emuiigen (Java, 1.1+),
which make virtual amiibo from AmiiboAPI's list -- are recognised as **not
for Switch**: off the grid, never something to review, not shown.

## Installing amiibo

Through the ordinary queue: one frozen manifest per collection, verified
before sending, one file at a time over the existing WPD transport. An
AMIIBO manifest can only ever name destinations inside `emuiibo/amiibo/`
(checked when it is built), an EMUIIBO manifest only emuiibo's own three
places.

The whole collection, or a selection -- single amiibo or whole folders --
from the Amiibo page ("Install N not on this Switch", "Install selected").

A job decides per **amiibo**, never per file, because emuiibo rewrites an
amiibo's folder as soon as it is used, so an amiibo installed yesterday no
longer matches its source byte for byte:

- its folder is free → sent;
- it holds the **same** amiibo (figure and UUID, read back off the console)
  → already there, left exactly as it is, its save data included;
- it holds a **different** amiibo → left untouched under Settings' default
  "skip" policy; under "override" that folder is removed first and the new
  amiibo written in its place (nothing of the old one, its save data least
  of all, ends up mixed into the new one).

The rest of the collection is installed either way; History says how many
were added, already there, replaced or left alone.

**emuiibo with them.** Amiibo do nothing on a console without emuiibo. When
Library's install confirmation holds amiibo and the target Switch lacks
emuiibo (`GET /api/amiibo/emuiibo/offer`, from its last read), the
confirmation offers "Also install emuiibo": ticked when the console is known
to lack it, unticked when it was never read, absent while a download of it
runs or an install of it is queued. Confirming downloads the current release
from GitHub (the same downloader as Add-ons' Install), queues it, then
queues the selection. If the download fails nothing is queued, and the
dialog says so -- installing the amiibo without emuiibo is an untick away,
never a silent fallback.

**The Amiibo tab** exists only once emuiibo does -- on some console as of
its last read, or delivered there by SwitchAgent since
(`amiibo_views.amiibo_tab_visible`). Before that `/amiibo` redirects to the
emuiibo entry on Add-ons, and Library's line about amiibo off the grid
points there too.

## Reading a console

On the worker thread only (every MTP call has to come from it), when a
console connects, after every emuiibo/amiibo job on it, and on "Read again".
In slices of about a second between jobs: a read is a generator that yields
after every MTP request, so a 766-amiibo console (~800 folder listings) never
holds up an install. Stored per device (`device_emuiibo`, `device_amiibo`) and
kept across disconnects, with the time it was read. A second read does not
re-read an `amiibo.json` whose size has not changed.

What it reads: which of the four parts are there (a sysmodule without its
`boot2.flag` does not count -- it would not start), the overlay's version
from its NACP, emulation on/off, the favorites, and every amiibo: its name
and figure id, whether it has `amiibo.flag`, whether it holds save data,
whether emuiibo could even parse it, and raw dumps still waiting to be
converted.

## Removing amiibo

From the Amiibo page: single amiibo or whole folders. Not a queue job (the
jobs table needs a Library item, and a removal has none), but:

- limited to `emuiibo/amiibo/` -- `emuiibo.is_inside_amiibo_dir`, checked
  for the request and again for every single object before it is deleted;
- refused for a console that is not connected, or for an amiibo not in its
  last read;
- refused for an amiibo with save data unless that was explicitly
  confirmed (the dialog asks, the API requires `confirm_save_data`);
- one object at a time, files first, folders bottom-up, each deletion
  verified by a fresh listing of its folder, each one logged;
- afterwards the overlay's favorites lose the lines that named what was
  removed, every other line kept exactly as it was.

The Library is never touched: a removed amiibo can be installed again.

## Over DBI's MTP responder

What SwitchAgent does to the SD card, through WPD (`mtp/wpd.py`):

| Operation | WPD | Used for |
|---|---|---|
| list a folder (names, file/folder, sizes) | `EnumObjects` + properties | reading a console |
| read a small file | `IPortableDeviceResources::GetStream` | `amiibo.json`, the overlay's NACP, `favorites.txt` |
| create a folder, write a file | `CreateObjectWithPropertiesOnly/AndData` | installing (unchanged, proven) |
| replace a file | Shell copy engine (unchanged) | an emuiibo update, `favorites.txt` |
| delete one object | `IPortableDeviceContent::Delete`, no recursion | removing an amiibo |

Listing, reading and deleting are WPD-only (the Shell fallback cannot read
a file back without a copy, nor delete without a dialog); without a WPD
session they refuse rather than guess.

**On a real console** (OLED, DBI MTP responder, 2026-09-25): a 0-byte
file (`amiibo.flag`) writes and size-verifies like any other; an amiibo
installed through the queue reads back with its name and figure id;
`IPortableDeviceContent::Delete` removes files and empty folders, 0.13s an
object including the verifying listing. Two things DBI does that the
verification exists for:

- right after its MTP responder had hung and been restarted, one delete
  took 85s to be answered (and then was done) -- the worker waits, it does
  not lose track;
- in the session right after a client was killed rather than closed, DBI
  answered S_OK to deletes it did not carry out -- a fresh listing still
  showed the files. The listing after every delete is what catches that;
  a delete is never reported done on DBI's word alone.

## Not done, deliberately

- Enabling/disabling an amiibo by its flag is not offered; installing and
  removing are.
- A raw dump sent to a console is converted by emuiibo into a folder named
  after the amiibo; SwitchAgent cannot tell that folder came from that dump,
  so installing the same dump again would make emuiibo convert it again.
- `switch/key_retail.bin` (needed for emuiibo to take a dump's save data and
  mii along when converting) is never provided -- those are console keys.
