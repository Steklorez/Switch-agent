"""One-shot experimental FOLDER-merge test -- NOT part of the switchagent
package, NOT a RealMtpBackend implementation, NOT reused by anything else.

Question this answers: does IFileOperation.CopyItem() called on a whole
FOLDER (not one file at a time) behave like Windows Explorer's native
merge-copy on a real filesystem -- name collisions get replaced, new
files get added, everything else (including inside a nested subfolder)
is left completely untouched -- when the destination is DBI's MTP
responder on a real Switch? Or does it fail the same way the already-
documented Folder.CopyHere() finding did (docs/STAGE5A-MTP-RESEARCH.md:
"queued without error, file never appeared within 60s")?

Test plan (exactly what was asked for):
  1. Upload a local "payload" folder (4 files, 2 of them inside a nested
     "sub/" subfolder) into a fresh, uniquely-named test container on the
     SD card.
  2. Upload a SECOND local "payload" folder (different filenames, same
     structure) into the SAME container -- proves new files land
     alongside the old ones without deleting them.
  3. Upload a THIRD local "payload" folder where some filenames COLLIDE
     with batch 1/2 (deliberately different byte size, so a stale/
     unreplaced file is unambiguous) and some are brand new -- proves
     colliding files get REPLACED, non-colliding files are left alone,
     and this holds both at the top level AND inside "sub/".
  4. After every batch, recursively walks and polls the ACTUAL device
     tree (never assumes PerformOperations() returning means anything --
     same honesty-gate discipline as switchagent/mtp/windows.py) and
     logs a full comparison against the exact expected state.
  5. After batch 3, downloads every file back from the device and
     compares its SHA-256 against the expected content's own hash --
     stronger proof than switchagent's own production code uses (which
     only compares size, real MTP giving no cheap remote hash), because
     for a one-off diagnostic run we can afford the extra round trips.

Authorized scope, exactly:
  - target storage: "1: SD Card" only (verified by name prefix "1:")
  - target device: must match a specific, pre-confirmed fingerprint
    (refuses to run against any other device) -- reuses the same
    fingerprint mtp_write_test.py already confirmed this session
  - creates exactly ONE uniquely-named test container folder via
    NewFolder() -- every upload in this run targets that same container;
    nothing else on the card is ever touched
  - never deletes anything, never touches SD Card install/Saves/NAND,
    never transfers a real game file (.nsp/.nsz/.xci/.xcz)
  - all local scratch files (sources + downloaded-back copies) live
    under tools/_write_test_artifacts/ (gitignored)

Run manually, once, with the Switch connected and explicit authorization:
    python tools/mtp_folder_merge_test.py
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import win32com.client

EXPECTED_FINGERPRINT = "9a09de067c394419"  # Switch A, confirmed present this session (see mtp_write_test.py)
TARGET_STORAGE_PREFIX = "1:"  # "1: SD Card" -- never anything else in this script
COPY_FLAGS = 4 + 16 + 1024  # FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI -- same flags production uses
POLL_INTERVAL_SECONDS = 0.5
POLL_TIMEOUT_SECONDS = 60.0
STABLE_READS_REQUIRED = 3

LOCAL_SCRATCH_DIR = Path(__file__).resolve().parent / "_write_test_artifacts"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(path_value: str) -> str:
    return hashlib.sha256(path_value.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def rand_suffix(n: int = 8) -> str:
    import random
    import string
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# ---------------------------------------------------------------------------
# Deterministic per-file content -- distinct, known byte length per label so
# a stale (unreplaced) file is unambiguous from System.Size alone, plus a
# precomputed SHA-256 for the stronger post-download content check.
# ---------------------------------------------------------------------------

def make_content(label: str, size: int) -> bytes:
    header = f"{label}\n".encode("utf-8")
    assert len(header) <= size, f"label {label!r} too long for target size {size}"
    filler = hashlib.sha256(label.encode("utf-8")).digest()
    body = (filler * (size // len(filler) + 1))[: size - len(header)]
    return header + body


# (batch, relative-path-under-"payload/", content) -- batch order matters:
# a later batch's entry for the SAME relative path is the one that must win
# on the device after that batch's upload.
FILE_PLAN: list[tuple[int, str, bytes]] = [
    # Batch 1: fresh add, top-level + nested.
    (1, "alpha.txt", make_content("alpha-v1", 100)),
    (1, "beta.txt", make_content("beta-v1", 150)),
    (1, "sub/gamma.txt", make_content("gamma-v1", 120)),
    (1, "sub/delta.txt", make_content("delta-v1", 130)),
    # Batch 2: different names -- must be ADDED alongside batch 1's files,
    # which must NOT be deleted.
    (2, "epsilon.txt", make_content("epsilon-v1", 140)),
    (2, "zeta.txt", make_content("zeta-v1", 160)),
    (2, "sub/eta.txt", make_content("eta-v1", 110)),
    (2, "sub/theta.txt", make_content("theta-v1", 170)),
    # Batch 3: some names COLLIDE with batch 1/2 at a deliberately DIFFERENT
    # size (must be REPLACED), some are brand new (must be ADDED); both
    # top-level and nested. beta/zeta/delta/theta are NOT touched here --
    # they must survive completely unchanged.
    (3, "alpha.txt", make_content("alpha-v2-REPLACED", 300)),
    (3, "epsilon.txt", make_content("epsilon-v2-REPLACED", 320)),
    (3, "iota.txt", make_content("iota-v1", 90)),
    (3, "sub/gamma.txt", make_content("gamma-v2-REPLACED", 340)),
    (3, "sub/eta.txt", make_content("eta-v2-REPLACED", 360)),
    (3, "sub/kappa.txt", make_content("kappa-v1", 95)),
]


def expected_after(batch_n: int) -> dict[str, bytes]:
    """Cumulative expected {relative_path: content} after batches 1..batch_n
    have been uploaded -- a later batch's entry naturally overwrites an
    earlier one for the same path, since FILE_PLAN is batch-ascending and
    dict assignment keeps the last write."""
    result: dict[str, bytes] = {}
    for batch, rel, content in FILE_PLAN:
        if batch <= batch_n:
            result[rel] = content
    return result


def build_local_payload(batch_n: int) -> Path:
    payload_dir = LOCAL_SCRATCH_DIR / f"mergecopy_source_batch{batch_n}" / "payload"
    if payload_dir.exists():
        import shutil
        shutil.rmtree(payload_dir)
    payload_dir.mkdir(parents=True)
    (payload_dir / "sub").mkdir()
    for batch, rel, content in FILE_PLAN:
        if batch != batch_n:
            continue
        p = payload_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return payload_dir


# ---------------------------------------------------------------------------
# Device-side helpers -- IFileOperation, same COM approach as
# switchagent/mtp/windows.py (_copy_via_ifileoperation, _to_ishell_item,
# _download_file), duplicated here rather than imported so this throwaway
# tool stays independent of the real package (same convention already used
# by tools/mtp_probe.py's mask_serial()).
# ---------------------------------------------------------------------------

def to_ishell_item(folder_item):
    import win32com.shell.shell as shell_api
    pidl = shell_api.SHGetIDListFromObject(folder_item)
    return shell_api.SHCreateItemFromIDList(pidl, shell_api.IID_IShellItem)


def copy_folder_into(source_folder: Path, dest_container_item, dest_name: str, record) -> bool:
    """Copies source_folder (a whole local directory tree) into
    dest_container_item via ONE IFileOperation.CopyItem() call -- the
    thing being tested. Returns True if IFileOperation itself reported
    the operation was aborted (still never trusted as proof of success
    either way -- caller always polls the device afterwards)."""
    import pythoncom
    import pywintypes
    import win32com.shell.shell as shell_api

    source_item = shell_api.SHCreateItemFromParsingName(str(source_folder), None, shell_api.IID_IShellItem)
    dest_item = to_ishell_item(dest_container_item)

    op = pythoncom.CoCreateInstance(
        shell_api.CLSID_FileOperation, None, pythoncom.CLSCTX_ALL, shell_api.IID_IFileOperation,
    )
    op.SetOperationFlags(COPY_FLAGS)
    op.CopyItem(source_item, dest_item, dest_name, None)
    try:
        op.PerformOperations()
    except pywintypes.com_error as exc:
        record(f"  PerformOperations() RAISED: {exc}")
        raise
    aborted = bool(op.GetAnyOperationsAborted())
    record(f"  PerformOperations() returned; GetAnyOperationsAborted()={aborted}")
    return aborted


def download_file(source_item, dest_dir: Path) -> Path:
    import pythoncom
    import win32com.shell.shell as shell_api

    dest_item = shell_api.SHCreateItemFromParsingName(str(dest_dir), None, shell_api.IID_IShellItem)
    op = pythoncom.CoCreateInstance(
        shell_api.CLSID_FileOperation, None, pythoncom.CLSCTX_ALL, shell_api.IID_IFileOperation,
    )
    op.SetOperationFlags(COPY_FLAGS)
    op.CopyItem(to_ishell_item(source_item), dest_item, source_item.Name, None)
    op.PerformOperations()
    return dest_dir / source_item.Name


def walk_full_tree(folder_item, prefix: str = "") -> dict:
    """Recursively enumerates EVERY entry (files AND folders) under
    folder_item -- deliberately not limited to expected paths, so an
    unexpected extra item (e.g. a "payload (1)" duplicate folder, which
    is exactly what Explorer creates when it DOESN'T merge) is visible
    too, not just missing/wrong-size ones."""
    result = {}
    for child in folder_item.GetFolder.Items():
        rel = f"{prefix}{child.Name}"
        is_folder = bool(child.IsFolder)
        size = None if is_folder else child.ExtendedProperty("System.Size")
        result[rel] = {"is_folder": is_folder, "size": size, "item": child}
        if is_folder:
            result.update(walk_full_tree(child, prefix=f"{rel}/"))
    return result


def poll_until_stable(container_item, *, record) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last_sizes = None
    stable_count = 0
    tree: dict = {}
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        tree = walk_full_tree(container_item)
        sizes = {k: v["size"] for k, v in tree.items() if not v["is_folder"]}
        if sizes == last_sizes:
            stable_count += 1
        else:
            stable_count = 1
        last_sizes = sizes
        if stable_count >= STABLE_READS_REQUIRED:
            record(f"  poll: stable for {stable_count} consecutive reads "
                    f"({len(sizes)} file(s), {sum(1 for v in tree.values() if v['is_folder'])} folder(s))")
            return tree
    record(f"  poll: TIMED OUT after {POLL_TIMEOUT_SECONDS}s without stabilizing")
    return tree


def compare_tree(tree: dict, expected: dict[str, bytes], *, record) -> bool:
    """expected is {relative_path (under 'payload/'): content}. Reports
    per-file PASS/FAIL by size, plus any unexpected extra top-level items
    (duplicate-folder detection) and any expected file that's missing
    entirely."""
    ok = True

    top_level_folders = sorted(k for k, v in tree.items() if v["is_folder"] and "/" not in k)
    record(f"  top-level items in container: {top_level_folders}")
    if top_level_folders != ["payload"]:
        record(f"  FAIL: expected exactly one top-level folder named 'payload', found {top_level_folders}")
        ok = False

    observed_files = {k[len("payload/"):]: v for k, v in tree.items()
                       if not v["is_folder"] and k.startswith("payload/")}

    for rel, content in sorted(expected.items()):
        expected_size = len(content)
        entry = observed_files.get(rel)
        if entry is None:
            record(f"  FAIL: '{rel}' MISSING on device (expected {expected_size} bytes)")
            ok = False
            continue
        if entry["size"] != expected_size:
            record(f"  FAIL: '{rel}' size={entry['size']!r}, expected {expected_size} "
                    f"-- stale/unreplaced or corrupt")
            ok = False
        else:
            record(f"  OK:   '{rel}' size={entry['size']} matches expected")

    extra = sorted(set(observed_files) - set(expected))
    if extra:
        record(f"  FAIL: unexpected extra file(s) present that were never in the plan: {extra}")
        ok = False

    return ok


def main() -> int:
    log: list[str] = []

    def record(msg: str):
        line = f"[{now()}] {msg}"
        log.append(line)
        print(line)

    record("=== MTP folder-merge test starting ===")
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
    record("Device fingerprint confirmed.")

    sd_card = next((i for i in device.GetFolder.Items() if i.Name.startswith(TARGET_STORAGE_PREFIX)), None)
    if sd_card is None:
        record(f"ABORT: no storage starting with {TARGET_STORAGE_PREFIX!r} found.")
        return 1
    record(f"Target storage confirmed: {sd_card.Name!r}")
    sd_folder = sd_card.GetFolder

    LOCAL_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    suffix = rand_suffix()
    container_name = f"switchagent_mergetest_{suffix}"

    record(f"Calling NewFolder({container_name!r}) on {sd_card.Name!r}...")
    sd_folder.NewFolder(container_name)
    container_item = None
    for _ in range(10):
        time.sleep(0.5)
        container_item = next((i for i in sd_folder.Items() if i.Name == container_name), None)
        if container_item is not None:
            break
    if container_item is None:
        record(f"RESULT: NewFolder() FAILED -- '{container_name}' never appeared. Stopping.")
        return 1
    record(f"Container folder confirmed present: '{sd_card.Name}/{container_name}'")

    all_ok = True

    for batch_n in (1, 2, 3):
        record(f"\n=== Batch {batch_n}: building local payload ===")
        payload_dir = build_local_payload(batch_n)
        this_batch_files = sorted(rel for b, rel, _ in FILE_PLAN if b == batch_n)
        record(f"Local payload built at {payload_dir} with {len(this_batch_files)} file(s): {this_batch_files}")

        record(f"=== Batch {batch_n}: CopyItem('payload' folder) -> container ===")
        try:
            copy_folder_into(payload_dir, container_item, "payload", record)
        except Exception as exc:  # noqa: BLE001 -- diagnostic script, report and keep going to verification
            record(f"  EXCEPTION during copy: {type(exc).__name__}: {exc}")
            all_ok = False

        record(f"=== Batch {batch_n}: polling device tree ===")
        tree = poll_until_stable(container_item, record=record)

        record(f"=== Batch {batch_n}: comparing against expected cumulative state ===")
        expected = expected_after(batch_n)
        batch_ok = compare_tree(tree, expected, record=record)
        record(f"=== Batch {batch_n} result: {'PASS' if batch_ok else 'FAIL'} ===")
        all_ok = all_ok and batch_ok

    # -- Final content-level verification: download every file back and
    # compare its real SHA-256 against the expected content's own hash --
    # stronger than the size-only check above.
    record("\n=== Final content verification: downloading every file back ===")
    final_expected = expected_after(3)
    final_tree = walk_full_tree(container_item)
    download_dir = LOCAL_SCRATCH_DIR / f"mergecopy_downloaded_{suffix}"
    download_dir.mkdir(parents=True, exist_ok=True)

    content_ok = True
    for rel, content in sorted(final_expected.items()):
        key = f"payload/{rel}"
        entry = final_tree.get(key)
        if entry is None or entry["is_folder"]:
            record(f"  FAIL: '{rel}' not found on device for download -- cannot verify content")
            content_ok = False
            continue
        expected_hash = hashlib.sha256(content).hexdigest()
        local_target_dir = download_dir / Path(rel).parent
        local_target_dir.mkdir(parents=True, exist_ok=True)
        try:
            local_path = download_file(entry["item"], local_target_dir)
            actual_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
        except Exception as exc:  # noqa: BLE001
            record(f"  FAIL: '{rel}' download raised {type(exc).__name__}: {exc}")
            content_ok = False
            continue
        if actual_hash == expected_hash:
            record(f"  OK:   '{rel}' content hash matches ({actual_hash[:16]}...)")
        else:
            record(f"  FAIL: '{rel}' content hash mismatch -- expected {expected_hash[:16]}..., "
                    f"got {actual_hash[:16]}...")
            content_ok = False

    all_ok = all_ok and content_ok

    record("\n=== SUMMARY ===")
    record(f"Overall result: {'PASS -- folder-level CopyItem() merges safely on this device' if all_ok else 'FAIL -- see failures above'}")
    record(f"Test container left in place at: '{sd_card.Name}/{container_name}' -- NOT deleted, per instructions.")
    record(f"Downloaded verification copies left at: {download_dir}")

    log_path = LOCAL_SCRATCH_DIR / f"mergetest-log-{suffix}.txt"
    log_path.write_text("\n".join(log), encoding="utf-8")
    record(f"\nFull log saved to: {log_path}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
