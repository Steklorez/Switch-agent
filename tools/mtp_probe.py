"""Stage 5A diagnostic probe -- Windows Shell/WPD MTP device visibility.

READ-ONLY. Never writes, creates, deletes, or modifies anything on any
device or on disk (other than the plain-text report file this script
itself writes locally, if --save is given). Uses pywin32's
win32com.client to drive Shell.Application -- the same COM approach
already proven in Projects/Switch/sync-to-switch.ps1 and documented in
docs/RESEARCH-STAGE1.md -- not raw WPD COM, and does not touch
switchagent/mtp/base.py or any other project module. This is a standalone
research tool, not part of the SwitchAgent package.

Usage:
    python tools/mtp_probe.py                  print a full report
    python tools/mtp_probe.py --device-only     just identity + reconnect-relevant fields
    python tools/mtp_probe.py --mask-serial     redact the serial-like segment of device paths

Deliberately does NOT:
  - copy, create, delete, or rename anything
  - install/launch anything
  - assume a specific device name ("Switch") is present -- reports whatever
    non-filesystem, non-drive-letter items it finds under "This PC"
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

import win32com.client

CIM_THIS_PC = 0x11

# Canonical Shell/WPD property names we probe for -- per Stage 5A task
# instructions, every one of these is reported, with "NOT AVAILABLE" for
# anything that comes back empty/None/erroring. Nothing here is invented;
# every name below is a real Microsoft canonical property system key.
DEVICE_PROPERTY_KEYS = [
    "System.ItemNameDisplay",
    "System.ParsingName",
    "System.ItemPathDisplay",
    "System.ItemFolderPathDisplay",
    "System.ItemType",
    "System.ItemTypeText",
    "System.Devices.DeviceInstanceId",
    "System.Devices.ContainerId",
    "System.Devices.FriendlyName",
    "System.Devices.ModelName",
    "System.Devices.Manufacturer",
    "System.Devices.DeviceHasProblem",
    "System.Devices.Category",
    "System.Devices.CompatibleIds",
    "System.Devices.InterfacePaths",
    "System.Devices.HardwareIds",
]

STORAGE_PROPERTY_KEYS = [
    "System.ItemNameDisplay",
    "System.Size",
    "System.FreeSpace",
    "System.Capacity",
    "System.ItemTypeText",
    "System.Devices.DeviceInstanceId",
]

_SERIAL_SEGMENT_RE = re.compile(r"(usb#vid_[0-9a-f]{4}&pid_[0-9a-f]{4}#)([^#]+)(#\{)", re.IGNORECASE)


def mask_serial(path_value: Optional[str]) -> Optional[str]:
    """Redacts the serial-like segment of a WPD-style Shell path
    (usb#vid_XXXX&pid_XXXX#<SERIAL>#{guid}) -- used only when printing/
    saving a report, never for the actual identity comparison logic."""
    if not path_value:
        return path_value
    return _SERIAL_SEGMENT_RE.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", path_value)


def device_fingerprint(path_value: str) -> str:
    """Short, safe-to-print stand-in for comparing device identity across
    runs/devices at a glance -- SHA-256 of the full .Path, truncated. Never
    reveals the real serial (one-way hash), but two devices (or two probes
    of the same device) can be compared just by eyeballing whether this
    matches, without ever needing the masked-but-still-partially-visible
    Path string. Stage 5A.1 was added specifically because comparing two
    physical Switches by eye against a masked Path is error-prone (a
    transcription mistake during Stage 5A's own reconnect test produced a
    false mismatch that had to be re-verified programmatically) -- this
    fingerprint exists so that mistake is harder to repeat."""
    return hashlib.sha256(path_value.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def extended_property(item, key: str):
    try:
        val = item.ExtendedProperty(key)
    except Exception as exc:  # noqa: BLE001 -- this is a diagnostic probe, any COM failure is just "not available"
        return f"NOT AVAILABLE (error: {type(exc).__name__})"
    if val is None or val == "":
        return "NOT AVAILABLE"
    return val


@dataclass
class DeviceReport:
    name: str
    path: str
    is_folder: bool
    is_file_system: bool
    type_text: str
    properties: dict = field(default_factory=dict)
    storages: list = field(default_factory=list)


def find_this_pc_folder():
    shell = win32com.client.Dispatch("Shell.Application")
    return shell, shell.NameSpace(CIM_THIS_PC)


def list_this_pc_items(this_pc) -> list[dict]:
    out = []
    for item in this_pc.Items():
        out.append({
            "name": item.Name,
            "path": item.Path,
            "is_folder": bool(item.IsFolder),
            "is_file_system": bool(item.IsFileSystem),
        })
    return out


def find_mtp_devices(this_pc) -> list:
    """MTP/WPD devices under This PC are folders that are NOT drive-letter
    file-system volumes -- that's the same distinguishing signal
    watch-switch-connect.ps1 already relies on (checking presence by Name,
    which only works because these items are not ordinary drives)."""
    devices = []
    for item in this_pc.Items():
        if item.IsFolder and not item.IsFileSystem:
            devices.append(item)
    return devices


def probe_device(item) -> DeviceReport:
    props = {key: extended_property(item, key) for key in DEVICE_PROPERTY_KEYS}
    report = DeviceReport(
        name=item.Name,
        path=item.Path,
        is_folder=bool(item.IsFolder),
        is_file_system=bool(item.IsFileSystem),
        type_text=str(item.Type),
        properties=props,
    )
    try:
        folder = item.GetFolder
        for child in folder.Items():
            storage_props = {key: extended_property(child, key) for key in STORAGE_PROPERTY_KEYS}
            report.storages.append({
                "name": child.Name,
                "path": child.Path,
                "is_folder": bool(child.IsFolder),
                "is_file_system": bool(child.IsFileSystem),
                "properties": storage_props,
            })
    except Exception as exc:  # noqa: BLE001
        report.storages.append({"error": f"could not enumerate storages: {exc}"})
    return report


def probe_storage_contents(storage_item, *, max_entries: int = 25) -> list[dict]:
    """Read-only: lists top-level entries of a storage node. Does not
    descend, does not open/read file contents, does not write anything."""
    entries = []
    try:
        folder = storage_item.GetFolder
        for i, child in enumerate(folder.Items()):
            if i >= max_entries:
                entries.append({"note": f"... truncated after {max_entries} entries"})
                break
            size_ext = extended_property(child, "System.Size")
            entries.append({
                "name": child.Name,
                "is_folder": bool(child.IsFolder),
                "size_via_extended_property": size_ext,
            })
    except Exception as exc:  # noqa: BLE001
        entries.append({"error": str(exc)})
    return entries


def print_report(*, mask: bool) -> None:
    def m(value):
        return mask_serial(value) if mask and isinstance(value, str) else value

    shell, this_pc = find_this_pc_folder()
    print("=== This PC: all top-level items ===")
    for entry in list_this_pc_items(this_pc):
        entry = dict(entry)
        entry["path"] = m(entry["path"])
        print(f"  name={entry['name']!r} is_folder={entry['is_folder']} "
              f"is_file_system={entry['is_file_system']} path={entry['path']!r}")

    devices = find_mtp_devices(this_pc)
    print(f"\n=== Detected {len(devices)} non-filesystem folder item(s) (candidate MTP/WPD devices) ===")

    for item in devices:
        report = probe_device(item)
        print(f"\n--- Device: {report.name!r} ---")
        print(f"  Path: {m(report.path)!r}")
        print(f"  Fingerprint (sha256[:16] of full Path -- safe to compare/share as-is): "
              f"{device_fingerprint(report.path)}")
        print(f"  Type: {report.type_text!r}")
        print("  Extended properties:")
        for key, val in report.properties.items():
            print(f"    {key} = {m(val)!r}")

        print(f"  Storage nodes ({len(report.storages)}):")
        for s in report.storages:
            if "error" in s:
                print(f"    ERROR: {s['error']}")
                continue
            print(f"    - {s['name']!r}  path={m(s['path'])!r}")
            for key, val in s["properties"].items():
                print(f"        {key} = {val!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mask-serial", action="store_true", help="redact serial-like path segments in output")
    parser.add_argument(
        "--storage-contents", metavar="STORAGE_PREFIX", default=None,
        help="also read-only list top-level entries of the first storage whose name starts with this "
             "(e.g. '1:' for SD Card) -- still never writes anything",
    )
    parser.add_argument(
        "--device-match", metavar="SUBSTRING", default=None,
        help="required when more than one MTP device is connected at once (not the normal "
             "sequential-use case for this project, but the probe must not silently guess): "
             "a substring to match against a device's .Path, used together with "
             "--storage-contents to pick which device to inspect. Never an index/position.",
    )
    args = parser.parse_args()

    print_report(mask=args.mask_serial)

    if args.storage_contents:
        shell, this_pc = find_this_pc_folder()
        devices = find_mtp_devices(this_pc)
        if not devices:
            print("\nNo devices found -- cannot list storage contents.")
            return 1
        if len(devices) > 1:
            if not args.device_match:
                print(
                    f"\n{len(devices)} devices are connected at once -- refusing to guess which one "
                    "for --storage-contents. Re-run with --device-match <substring-of-.Path> "
                    "(identity-based, never a position/index) to disambiguate."
                )
                return 1
            matching = [d for d in devices if args.device_match in d.Path]
            if len(matching) != 1:
                print(
                    f"\n--device-match {args.device_match!r} matched {len(matching)} device(s), "
                    "expected exactly 1 -- refusing to guess."
                )
                return 1
            device = matching[0]
        else:
            device = devices[0]
        folder = device.GetFolder
        match = next((i for i in folder.Items() if i.Name.startswith(args.storage_contents)), None)
        if match is None:
            print(f"\nNo storage starting with {args.storage_contents!r} found.")
            return 1
        print(f"\n=== Top-level contents of {match.Name!r} (read-only) ===")
        for entry in probe_storage_contents(match):
            print(" ", entry)

    return 0


if __name__ == "__main__":
    sys.exit(main())
