"""Shared fixture builders for archive-based tests.

Nothing here touches the real inbox/ or data/switchagent.db -- every test
gets its own tmp_path, and DB-touching tests open a throwaway sqlite file
inside that tmp_path.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import py7zr
import pytest

from switchagent import config, db, known_folders


@pytest.fixture(autouse=True)
def _own_preferences(tmp_path_factory, monkeypatch):
    """preferences.json (auto-scan, covers, the beta features) lives in
    config.DATA_DIR -- never the real one: a test must not depend on what
    the person running it turned on in Settings, nor change it."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path_factory.mktemp("data"))


def build_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def build_zip_with_symlink(path: Path, link_name: str, target: str) -> Path:
    """A zip entry that, if extracted naively, would create a symlink.
    Emulates what a unix-made zip (e.g. via `zip --symlinks`) looks like:
    S_IFLNK encoded in the top 16 bits of external_attr, entry content is
    the link target text."""
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo(link_name)
        info.external_attr = (0xA1FF) << 16  # S_IFLNK | 0o777
        zf.writestr(info, target.encode())
    return path


def build_7z(path: Path, entries: dict[str, bytes]) -> Path:
    with py7zr.SevenZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(data, name)
    return path


def choose_library_folder(path=None):
    """Makes config.LIBRARY_DIR a folder the user has actually CHOSEN, by
    writing the config.yaml key that says so.

    The watcher deliberately refuses to run against an unchosen folder --
    with no config.yaml, config.LIBRARY_DIR falls back to the Windows
    Downloads folder, and walking that unasked is the whole problem that
    guard exists for (see WebContext.start_library_watcher). Test fixtures
    set config.LIBRARY_DIR directly, which is not the same thing, so any
    test that wants a live watcher has to say so explicitly -- exactly as a
    real user does by picking a folder in Settings."""
    from switchagent import config as config_mod

    target = path or config_mod.LIBRARY_DIR
    escaped = str(target).replace("\\", "\\\\").replace('"', '\\"')
    config_mod.CONFIG_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_mod.CONFIG_YAML_PATH.write_text(
        'library:\n  source_dir: "' + escaped + '"\n', encoding="utf-8",
    )
    return target


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """A real sqlite db in a tmp dir, with config.INBOX_DIR/WORK_DIR/
    LIBRARY_DIR also redirected there so scanner.scan_once()/
    scan_library_once() never touch the real inbox/work/data/Download
    folders."""
    inbox_dir = tmp_path / "inbox"
    work_dir = tmp_path / "work"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", work_dir)
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    # See tests/test_web_api.py's web_ctx fixture for why both of these
    # matter: without them, config.library_dir_info() would silently read
    # this machine's real config.yaml and call the real
    # SHGetKnownFolderPath instead of staying isolated in tmp_path.
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)

    db_path = tmp_path / "test.db"
    with db.open_db(db_path) as conn:
        yield conn, inbox_dir
