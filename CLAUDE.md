# SwitchAgent — working notes

## Releases

A push to `main` **is** a release. `.github/workflows/release-windows.yml`
derives the tag from `pyproject.toml`'s `version`, builds the installer and
portable zip on a Windows runner, and publishes them. Pushing to `main`
with a version that already has a release **re-publishes** that release:
the old one is deleted and recreated, which resets its download count. Fine
for fixing a release page minutes after the fact; worth knowing before
pushing a doc typo to a release that has been up for a week.

To cut a new version: bump `version` in `pyproject.toml`, add its section
to `CHANGELOG.md`, commit, push.

## CHANGELOG.md is the release body

The workflow copies the `## v<X.Y.Z>` section verbatim into the GitHub
Release, then appends the auto-generated `**Full Changelog**` compare link.
There is nowhere else release notes are written. A tag with no section
falls back to generated notes only — a release is never blocked on prose.

### How to write an entry

**One sentence per change. Three to five lines for a normal release.**

The reader is someone on the releases page deciding whether to download
this. The only question they have is *what is different now*.

- **Only what a user would notice.** If a change cannot be seen from the
  UI, it does not belong here. Cache keys, refactors, a fixed internal
  race, test coverage — all real work, none of it a release note.
- **No reasoning, no cause, no history.** Not why it broke, not how it was
  fixed, not what it used to do. That belongs in the commit message, which
  is where anyone asking those questions will look.
- **Fold related fixes into one line.** Four separate bugs that all made
  the library show the wrong thing are one sentence, not four.
- **No bold lead-ins, no sub-bullets, no paragraphs.** A flat list of plain
  sentences.

Good:

```markdown
## v1.0.12

- The library shows what is actually on disk: no duplicate entries after a
  folder's path changes, no games missing after re-adding one.
- A scan can be stopped, and changing Library folders no longer waits for
  one to finish.
- The SD card space bar works with no Switch connected.
```

Bad — this is the same release, and it is an inventory of the work rather
than a summary of it:

```markdown
- **One file is one row again.** Pointing a Library folder at the same
  files by another path -- `D:\shared\Download` to `\\192.168.50.2\...`,
  a new drive letter, a renamed parent folder -- used to re-index
  everything as new and leave the old rows behind, so a single game
  counted as two ("Base game: present (2 copies)").
- **Static assets are versioned by content.** Two of the fixes above
  shipped briefly invisible for exactly that reason.
```

If a section runs past ~500 characters, it is describing the work instead
of the result.

## Commits

Commit as the repository owner alone. **No `Co-Authored-By` trailer** for
Claude or any other tool.

Commit messages are the opposite of changelog entries: that is where the
cause, the reasoning, the dead ends and the measurements go, at whatever
length the change earns.

## History is a journal, not a pulpit

**Nothing on the History page changes anything.** No install confirmation, no
Override / Skip / Send again, no dismiss-or-hide. Decisions live in Queue;
what is on a console lives in Devices. This is a product decision, not an
oversight — do not add a control there because it would be convenient.

The line, when it is unclear: *the page may change what you are looking at;
it may not change what happened, or what will.* Search, filters, day
grouping, a `<details>` fold, pagination and links to another page are all
fine. Anything that writes — including "mark as read" — is not: a journal
you can edit stops being a record.

The one link it is allowed is an **address, not a control**: a stuck row says
`Still waiting in Queue →`, rendered as a text link rather than a `.btn`,
worded as a state rather than an order, and shown only while there is
actually something in Queue to decide. Reporting a problem and staying silent
about where it gets resolved is worse than not reporting it.

### Why the confirmation prompt was deleted

It asked "did this install?" on every unverified row. In a real library that
was 55 questions and **0 answers, ever**. The answer lives on another device,
costs a walk to the console, and buys nothing once given — the row changes
one caption for another. Eighteen retries of one mod asked eighteen times
about one file. `POST /api/history/{id}/verification` and
`set_history_verification()` are still correct and still tested; they simply
have no caller in the UI.

### What the page owes the reader

Two questions, and it is not asked to do more:

1. *"I pressed Install — did it arrive?"* — most visits, minutes old.
2. *"Why is this game not on the Switch?"* — the stuck ones.

One row per **title**, not per transfer: a worker that retried the same mod
18 times is one fact to a person, not eighteen (86 rows for 19 things is what
this replaced). A row states its latest outcome, and carries what it took to
get there — dropping the earlier refusals would hide exactly what someone
came to find out.

Local time, never the stored UTC. Human wording from `classify_activity()`,
never a raw enum. The same name Library shows, via the same
`strip_release_tags` — one object must not read as two different things
depending on which page you are on.

## A game is its folder: SD files

A homebrew port ships as a small **forwarder** `.nsp` (installed through DBI,
it only puts an icon on the home menu) plus a **`switch/` folder** that has to
land on the SD card verbatim — the forwarder launches e.g.
`sdmc:/switch/zumaportable/dbc17o.nro`, stored in plain text inside it. The
`switch/` folder comes as an archive (`switch.7z`) or already unpacked
(`Homebrew (1.0.0)/switch/...`), and a port can need both (Mega Man X
Regenesis: data in the archive, the `.nro` in the unpacked folder). All of it
is `ContentType.SD_FILES`; the rules live in `switchagent/sd_files.py`.

What must stay true — `tests/test_sd_files.py` encodes each of these against
the real Zuma / Mega Man layouts, so change a test on purpose or not at all:

