# Web UI — local library + queue + devices management

FastAPI + Jinja2 + vanilla JS, running on the same Windows machine as
everything else in this project. No cloud, no external services, no
account/auth beyond "reachable on your LAN". Built on top of, not instead
of, everything from Stage 1-5B/SD_INSTALL-fix -- `switchagent/queue_worker.py`,
`manifest.py`, `db.py`, `mtp/*` are unchanged in their core transfer
semantics; the Web UI is a new presentation + orchestration layer
(`switchagent/web/`) plus two small, additive extensions to `db.py`
(`library_items`/`devices`/`install_history` tables, and letting a job
reference a `library_items` row instead of only `inbox_items`).

## Running it

```
switch-agent web                      # http://127.0.0.1:8765, real hardware
switch-agent web --host 0.0.0.0       # also reachable from phone/tablet on the LAN
switch-agent web --mock               # MockMtpBackend, no Switch needed (dev/testing)
switch-agent web --port 9000
```

`MOCK_MTP=true` (env var) is equivalent to `--mock`, matching the
project-wide mock-mode convention from the original spec.

Either way, mock mode opens its **own** database -- `switchagent-mock.db`
next to the real `switchagent.db`, never the real one (see
`config.MOCK_DB_PATH`). This matters most for a frozen build: `dist/`
carries no `portable.flag`, so a hand-started `dist\SwitchAgent\SwitchAgent.exe --mock`
runs in *installed* mode and its app-data root is the real
`%LOCALAPPDATA%\SwitchAgent` -- before this split, such a run wrote its two
fixture devices straight into the user's own Devices page (2026-09-17).

Binds to `127.0.0.1` by default -- not reachable from other devices until
you explicitly pass `--host 0.0.0.0`. Never exposed to the internet
automatically; no reverse proxy is set up by this project.

Open the printed URL in a browser on the same PC, or (after `--host
0.0.0.0`) from a phone/tablet on the same home network, e.g.
`http://192.168.x.x:8765`.

## Architecture

```
Web UI (Jinja2 pages + vanilla JS)
        ↓ fetch()/form submit
FastAPI routes (switchagent/web/app.py) -- thin, no business logic
        ↓
Application services (switchagent/web/services.py)
        ↓
Library / Queue / Transfer (scanner.py, preview.py, manifest.py, queue_worker.py, db.py)
        ↓
MtpBackend (RealMtpBackend / MockMtpBackend)
```

`switchagent/web/`:
- `app.py` -- FastAPI app factory + every route (HTML pages and `/api/*`).
- `services.py` -- the application/service layer the spec asked for:
  library listing/filtering, device merge (live + known), job creation
  (the one place that calls `queue_worker.create_job_from_report()` +
  `db.confirm_job()`), retry/cancel, history, settings.
- `context.py` -- `WebContext`: the device registry, the background worker
  thread, the background scan thread, worker pause/resume state. Built
  once per process (`build_real_context()` / `build_mock_context()`),
  stored on `app.state.ctx`.
- `schemas.py` -- Pydantic request bodies for the few mutating endpoints
  (`POST /api/jobs`, `POST /api/devices/{id}/rename`). GET responses are
  plain dicts from `services.py` -- no response-schema duplication.
- `templates/*.html`, `static/{style.css,*.js}`.

SQLite connections are never shared across threads: every HTTP request
opens and closes its own short-lived connection (`get_conn` FastAPI
dependency); the background worker thread and the background scan thread
each own their own connection. WAL mode (already enabled by
`db.get_connection()`) makes this safe for SQLite's one-writer/many-readers
model at this scale.

## Library: how `D:\shared\Download` is scanned

`config.LIBRARY_DIR` (default `D:\shared\Download`, overridable via
`config.yaml`'s `library: source_dir:`) is scanned by
`scanner.scan_library_once()` -- the same classification logic as Stage
2's `scan_once()` (NSP/NSZ/XCI/XCZ detection, archive listing, atmosphere
mod folder detection, TITLE_ID validation, SHA-256 dedup), applied to a
different root and indexed into a **new, separate table**,
`library_items` (keyed by absolute path), never mixed with `inbox_items`
(keyed by inbox-relative path). Stage 2's `inbox/` workflow and CLI
(`switch-agent scan`/`preview`/`mtp-mock`) are completely unchanged and
still work.

