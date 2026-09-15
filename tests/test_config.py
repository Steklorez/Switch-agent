"""Tests for switchagent/config.py's library-directory resolution --
load_library_dir()/library_dir_info() -- covering the priority order the
packaging task requires: explicit config.yaml value > detected Windows
Downloads folder > inert placeholder, and that the placeholder is never
this project's own dev-machine "D:\\shared\\Download" default.
"""

from __future__ import annotations

from switchagent import config, known_folders


def test_explicit_config_value_always_wins_over_downloads_detection(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: downloads)

    config_path = tmp_path / "config.yaml"
    explicit = tmp_path / "MyExplicitLibrary"
    # Single-quoted YAML scalar -- a double-quoted one would try to
    # interpret this real Windows path's backslashes as escape sequences.
    config_path.write_text(f"library:\n  source_dir: '{explicit}'\n", encoding="utf-8")

    info = config.library_dir_info(config_path)

    assert info.configured is True
    assert info.path == explicit


def test_falls_back_to_detected_downloads_when_config_missing_the_section(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: downloads)

    config_path = tmp_path / "config.yaml"
    config_path.write_text("extraction:\n  max_file_count: 5\n", encoding="utf-8")

    info = config.library_dir_info(config_path)

    assert info.configured is False
    assert info.path == downloads
    assert info.exists is True


def test_falls_back_to_detected_downloads_when_config_file_absent(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: downloads)

    info = config.library_dir_info(tmp_path / "does-not-exist.yaml")

    assert info.configured is False
    assert info.path == downloads


def test_falls_back_to_an_inert_placeholder_when_nothing_can_be_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)

    info = config.library_dir_info(tmp_path / "does-not-exist.yaml")

    assert info.configured is False
    assert info.exists is False
    # Never this project's own dev-machine default (the old hardcoded
    # "D:\shared\Download" literal, removed by this refactor -- see
    # config.py's _fallback_library_dir() docstring) -- proven precisely by
    # the exact-path check below. A substring check for "shared" would be
    # a weaker, environment-fragile proxy for the same claim: it would
    # misfire on any checkout whose own path happens to contain that
    # substring for unrelated reasons (e.g. a "D:\shared\..." workspace
    # root), which is exactly what this project's own dev machine has.
    assert info.path == config.APP_DATA_ROOT / "Library"


def test_load_library_dir_returns_just_the_path(tmp_path, monkeypatch):
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    config_path = tmp_path / "config.yaml"
    library_path = tmp_path / "Games"
    config_path.write_text(f"library:\n  source_dir: '{library_path}'\n", encoding="utf-8")

    assert config.load_library_dir(config_path) == library_path


def test_exists_flag_reflects_real_filesystem_state(tmp_path, monkeypatch):
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    config_path = tmp_path / "config.yaml"
    missing = tmp_path / "GhostFolder"
    config_path.write_text(f"library:\n  source_dir: '{missing}'\n", encoding="utf-8")

    info = config.library_dir_info(config_path)

    assert info.configured is True
    assert info.exists is False


# ---------------------------------------------------------------------------
# W3-002: set_library_source_dir() -- in-place config.yaml update
# ---------------------------------------------------------------------------

def test_set_library_source_dir_creates_config_if_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    config_path = tmp_path / "config.yaml"
    new_dir = tmp_path / "NewLibrary"

    config.set_library_source_dir(new_dir, config_path)

    assert config_path.exists()
    assert config.load_library_dir(config_path) == new_dir


def test_set_library_source_dir_replaces_existing_value_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    config_path = tmp_path / "config.yaml"
    old_dir = tmp_path / "OldLibrary"
    new_dir = tmp_path / "NewLibrary"
    config_path.write_text(
        "# a user comment that must survive\n"
        "extraction:\n"
        "  max_extracted_size_bytes: 123\n"
        "\n"
        "library:\n"
        f'  source_dir: "{old_dir}"\n',
        encoding="utf-8",
    )

    config.set_library_source_dir(new_dir, config_path)

    text = config_path.read_text(encoding="utf-8")
    assert "# a user comment that must survive" in text  # comment preserved
    assert "max_extracted_size_bytes: 123" in text  # unrelated section preserved
    assert str(old_dir) not in text
    assert config.load_library_dir(config_path) == new_dir


def test_set_library_source_dir_adds_key_to_existing_library_section_without_one(tmp_path, monkeypatch):
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    config_path = tmp_path / "config.yaml"
    new_dir = tmp_path / "NewLibrary"
    config_path.write_text(
        "extraction:\n  max_file_count: 5\n\nlibrary:\n  # no source_dir set yet\n",
        encoding="utf-8",
    )

    config.set_library_source_dir(new_dir, config_path)

    assert "max_file_count: 5" in config_path.read_text(encoding="utf-8")
    assert config.load_library_dir(config_path) == new_dir


def test_set_library_source_dir_handles_a_path_with_backslashes_and_quotes(tmp_path, monkeypatch):
    """A real Windows path (backslashes) must round-trip exactly, since
    this function writes a double-quoted YAML scalar (unlike this test
    file's own fixtures elsewhere, which use single-quoted scalars)."""
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    config_path = tmp_path / "config.yaml"
    new_dir = tmp_path / "Weird Folder"
    new_dir.mkdir()

    config.set_library_source_dir(new_dir, config_path)

    assert config.load_library_dir(config_path) == new_dir
