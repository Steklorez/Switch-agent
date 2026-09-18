"""One-shot timed transfer test over raw WPD -- NOT part of the switchagent
package, NOT used by the app, NOT a RealMtpBackend.

Purpose: split the single opaque number the current Shell/IFileOperation
path produces ("send_file elapsed=79.08s") into the three parts that
actually matter, so we can tell whose seconds those are:

    1. create   -- CreateObjectWithPropertiesAndData() round-trip
    2. stream   -- our own IStream::Write() calls, i.e. bytes actually
                   moving over USB, with a per-chunk rate profile
    3. commit   -- IStream::Commit() AFTER the last byte was accepted,
                   i.e. the console finalising the install on its own

If the long tail sits in (3), it is DBI writing to the SD card and no PC-side
change can remove it. If it sits in (2) at a rate far above what the Shell
achieved, IFileOperation was the bottleneck.

Safety, enforced in code:
  - refuses to run without --yes-write
  - refuses any device whose fingerprint is not the one passed in --device
  - writes exactly ONE object, exactly once, from an existing local file
  - never deletes, never renames, never touches anything else on the device

Usage (explicit authorization required):
    python tools/wpd_transfer_test.py --list
    python tools/wpd_transfer_test.py --device <fingerprint> \
        --storage "5:" --file "D:\\path\\game.nsp" --yes-write
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switchagent.mtp import wpd  # noqa: E402


def fingerprint(device_id: str) -> str:
    return hashlib.sha256(device_id.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def shell_device_id(pnp_id: str) -> str:
    """The id switchagent/db stores (a Shell parsing path) for this device."""
    return "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\" + pnp_id


def find_storage(content, properties, prefix: str):
    for object_id in wpd.enum_children(content, wpd.DEVICE_OBJECT_ID):
        name = wpd.object_name(wpd.read_props(properties, object_id))
        if name and name.startswith(prefix):
            return object_id, name
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list devices/storages and exit (read-only)")
    parser.add_argument("--device", help="fingerprint of the ONE device this run may touch")
    parser.add_argument("--storage", default="5:", help="storage name prefix, e.g. '5:' for SD Card install")
    parser.add_argument("--file", help="local file to send")
    parser.add_argument("--yes-write", action="store_true", help="required: this run writes to the console")
    args = parser.parse_args()

    wpd.co_initialize()
    device_ids = wpd.list_device_ids()
    switches = [d for d in device_ids if "vid_057e" in d.lower()]
    if not switches:
        print("No Nintendo device visible over WPD.")
        return 1

    if args.list:
        for pnp in switches:
            print(f"device   {pnp}")
            print(f"  fingerprint (as switchagent sees it): {fingerprint(shell_device_id(pnp))}")
            device = wpd.open_device(pnp)
            try:
                content = wpd.get_content(device)
                properties = wpd.get_properties(content)
                for object_id in wpd.enum_children(content, wpd.DEVICE_OBJECT_ID):
                    props = wpd.read_props(properties, object_id)
                    free = props.get(str(wpd.WPD_STORAGE_FREE_SPACE_IN_BYTES))
                    print(f"  {object_id:8} {wpd.object_name(props)!r}  free={free}")
            finally:
                wpd.close_device(device)
        return 0

    if not (args.device and args.file and args.yes_write):
        print("Refusing: --device, --file and --yes-write are all required for a write run.")
        return 2

    source = Path(args.file)
    if not source.is_file():
        print(f"Refusing: no such file: {source}")
        return 2

    matching = [p for p in switches if fingerprint(shell_device_id(p)) == args.device]
    if not matching:
        print(f"Refusing: no connected device has fingerprint {args.device}.")
        print("Connected:", [fingerprint(shell_device_id(p)) for p in switches])
        return 2

    pnp = matching[0]
    size = source.stat().st_size
    device = wpd.open_device(pnp)
    try:
        content = wpd.get_content(device)
        properties = wpd.get_properties(content)
        storage_id, storage_name = find_storage(content, properties, args.storage)
        if storage_id is None:
            print(f"Refusing: no storage whose name starts with {args.storage!r}")
            return 2

        print(f"device   {fingerprint(shell_device_id(pnp))}")
        print(f"storage  {storage_id} {storage_name!r}")
        print(f"file     {source.name}  ({size / 2**30:.2f} GB)")
        print("--- writing ---", flush=True)

        t_start = time.perf_counter()
        stream, chunk_size = wpd.create_file_object(content, storage_id, source.name, size)
        t_created = time.perf_counter()
        chunk_size = chunk_size or 262144
        print(f"create   {t_created - t_start:7.2f} s   (device's optimal chunk: {chunk_size} bytes)", flush=True)

        buffer = ctypes.create_string_buffer(chunk_size)
        sent = 0
        slowest = 0.0
        marks = []
        next_mark = 0.10
        try:
            with source.open("rb") as handle:
                while True:
                    read = handle.readinto(buffer)
                    if not read:
                        break
                    t_chunk = time.perf_counter()
                    wpd.stream_write(stream, buffer, read)
                    took = time.perf_counter() - t_chunk
                    slowest = max(slowest, took)
                    sent += read
                    if sent / size >= next_mark:
                        elapsed = time.perf_counter() - t_created
                        marks.append((sent / size, elapsed, sent / 2**20 / elapsed))
                        print(f"  {sent / size:5.0%}  {elapsed:7.2f} s  {sent / 2**20 / elapsed:6.1f} MB/s", flush=True)
                        next_mark += 0.10
            t_written = time.perf_counter()
            print(f"stream   {t_written - t_created:7.2f} s   "
                  f"({size / 2**20 / (t_written - t_created):.1f} MB/s avg, "
                  f"slowest single chunk {slowest * 1000:.0f} ms)", flush=True)
            print("commit   ... (this is where the console finalises)", flush=True)
            wpd.stream_commit(stream)
            t_committed = time.perf_counter()
            print(f"commit   {t_committed - t_written:7.2f} s", flush=True)
            print(f"TOTAL    {t_committed - t_start:7.2f} s "
                  f"for {size / 2**20:.0f} MiB", flush=True)
        finally:
            wpd.release(stream)
    finally:
        wpd.close_device(device)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