A full rescan only happens when you press **Rescan** (`POST /api/scan`) --
it runs in a background thread (`WebContext.run_scan_in_background()`) so
the UI never blocks; `GET /api/scan/status` is polled by the page to show
progress and refresh once done. Unchanged files (same size+mtime, or same
folder fingerprint for atmosphere mods) are recognized and skipped without
re-hashing, exactly like Stage 2.

**Scanning never creates a job.** `scan_library_once()` only ever writes
`library_items` rows with a status like `AVAILABLE`/`NEEDS_REVIEW`/
`ERROR`/`SKIP_DUPLICATE` -- see `tests/test_library_scanner.py::
test_scan_never_creates_a_job` and `tests/test_web_api.py::
test_scan_never_creates_a_job_via_api`.

Large files are never copied into a second location just to be indexed --
`library_items.absolute_path` is a reference to the file where it already
lives; only small per-job manifest data (SHA-256 hash, size) is stored
separately.

### Grouping: Base Game / Update / DLC / Mod

The Library page groups `GAME_PACKAGE` entries by TITLE_ID using
`title_id.classify_title_variant()` (base/update/DLC arithmetic on the
public Nintendo Switch TITLE_ID convention -- see the install-order
section below for the exact rule; same function backs both the UI
grouping here and the queue's dependency enforcement, never two
independently-maintained copies). Default filter (**Games**) shows base
games as the top-level cards; Updates/DLC/matching mods nest underneath
via a native `<details>` (no JS needed to expand/collapse). Dedicated
**Updates**/**DLC**/**Mods** filter tabs show flat, ungrouped lists across
every game. An entry with no usable TITLE_ID, or a genuine duplicate
download of the same base game, is still shown, never silently dropped.

**Selecting a Base Game's checkbox auto-selects every sibling in its
TITLE_ID family** (its updates, DLC, and matching mods) -- the default
"select a game" action is "select the whole installable set", since
installing an update/DLC without its base is exactly the broken state the
install-order fix above exists to prevent. The user can still uncheck
individual siblings afterward (unchecking a sibling never affects the
base or other siblings); unchecking the base itself mirrors back off,
deselecting the whole family so nothing stays selected invisibly. Never
reaches into a different TITLE_ID. Purely client-side (`library.js`) --
each checkbox carries `data-family="<base_title_id>"`, and the base
carries `data-role="base"` too; selecting a card still never creates
anything by itself (see "Confirmation" below).

## Devices

`WebContext.refresh_devices()` does the real work:
1. **Discovery** (mode-specific): real mode calls
   `mtp.windows.enumerate_devices()` and registers a fresh
   `RealMtpBackend` for any never-before-seen `device_id`; mock mode's
   registry is pre-populated at startup, nothing to discover.
2. **Liveness** (mode-agnostic): tries `.connect()` on every already-known
   backend -- the same reachability check `queue_worker.run_worker_once()`
   already relies on. Success updates `devices.last_seen_at`/
   `last_known_display_name`; failure just means "not currently connected"
   -- the device is still listed, marked disconnected, never dropped.

