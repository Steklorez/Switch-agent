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
