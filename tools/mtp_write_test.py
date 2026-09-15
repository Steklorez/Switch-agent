"""One-shot experimental write test -- NOT part of the switchagent package,
NOT a RealMtpBackend implementation, NOT reused by anything else.

Authorized scope, exactly:
  - target storage: "1: SD Card" only (verified by name prefix "1:")
  - target device: must match a specific, pre-confirmed fingerprint
    (refuses to run against any other device)
  - creates exactly one uniquely-named test folder via NewFolder()
  - copies exactly one small (~4 KB) locally-generated test file into it
    via CopyHere()
  - never deletes anything, never touches SD Card install/Saves/NAND,
    never transfers a real game file (.nsp/.nsz/.xci/.xcz)
  - polls the destination afterwards to observe appearance/size
    stabilization -- does not assume CopyHere() is synchronous

Run manually, once, with explicit authorization:
    python tools/mtp_write_test.py
"""

from __future__ import annotations

import hashlib
import os
import random
import string
import time
from datetime import datetime, timezone
from pathlib import Path

import win32com.client

EXPECTED_FINGERPRINT = "9a09de067c394419"  # Switch A, confirmed present this session
TARGET_STORAGE_PREFIX = "1:"  # "1: SD Card" -- never anything else in this script
COPY_FLAGS = 4 + 16 + 512  # FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 60
STABLE_READS_REQUIRED = 3

LOCAL_SCRATCH_DIR = Path(__file__).resolve().parent / "_write_test_artifacts"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(path_value: str) -> str:
    return hashlib.sha256(path_value.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def rand_suffix(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def main() -> int:
    log = []

    def record(msg: str):
        line = f"[{now()}] {msg}"
        log.append(line)
        print(line)

    shell = win32com.client.Dispatch("Shell.Application")
    this_pc = shell.NameSpace(0x11)
    devices = [i for i in this_pc.Items() if i.IsFolder and not i.IsFileSystem]

    if len(devices) != 1:
        record(f"ABORT: expected exactly 1 MTP device connected, found {len(devices)}. Not proceeding.")
        return 1

    device = devices[0]
    fp = fingerprint(device.Path)
    record(f"Connected device fingerprint: {fp}")
    if fp != EXPECTED_FINGERPRINT:
        record(f"ABORT: fingerprint does not match expected {EXPECTED_FINGERPRINT}. Not proceeding.")
        return 1
    record("Device fingerprint confirmed -- this is the pre-authorized Switch A.")

    sd_card = next((i for i in device.GetFolder.Items() if i.Name.startswith(TARGET_STORAGE_PREFIX)), None)
    if sd_card is None:
        record(f"ABORT: no storage starting with {TARGET_STORAGE_PREFIX!r} found.")
        return 1
    record(f"Target storage confirmed: {sd_card.Name!r}")
    sd_folder = sd_card.GetFolder

    # --- prepare local test file -------------------------------------------
    LOCAL_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    suffix = rand_suffix()
    folder_name = f"switchagent_test_{suffix}"
    file_name = f"switchagent-mtp-test-{suffix}.bin"
    local_file = LOCAL_SCRATCH_DIR / file_name

    payload = os.urandom(4096)  # 4 KB of random bytes -- clearly not a game file
    local_file.write_bytes(payload)
    local_sha256 = hashlib.sha256(payload).hexdigest()
    record(f"Local test file created: {local_file} ({len(payload)} bytes, sha256={local_sha256[:16]}...)")

    # --- create the test folder via NewFolder() -----------------------------
    record(f"Calling NewFolder({folder_name!r}) on {sd_card.Name!r}...")
    sd_folder.NewFolder(folder_name)

    test_folder_item = None
    for attempt in range(10):
        time.sleep(0.5)
        test_folder_item = next((i for i in sd_folder.Items() if i.Name == folder_name), None)
        if test_folder_item is not None:
            break
    if test_folder_item is None:
        record(f"RESULT: NewFolder() FAILED -- '{folder_name}' did not appear on '{sd_card.Name}' after waiting.")
        record("Stopping here as instructed -- not attempting any other storage.")
        return 1
    record(f"NewFolder() SUCCEEDED -- '{folder_name}' confirmed present on '{sd_card.Name}'.")

    dest_folder = test_folder_item.GetFolder

    # --- CopyHere() ----------------------------------------------------------
    source_ns = shell.NameSpace(str(LOCAL_SCRATCH_DIR))
    source_item = source_ns.ParseName(file_name)
    if source_item is None:
        record("ABORT: could not get a Shell item for the local source file.")
        return 1

    copy_started_at = time.monotonic()
    record(f"CopyHere() started -> '{sd_card.Name}/{folder_name}/{file_name}'")
    dest_folder.CopyHere(source_item, COPY_FLAGS)

    # --- observe destination: appearance + size stabilization ----------------
    appeared_at = None
    stabilized_at = None
    last_size = None
    stable_count = 0
    final_size = None
    error = None

    deadline = copy_started_at + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        dest_item = next((i for i in dest_folder.Items() if i.Name == file_name), None)
        if dest_item is None:
            record("  poll: not yet visible on device")
            continue

        if appeared_at is None:
            appeared_at = time.monotonic()
            record(f"File first observed on device at +{appeared_at - copy_started_at:.1f}s")

        size_val = dest_item.ExtendedProperty("System.Size")
        record(f"  poll: System.Size = {size_val!r}")

        if size_val not in (None, "", 0) and size_val == last_size:
            stable_count += 1
        else:
            stable_count = 0
        last_size = size_val

        if stable_count >= STABLE_READS_REQUIRED:
            stabilized_at = time.monotonic()
            final_size = size_val
            record(f"Size stabilized at {final_size} after {stable_count} consistent reads, "
                    f"+{stabilized_at - copy_started_at:.1f}s since CopyHere() call")
            break
    else:
        error = f"Timed out after {POLL_TIMEOUT_SECONDS}s waiting for size to stabilize."
        record(f"RESULT: {error}")

    # --- final verification ---------------------------------------------------
    record("\n=== SUMMARY ===")
    record(f"NewFolder() succeeded: True")
    record(f"CopyHere() invoked: True")
    record(f"File appeared on device: {appeared_at is not None}")
    record(f"Local source size: {len(payload)} bytes")
    record(f"Final observed device size: {final_size!r}")
    size_match = (final_size == len(payload))
    record(f"Size matches local source: {size_match}")
    record(f"Size stabilization reliably detected: {stabilized_at is not None}")
    record(f"Error: {error!r}")
    record(f"\nLocal source file left in place at: {local_file}")
    record(f"Device-side artifact left in place at: '{sd_card.Name}/{folder_name}/{file_name}' "
           f"-- NOT deleted, per instructions.")

    log_path = LOCAL_SCRATCH_DIR / f"write-test-log-{suffix}.txt"
    log_path.write_text("\n".join(log), encoding="utf-8")
    record(f"\nFull log saved to: {log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