**Called ONLY by the worker thread, never by an HTTP request handler**
(fixed after a real-hardware incident: a second thread calling into the
same cached COM object concurrently corrupted its state). `RealMtpBackend`'s cached COM object
references (`_device_item`/`_device_folder`) are not thread-safe;
`refresh_devices()` used to also run on every `/api/devices` request (a
FastAPI request-thread-pool thread, a different thread than the worker's),
which could race a real transfer in progress on the SAME backend instance
and invalidate its COM references mid-transfer -- confirmed root cause of
a job crashing with `AttributeError: <unknown>.Items` while a large real
file was transferring. `GET /api/devices` (and every page that shows
device status) now calls `WebContext.get_known_devices()` instead -- a
plain, lock-protected read of the worker thread's last observation, no COM
involved at all. At most a couple of `worker_poll_interval_seconds` stale;
while a transfer is in progress the cache simply keeps showing "connected"
(accurate -- it was reachable right before the transfer started) rather
than blocking on or racing the busy worker thread. A real disconnect
mid-transfer still surfaces correctly and promptly via that job's own
`INTERRUPTED` status, independent of this cache.

`device_id` (a stable `FolderItem.Path`-derived string) is never the display name and never
editable. `friendly_name` (Devices page, `POST /api/devices/{id}/rename`)
is purely cosmetic, stored in the new `devices` table, and can only be set
for a device that has been seen at least once -- renaming never invents a
device and never changes `device_id`. Pages that show a job/history row
never render the raw `device_id` as visible text either (it embeds the
device's real USB serial number) -- `services.device_label()` resolves it
to a friendly name or a safe fingerprint first; the raw id is still used
wherever it functionally has to be (API payloads, HTML form values,
matching logic).

A device row can also be **forgotten**
(`POST /api/devices/by-fingerprint/{fingerprint}/forget`, the corner ×
on a disconnected Devices row): SwitchAgent drops its own memory of
that identity -- the `devices` row, its friendly name, its storage mappings
and its cached installed-title list -- and nothing else. Queue/History rows
that reference it are deliberately left intact and keep rendering through
`services.device_label()`'s fingerprint fallback. It is refused (409) for a
device that is connected right now, or one with unfinished jobs still
targeting it, and it is not a blocklist: the same device plugged in again
is recorded anew, with no name carried over.

## Jobs / queue / confirmation

`POST /api/jobs` (`{library_item_ids: [...], target_device_id: "..."}`) is
the **only** place a job is created from the Web UI --
`services.create_and_confirm_jobs()`:
1. For each item: reject anything not currently `AVAILABLE` (already
   queued/installed/needing review) with a per-item error, not a hard
   failure for the whole batch.
2. `preview.preview_path(path, extract=True)` (same function the CLI's
   `mtp-mock` already uses) -- extracts an archive if needed, right here,
   not at scan time.
3. `queue_worker.create_job_from_report(..., library_item_id=...)` --
   freezes a manifest exactly like Stage 4.1, from the exact content just
   previewed.
4. `db.confirm_job(...)` immediately -- the confirmation modal the user
   already clicked through **is** the human confirmation; there is no
   second, separate confirmation step later.

The background worker thread (started at `switch-agent web` startup,
`WebContext._worker_loop`) picks up `CONFIRMED` jobs independently, on its
own ~2s poll -- **job creation and transfer are decoupled**: creating a
job never transfers anything synchronously inside the HTTP request (see
`tests/test_web_api.py::test_create_jobs_does_not_transfer_immediately`).
Closing the browser/phone does not stop the queue; only stopping the
`switch-agent web` process does.

A job's `target_device_id` is fixed at creation (Stage 4's invariant,
unchanged) -- if that device isn't currently reachable, the worker leaves
the job `DEVICE_UNAVAILABLE` and waits; it is never redirected to a
different, currently-connected device.

**Cancel** (`POST /api/jobs/{id}/cancel`) only works for
`PENDING_CONFIRM`/`CONFIRMED`/`DEVICE_UNAVAILABLE`/`WAITING_FOR_BASE` --
jobs that haven't started a physical transfer yet. A `RUNNING` job cannot
be atomically stopped (real MTP has no such primitive, see `mtp/base.py`'s
module docstring) -- the API returns 409 rather than pretend otherwise.

### Install order: Base Game -> Update -> DLC -> Mod

Added after a real-hardware incident: a Base Game job and its Update job were both allowed to
reach `RUNNING`, the update transferred and installed correctly, and the
base game's job silently crashed and was left stuck (see the COM
thread-safety fix above for *why* it crashed) -- Horizon ended up showing
an icon for a title whose actual application data had never arrived.

