# Security Policy

## Supported versions

Only the [latest release](../../releases/latest) is supported. SwitchAgent
has no auto-updater, so if you are reporting something, please say which
version you are on (**Settings** page, or `switch-agent --version`).

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use GitHub's [private vulnerability
reporting](../../security/advisories/new) instead. If that is unavailable
to you, open a normal issue saying only that you have a security report and
asking for a contact -- no details in it.

Expect a first reply within about a week. This is a personal project
maintained by one person in their own time, not a product with an on-call
rotation, and it is better to say so than to imply an SLA that will not be
met.

## What this program does and does not do

Worth knowing before deciding whether something is a vulnerability:

- **The web UI binds to `127.0.0.1` only** unless you start it with a
  different bind address yourself (`--host 0.0.0.0`). There is **no
  authentication of any kind**. That is a deliberate design for a
  single-user local tool -- but it means that binding it to a network
  interface exposes your library and install controls to everything that
  can reach that address. Do that only on a network you trust.
- **No telemetry, no auto-updater, no remote code.** The only outbound
  request SwitchAgent makes on its own is a version check against GitHub's
  public Releases API, at most once every 24h (or on demand). It sends
  nothing about you, your library, or your devices, and it never downloads
  or executes anything as a result.
- **Archives are extracted defensively.** Entry count, uncompressed size
  and path depth are capped (`extraction:` in `config.yaml`), and paths
  that escape the extraction root -- `..`, absolute paths, symlinks --
  are refused rather than sanitised. A zip-slip or zip-bomb that gets past
  this is a genuine vulnerability; please report it.
- **Device serial numbers are masked** everywhere SwitchAgent writes or
  displays them: logs, error reports, diagnostics, and page HTML carry a
  short non-reversible fingerprint, never the raw USB serial. A path that
  leaks the raw value is a genuine bug; please report it.
- **Nothing is written to the console except what you ask for.** NAND is
  never a write target, and an existing file at a destination is never
  overwritten without an explicit Override.

## Out of scope

- The Windows SmartScreen warning on an unsigned installer. It is expected
  for an independently-published app; see `docs/PACKAGING.md`.
- Anything requiring an attacker to already have write access to your
  `config.yaml`, your library folders, or `%LOCALAPPDATA%\SwitchAgent`. At
  that point they already have your account.
- The absence of authentication on `127.0.0.1`, as described above.
- DBI itself, the console, or its custom firmware. SwitchAgent hands bytes
  to [DBI](https://github.com/rashevskyv/dbi) over MTP and its
  responsibility ends there.
