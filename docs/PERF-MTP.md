# Where an install's time actually goes

Measured 2026-09-18/19 against real hardware: an OLED Switch (fingerprint
`9a09de06…`) and a second console (`e7f30155…`), both running DBI's MTP
Responder, over the same USB 2.0 cable, with the library on a local drive
that sustains 265 MB/s.

The complaint that started this: *"a game installs in 15-20 seconds and then
I wait a minute or two for something to happen; the gaps between images are
enormous."*

## What the old path spent its time on

Per game, from the `jobs` table and `switchagent.log`:

| Phase | What ran | Measured |
|---|---|---|
| `created_at` → `started_at` | **two full SHA-256 passes over the same file** plus the worker's 2s poll | 21s for 2.49 GB, 13s for 1.42 GB, 8s for 0.82 GB, 4s for 0.29 GB |
| `send_file` | `IFileOperation.PerformOperations()` + verification | 70-166s |
| gap to the next game | extraction, staging and hashing of the next item, strictly after this one finished | 4-21s |

The first row is twice the hash plus the poll interval, and it fits every
row in the table: `build_manifest_and_stage()` hashed the file when the job
was created, then `verify_manifest_against_source()` hashed the identical
bytes again immediately before sending. For a 13.8 GB game that is ~108
seconds before the first byte leaves the PC.

## The 60-second tail

`send_file` for the same file, on the same console, reproduced to within
0.2s across runs hours apart:

| File | Shell / `IFileOperation` |
|---|---|
| `World of Goo …[v0].nsp`, 123 MiB | 7.0-7.4s on six runs, **68.4-68.7s** on two |
| `Bread and Fred …[v0].nsz`, 0.29 GB | 79.08 / 79.21 / 79.00s |
| `Deer and Boy …[v0].nsp`, 1.42 GB | 120.2s |

Two hypotheses fitted that data equally well: the console finalising the
install, or the Windows shell. Two measurements settled it.

**1. Every Shell enumeration is fast.** Timing each COM step of `send_file`
against the live device: `This PC` 112ms, device storages 17-24ms, the
`5: SD Card install` node 7-11ms. The pre-copy existence check and the
post-copy poll together cost well under a second, so the tail was inside
`PerformOperations()` itself.

**2. Streaming the same file over WPD instead.** `World of Goo …[v0].nsp`,
123 MiB, same console, same cable:

```
create      0.00 s   (device's optimal chunk: 262144 bytes)
stream      3.28 s   (37.6 MB/s avg, slowest single chunk 13 ms)
commit      2.69 s
TOTAL       5.97 s
```

DBI's own screen reported `Общее время установки: 0:00:05`, with every step
`[OK]` — signature checks, content registration, ticket, completion. So the
console was never the bottleneck: it accepts at 37.6 MB/s (above DBI's
typical 20-25 MB/s) and finalises in 2.7s. **The ~60 seconds belonged to
`IFileOperation`,** which returned long after the console had finished.

## What changed

- **`switchagent/mtp/wpd.py`** — a raw-ctypes WPD client. Files are streamed
  through `CreateObjectWithPropertiesAndData()` + `IStream::Write` +
  `Commit()`, which also means byte-level progress and a clean split between
  "bytes moving" and "device finalising". No new dependency: plain ctypes
  over the COM vtables, so PyInstaller packaging is unaffected. Every
  CLSID/IID was read from this machine's own type libraries; every
  PROPERTYKEY was verified against the device by dumping an object's full
  property set and matching it to what the Shell reports for the same
  object.
- **`RealMtpBackend` uses WPD and keeps the Shell as an automatic
  fallback.** Any WPD fault demotes the connection to the old path rather
  than failing the job. `WPD_TRANSPORT_ENABLED = False` in
  `switchagent/mtp/windows.py` forces the old path everywhere. The Override
  (overwrite) case deliberately stays on the Shell: replacing an object
  means deleting it first, and the Shell copy engine already does that
  correctly.
- **The pre-send hash is now a stamp check.** `verify_manifest_against_source()`
  compares `(size, mtime_ns)` and only recomputes SHA-256 when that stamp
  moved. The manifest's hash is still the one this job pinned; nothing
  trusts a hash it did not compute itself. `SWITCHAGENT_ALWAYS_REHASH=1`
  restores the old unconditional re-read.
- **The next item is prepared while the current one transfers** —
  except archives, which stay strictly sequential so that only one
  archive's payload ever occupies `work/` (see
  `test_hotfix_reconciliation.py`). A prepared-ahead item's jobs are created
  `PENDING_CONFIRM` and only confirmed when its turn arrives, so an Update
  still cannot install ahead of its Base.
- **Games show real progress.** The Queue page drew a progress bar for mods
  only, because the Shell could not report anything mid-copy; WPD counts the
  bytes it writes and reports about once a second.

## Results on real hardware

| | before | after |
|---|---|---|
| `World of Goo …[v0].nsp`, 123 MiB, SD Card install | 68.5s | **5.97s** |
| `World of Goo …[v131072].nsp`, 6 MB | ~2s | **1.20s** (34.7 MB/s) |
| 45-file Atmosphère mod, 4.3 MB, SD Card | 8.3s/file originally, ~3s/file after the 2026-09-17 poll-interval fix | **0.27s/file**, 12.2s total, every file `COMPLETED` |
| 2.49 GB `.nsp` pre-send verification | 2 × 9.6s hash | one stamp check |

Mod files still verify by reading the destination size back, so `COMPLETED`
means exactly what it meant before — the difference is that WPD reads it
live instead of waiting out the shell namespace cache.

## Things worth knowing that came out of this

- **DBI leaves a phantom placeholder.** After an install completes, the
  sent filename stays listed under `5: SD Card install` with size 0 for the
  rest of the MTP session. Both the Shell and WPD see it identically, so
  conflict behaviour is unchanged — but re-sending the same filename twice
  in one session legitimately raises `FileAlreadyExistsError`.
- **`.nsz` costs the console 2-3x more time per installed byte than `.nsp`**
  on both consoles (DBI decompresses on the fly).
- **The two consoles differ by ~2.5x in raw throughput** (≈36 MB/s vs
  ≈14 MB/s on the old path). That difference was measured through the Shell;
  it is worth re-measuring over WPD before blaming the SD card.