- **It belongs to its game, never a card called "switch".** Owner, in order:
  a `[TITLE_ID]` in its own name; a forwarder in the library that launches an
  `.nro` inside it; the nearest folder above it holding any game — only if
  that is exactly one game. Two games in one folder: it stays its own card
  (named after the folder), never guessed onto either.
- **Only `switch/`, only verbatim.** Every destination is `switch/<path as in
  the release>`; nothing else of a release (a README beside it) is copied to
  the card. A `switch` folder holding packages is a downloads category, one
  under `atmosphere/` belongs to a mod, one deeper than a single wrapper folder
  inside an archive is game data — none of them is SD content.
- **Nothing an archive ships silently stays behind.** A package or mod archive
  that also carries `switch/` gets one more job for it.
- **Bump `scanner.CLASSIFICATION_REVISION` whenever classification learns to
  recognise something new.** Unchanged files are otherwise never looked at
  again, and the change would not reach the very libraries it was made for.
- The forwarder's launch path is checked against the game's own SD files; a
  missing `.nro` is said on the card, not discovered on the console.
- DBI's installed list cannot confirm SD files (like mods): no "On Switch" for
  them, and DBI itself only refreshes that list when reopened on the console.
- **Thousands of small files over MTP must survive a console that stalls.**
  One timed-out write reopens the WPD session and retries that file; it does
  not move the rest of the job to the Shell (25x slower). Retry continues
  from what the previous attempt delivered, and a file already on the card
  with exactly the same bytes (read back, hashed) is done, not a conflict.

## A virtual amiibo is its folder: emuiibo

emuiibo (`atmosphere/contents/0100000000000352` + `switch/.overlays/emuiibo.ovl`)
answers games' amiibo requests from `emuiibo/amiibo/`, where a virtual amiibo
is a folder holding `amiibo.json` and `amiibo.flag`. The knowledge lives in
`switchagent/emuiibo.py`, the full picture in `docs/EMUIIBO.md`;
`tests/test_emuiibo.py` encodes each rule below against the real 766-amiibo
pack's layout.

- **Recognised by structure, never by name.** A release is its sysmodule's
  `exefs.nsp`; its version is the overlay's own NACP. An `.nsp` inside
  `atmosphere/contents/<id>/` is never a package to install.
- **Copied by what it is, nowhere else.** An AMIIBO manifest can only name
  `emuiibo/amiibo/...`; an EMUIIBO one only emuiibo's own three places.
  Checked when the manifest is built.
- **Decided per amiibo, never per file.** emuiibo rewrites an amiibo as soon
  as it is used, so bytes cannot say "already there": the same figure and
  UUID at the same place is the same amiibo, and it is left as it is, save
  data and all. A different one in that folder is left alone unless the
  conflict policy says override -- then its folder goes first, whole.
- **A collection that came with a game is part of it**: an "N amiibo" tag on
  the game's card, installed after the game when the game is selected. Owner
  by `[TITLE_ID]` in its name, else the one game of its release folder -- or,
  with several there, the one whose package lies directly in that folder.
  Otherwise nobody, never a guess. emuiibo releases, unowned collections and
  PC tools are not games: they live on the Amiibo tab, not in the grid.
- **No Amiibo tab before emuiibo.** It exists once some console's last read
  has emuiibo, or SwitchAgent delivered it there since
  (`amiibo_views.amiibo_tab_visible`); until then `/amiibo` redirects to
  Add-ons, where emuiibo is installed. Installing amiibo onto a console
  without it offers emuiibo's download and install in the same confirmation
  -- ticked when the console is known to lack it, unticked when it was never
  read. A failed download queues nothing; installing without emuiibo is the
  user's untick, never a silent fallback.
- **The one thing ever removed from a console** is an amiibo folder under
  `emuiibo/amiibo/`, explicitly, from the Amiibo page: checked path by path,
  one object at a time, each deletion proven by a fresh listing, save data
  only with an explicit yes. Nothing else anywhere deletes anything.
- **Every MTP call from the worker thread, in slices.** Reading a console is
  a generator the worker advances between jobs; an install never waits
  behind a read of 800 folders.
- emuiibo's current release can be fetched from GitHub on an explicit click
  (`emuiibo_download.py`): only `emuiibo.zip` of XorTroll/emuiibo, only from
  GitHub's hosts, size and published SHA-256 checked, emuiibo by structure,
  into a Library folder -- then the ordinary queue. Nothing fetched is run.
- Tesla (nx-ovlloader + Tesla Menu) is required and never installed -- the
  page says it is missing and where it comes from.

## Add-ons: a curated catalog, not an installer

The Add-ons tab lists open-source Switch utilities from
`switchagent/web/addons/catalog.yaml`, in file order -- what each is, how it
works, how it gets onto a console, how to use it. Entries are added by hand;
the file's header lists every field, and `tests/test_addons.py` refuses an
entry the tab could not show truthfully (a `requires` naming nothing, a
`status` check or `install` SwitchAgent does not have, a missing preview).

- A status line appears only where SwitchAgent really checks the console
  (`status:`), as of that console's last read -- never a guess.
- An Install button appears only where SwitchAgent installs it itself
  (`install:`, today only emuiibo, through the ordinary queue). Everything
  else is instructions and a link to the project's own releases.
- A preview is a small honest picture in `web/static/`; the emuiibo one is an
  illustration drawn from the overlay's own labels, said so in its caption.