`queue_worker.run_worker_once()` now gates every Update/DLC/Mod job behind
its Base Game (`queue_worker._dependency_status()`, using
`title_id.classify_title_variant()` -- the same public, well-documented
TITLE_ID convention the Library UX grouping uses, base = low 12 bits
`0x000`, update = `0x800`, DLC = `(id & ~0xFFF) - 0x1000`; mods are tagged
with the base game's own TITLE_ID directly, no arithmetic needed):
- **Base Game jobs have no dependency** -- always eligible (subject to the
  normal device-reachability check).
- **Update/DLC/Mod jobs** check, for the base game's own TITLE_ID on the
  same target device, in order:
  1. `install_history` has a `DONE`/`DONE_UNVERIFIED` entry for it ->
     eligible, proceeds normally.
  2. `install_history` has a `FAILED`/`INTERRUPTED`/`SOURCE_CHANGED`/
     `DESTINATION_CONFLICT` entry for it (and no successful one) -> job
     status becomes `BLOCKED_BY_DEPENDENCY` (terminal, not retryable --
     same "resolve the real problem, then create a fresh job" principle
     as `SOURCE_CHANGED`/`DESTINATION_CONFLICT`), recorded in History too.
  3. a Base Game job for that title on that device is currently
     outstanding (`PENDING_CONFIRM`/`CONFIRMED`/`DEVICE_UNAVAILABLE`/
     `RUNNING`/`VERIFYING`) -> job status becomes `WAITING_FOR_BASE`
     (re-checked automatically every worker pass, exactly like a fresh
     `CONFIRMED` job -- no explicit action needed, unlike
     `DEVICE_UNAVAILABLE`). Cancelable if the user has no intention of
     ever installing the base.
  4. none of the above (no competing base job, and no history either way)
     -> eligible, proceeds normally. Deliberately NOT "block forever until
     install_history proves a base install happened": this hardware
     cannot prove what's already on the console at all (no MTP-visible
     installed-games list), so treating
     silence as "must wait" would make it impossible to ever install an
     update/DLC/mod for a game the user already owns or installed some
     other way -- a real regression caught by
     `tests/test_stage41_recovery.py` while building this feature (its
     mod-only tests, which never create a base job at all, started
     hanging forever in `WAITING_FOR_BASE`). What's actually enforced is
     narrower and matches the real incident exactly: an update/DLC/mod
     never overtakes a base job for the SAME title that is genuinely
     still in flight or already known to have failed -- not "prove a
     base exists before anything else can ever run".

A second, related trap caught by the same test file: a mod's "base
TITLE_ID" for this check is its own TITLE_ID (mods have no variant
arithmetic, they're tagged with the base game's TITLE_ID directly) -- so
without care, a mod's own prior `INTERRUPTED` attempt looked like "its
base game failed" on retry. Fixed by scoping step 1/2 above to
`install_history` rows with `target_storage = 'SD_INSTALL'` only -- base/
update/DLC always target `SD_INSTALL`, mods always target `SD_CARD`, so a
mod's own history can never be mistaken for its base's.

This is **order-independent by construction**: it doesn't matter which job
was created or confirmed first, or which one `run_worker_once()` happens
to check first on a given pass -- an Update/DLC/Mod simply never reaches
`_process_job()` (and therefore never calls `RealMtpBackend.send_file()`
at all) until its base's success is already on record. Two *unrelated*
TITLE_IDs never block each other -- the dependency check only ever looks
at the SAME title's base variant.

