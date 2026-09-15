"""Builds SwitchAgent-Setup-x64.exe by invoking Inno Setup's ISCC.exe with
the correct /DMyAppVersion automatically computed from the installed
package's own metadata (ultimately pyproject.toml's [project] version --
see switchagent/__init__.py).

Exists so a local developer build never needs to manually type
`/DMyAppVersion=X.Y.Z` (a real, found-during-audit manual-sync risk: a
copy-pasted literal version left over from a previous release would
silently build an installer whose AppVersion doesn't match the actual
EXE's own embedded version resource, which IS always computed correctly
by SwitchAgent.spec). CI (.github/workflows/release-windows.yml) computes
its own version the same way, independently, for the same reason.

Usage (from the project root, after
`pyinstaller --noconfirm packaging/SwitchAgent.spec`):
    python packaging/build_installer.py
    python packaging/build_installer.py --iscc "C:\\path\\to\\ISCC.exe"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ISS_PATH = PROJECT_ROOT / "packaging" / "SwitchAgent.iss"

_CANDIDATE_ISCC_PATHS = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
]


def _find_iscc() -> Path | None:
    for candidate in _CANDIDATE_ISCC_PATHS:
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iscc", type=Path, default=None, help="path to ISCC.exe (auto-detected if omitted)")
    args = parser.parse_args()

    iscc_path = args.iscc or _find_iscc()
    if iscc_path is None or not iscc_path.is_file():
        print(
            "error: could not find ISCC.exe -- install Inno Setup 6 "
            "(winget install JRSoftware.InnoSetup) or pass --iscc explicitly",
            file=sys.stderr,
        )
        return 1

    app_version = _pkg_version("switchagent")
    print(f"Building installer for version {app_version} using {iscc_path}")

    result = subprocess.run(
        [str(iscc_path), f"/DMyAppVersion={app_version}", str(ISS_PATH)],
        cwd=PROJECT_ROOT,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
