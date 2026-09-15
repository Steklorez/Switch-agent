"""Builds SwitchAgent-Portable-x64.zip from the PyInstaller onedir bundle
at dist/SwitchAgent/ (see SwitchAgent.spec).

Copies the bundle to a SEPARATE staging directory before adding
portable.flag -- dist/SwitchAgent/ itself must stay flag-free, since
SwitchAgent.iss's installer also consumes it as-is, and portable.flag's
mere presence next to the running executable is what switchagent/paths.py
uses to decide "portable" vs "installed" mode. Adding it to the shared
source directory would make the *installed* build think it's portable too.

Usage (from the project root, after
`pyinstaller --noconfirm packaging/SwitchAgent.spec`):
    python packaging/build_portable_zip.py
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_ROOT / "dist" / "SwitchAgent"
STAGING_DIR = PROJECT_ROOT / "dist" / "SwitchAgent-Portable"
ZIP_PATH = PROJECT_ROOT / "dist" / "SwitchAgent-Portable-x64.zip"

_PORTABLE_FLAG_CONTENTS = (
    "Marker file -- its presence next to SwitchAgent.exe means portable mode:\n"
    "persistent data (config.yaml, data/, work/, logs/) is stored in this\n"
    "same directory instead of %LOCALAPPDATA%\\SwitchAgent. See\n"
    "switchagent/paths.py. Do not delete this file unless you specifically\n"
    "want to lose that separation -- there is no supported way to convert a\n"
    "portable copy to installed-mode semantics in place.\n"
)


def main() -> int:
    if not SOURCE_DIR.is_dir():
        print(
            f"error: {SOURCE_DIR} does not exist -- build it first with "
            f"`pyinstaller --noconfirm packaging/SwitchAgent.spec`",
            file=sys.stderr,
        )
        return 1

    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    shutil.copytree(SOURCE_DIR, STAGING_DIR)

    (STAGING_DIR / "portable.flag").write_text(_PORTABLE_FLAG_CONTENTS, encoding="utf-8")
    shutil.copy2(PROJECT_ROOT / "LICENSE", STAGING_DIR / "LICENSE")
    shutil.copy2(PROJECT_ROOT / "THIRD_PARTY_NOTICES.md", STAGING_DIR / "THIRD_PARTY_NOTICES.md")

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(STAGING_DIR.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(STAGING_DIR))

    print(f"wrote {ZIP_PATH} ({ZIP_PATH.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