A second, independent safety net covers any *other* unexpected failure
the same way: `queue_worker._process_job()` now wraps the whole transfer
in a broad exception handler. Previously, an exception that wasn't a
clean `MtpError` (the real incident: a raw `AttributeError` from a
COM object invalidated by the thread-safety bug above) propagated all the
way up to the worker loop's own handler, which only logs and moves on --
leaving the job stuck at `RUNNING` forever, with no `install_history`
row, invisible as an error anywhere in the UI. Now it's caught where the
job id is still known and marked `INTERRUPTED` (never `FAILED` -- we
genuinely don't know how much reached the device) with a normal History
entry, whatever the underlying cause turns out to be in the future.

**Destination conflict / source changed / interrupted** all behave exactly
as Stage 4.1 defined them (`queue_worker.py`, unchanged) -- no automatic
overwrite, no automatic new job, no automatic retry. The Queue page shows
the status and a Retry button where retrying is actually valid
(`FAILED`/`INTERRUPTED`/`DEVICE_UNAVAILABLE`); `SOURCE_CHANGED`/
`DESTINATION_CONFLICT` intentionally have no Retry button (Stage 4.1's
`db.retry_job()` refuses both -- see its docstring).

## SD_INSTALL / DONE_UNVERIFIED

A job whose transfer resolved to `TransferStatus.UNVERIFIED` (SD_INSTALL's
honest "transport accepted, DBI result unprovable" outcome) ends as
job status `DONE_UNVERIFIED`, shown on cards/queue/history as **"Installed
(unverified)"**, visually distinct from a plain `DONE`. The Library page
never claims "Currently installed" -- only "Installed by SwitchAgent",
grounded in `install_history` (our own record), never in a live read of
the console's installed-games list (this hardware has no such node to
read from at all).

## History

`install_history` (new table) gets one row every time a job reaches ANY
terminal outcome -- `DONE`, `DONE_UNVERIFIED`, `FAILED`, `INTERRUPTED`,
`SOURCE_CHANGED`, `DESTINATION_CONFLICT` -- written by
`queue_worker._process_job()`'s wrapper, so History shows failures too,
not just successes. Stored in SQLite, not memory -- survives a server
restart.

## Confirmation / anti-accident guarantees (highest priority, per spec)

- Selecting a card's checkbox never creates anything -- purely client-side
  UI state (`selected` Map in `library.js`) until "Install selected" +
  "Confirm Install" are both explicitly pressed.
- `POST /api/jobs` requires a non-empty `target_device_id` (Pydantic
  `min_length=1`) -- there is no "just pick one for me" path, client or
  server side.
- A job's target device and target content are both frozen at creation
  (Stage 4 / Stage 4.1, unchanged) -- nothing in the Web UI layer ever
  edits either after the fact.
- The confirmation modal shows the actual, current selection (not a
  snapshot from when the card was clicked) and lets the user drop
  individual items right there -- each row has its own remove button,
  updating the live count/size/list and unchecking the matching card, all
  before "Confirm Install" is pressed. Auto-selecting a game's whole
  family (see Library grouping above) is a convenience default, never a
  requirement -- Update/DLC/Mods can always be excluded manually, at
  either the card or the confirmation-modal step.
- No delete/format/execute/run-arbitrary-command affordance exists
  anywhere in the UI or API -- `services.py` only exposes the specific
  operations listed in this document, nothing else. Deleting a downloaded
  file after a successful install is explicitly out of scope for this
  stage (spec point 22) and not implemented.

## Mobile

Mobile-first CSS (`static/style.css`): bottom tab bar for navigation on
narrow screens (becomes a left sidebar ≥768px), large (≥40px) touch
targets throughout, a sticky bottom selection bar that appears only once
≥1 card is selected, native `<dialog>` for the confirmation and detail
modals (no custom modal JS/focus-trapping needed). Tested by hand at
375px-wide and desktop widths.

## Security boundaries

- No shell execution, no arbitrary file deletion, no arbitrary path
  access from any endpoint -- every mutating endpoint maps to one
  specific, narrow operation in `services.py`.
- Binds to `127.0.0.1` unless `--host 0.0.0.0` is explicitly passed; never
  auto-exposed to the internet; no reverse proxy configured by this
  project.
- No authentication layer -- this is a LAN-only, single-household tool,
  matching the original spec ("НЕ нужна авторизация в интернете"). Do not
  port-forward this to the public internet.

## Tests

`tests/test_web_db.py` (schema additions + jobs-table rebuild migration),
`tests/test_library_scanner.py` (library indexing), `tests/test_web_api.py`
(FastAPI routes via `TestClient`, `MockMtpBackend` throughout, HTML pages
render smoke test).
