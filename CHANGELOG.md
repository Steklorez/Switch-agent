# Changelog

Each `## vX.Y.Z` section becomes the body of that version's
[GitHub Release](../../releases).

## v1.0.12

- One file counts as one library row again, whatever path it is reached by.
- Re-adding a Library folder brings its games back instead of leaving them invisible.
- Files the library no longer holds are no longer shown as games.
- Queue draws one row per file instead of two.
- A running scan can be stopped.
- Changing the Library folders no longer has to wait for a scan to finish.
- The last Library folder can be removed.
- A fresh install no longer scans your Downloads folder unasked.
- The SD card space bar still works with no Switch connected, estimating against an empty 256 GB card.
- Static assets are versioned by content, so an update always reaches the browser.

## v1.0.11

- Installs stream over WPD instead of the Shell's copy engine: a 123 MiB NSP went from 68.5s to 5.97s, a 45-file Atmosphère mod from ~3s per file to 0.27s.
- Games show real progress while they transfer.
- Pre-send verification no longer re-reads every byte of a file it already hashed.

---

Releases before 1.0.11 predate this file — see the
[releases page](../../releases).
