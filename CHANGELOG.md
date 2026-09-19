# Changelog

Each `## vX.Y.Z` section below becomes the body of that version's
[GitHub Release](../../releases), so this file is the one place a change
gets written up for the people who arrive to download it.

## v1.0.12

**The library now says what is actually on disk.** Everything here came out
of one session of using the app and finding it disagreeing with reality.

- **One file is one row again.** Pointing a Library folder at the same
  files by another path -- `D:\shared\Download` to
  `\\192.168.50.2\shared\Download`, a new drive letter, a renamed parent
  folder -- used to re-index everything as new and leave the old rows
  behind, so a single game counted as two ("Base game: present (2
  copies)").
- **Re-adding a folder brings its games back.** Remove a Library folder and
  add it straight back and most of it stayed invisible: every file is
  byte-identical, so the "unchanged" shortcut skipped past re-indexing
  while the rows still said the files had left.
- **Rows kept only as a record stop being shown as games.** A file the
  library no longer holds, but that an install job still references, is
  kept so Queue and History keep their names -- it is no longer rendered
  as installable content.
- **Queue draws one row per file.** A file being prepared ahead of its turn
  was drawn twice, for as long as the previous transfer took.

**Scanning is no longer something you have to wait out.**

- A running scan can be **stopped** ("Stop scanning"). It keeps whatever it
  already indexed and never removes anything on the strength of a
  half-finished walk.
- Saving the Library folders **cancels** a running scan instead of refusing
  to save. Previously the one action that would end an unwanted scan was
  the one action that scan blocked.
- The **last** folder can be removed. SwitchAgent then watches nothing and
  clears its index; your files are untouched.
- A fresh install no longer adopts your Downloads folder unasked. The
  detected path is written into `config.yaml` commented out, as the
  suggestion it always was, and nothing is scanned until you choose.

**Other**

- The SD card space bar keeps working with **no Switch connected**,
  estimating against an assumed empty 256 GB card so you can see what a
  selection needs while packing for a trip.
- Static assets are versioned by content instead of by hand. Two of the
  fixes above shipped briefly invisible for exactly that reason.

## v1.0.11

Installs now stream over WPD instead of the Shell's copy engine. On real
hardware a 123 MiB `.nsp` went from 68.5s to 5.97s, and a 45-file
Atmosphère mod from ~3s per file to 0.27s. Pre-send verification no longer
re-reads every byte of a file it already hashed minutes earlier, and games
finally show real progress while they transfer.

1.0.10 was bumped but never published, so this was the first release
carrying any of it.

---

Releases before 1.0.11 predate this file. Their contents are in the
[commit history](../../commits/main) and on the
[releases page](../../releases).
