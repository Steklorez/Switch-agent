"""Tests for switchagent/firstrun.py -- config.yaml bootstrap on first run.

The one hard invariant covered repeatedly here: an existing config.yaml is
NEVER overwritten, regardless of what the (injected) Downloads resolver
would return -- an app update must never clobber a user's own
library.source_dir.
"""

from __future__ import annotations

import yaml

from switchagent import firstrun


def test_creates_a_config_when_none_exists(tmp_path):
    config_path = tmp_path / "config.yaml"
    created = firstrun.ensure_app_config(config_path, downloads_resolver=lambda: None)

    assert created is True
    assert config_path.exists()


def test_never_overwrites_an_existing_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("library:\n  source_dir: \"E:\\\\MyOwnFolder\"\n", encoding="utf-8")

    created = firstrun.ensure_app_config(config_path, downloads_resolver=lambda: tmp_path / "Downloads")

    assert created is False
    assert "MyOwnFolder" in config_path.read_text(encoding="utf-8")


def _uncommented_source_dir(text: str) -> str:
    """The commented-out `# source_dir: "..."` suggestion firstrun writes,
    read back as if the user had uncommented it -- which is the only thing
    that line has to get right (escaping included)."""
    line = next(l for l in text.splitlines() if l.strip().startswith("# source_dir:"))
    return yaml.safe_load(line.replace("#", "", 1))["source_dir"]


def test_suggests_the_resolved_downloads_folder_without_adopting_it(tmp_path):
    """The detected Downloads folder is written as a commented suggestion,
    never as an active source_dir: adopting it silently made a fresh
    install start watching and re-walking a folder of unrelated junk from
    its very first launch. config.load_library_dir() still falls back to
    this same path to prefill Settings; what it no longer does is call it
    chosen."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    config_path = tmp_path / "config.yaml"

    firstrun.ensure_app_config(config_path, downloads_resolver=lambda: downloads)

    text = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert (data.get("library") or {}).get("source_dir") is None
    # The suggestion itself must still be there, ready to uncomment -- and
    # still correctly escaped, so uncommenting it yields the real path.
    assert _uncommented_source_dir(text) == str(downloads)


def test_omits_source_dir_when_downloads_cannot_be_resolved(tmp_path):
    config_path = tmp_path / "config.yaml"

    firstrun.ensure_app_config(config_path, downloads_resolver=lambda: None)

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert (data.get("library") or {}).get("source_dir") is None


def test_written_config_is_valid_yaml_when_downloads_is_undetected(tmp_path):
    config_path = tmp_path / "unresolved" / "config.yaml"
    firstrun.ensure_app_config(config_path, downloads_resolver=lambda: None)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "extraction" in data


def test_written_config_is_valid_yaml_when_downloads_is_detected(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    config_path = tmp_path / "resolved" / "config.yaml"
    firstrun.ensure_app_config(config_path, downloads_resolver=lambda: downloads)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "extraction" in data


def test_written_config_includes_the_default_extraction_limits(tmp_path):
    config_path = tmp_path / "config.yaml"
    firstrun.ensure_app_config(config_path, downloads_resolver=lambda: None)

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["extraction"]["max_extracted_size_bytes"] == firstrun._DEFAULT_MAX_EXTRACTED_SIZE_BYTES
    assert data["extraction"]["max_file_count"] == firstrun._DEFAULT_MAX_FILE_COUNT
    assert data["extraction"]["max_directory_depth"] == firstrun._DEFAULT_MAX_DIRECTORY_DEPTH


def test_creates_parent_directories_if_needed(tmp_path):
    config_path = tmp_path / "nested" / "dirs" / "config.yaml"
    created = firstrun.ensure_app_config(config_path, downloads_resolver=lambda: None)

    assert created is True
    assert config_path.exists()


def test_a_downloads_path_with_an_apostrophe_round_trips_through_yaml(tmp_path):
    # A double quote isn't even a legal Windows filename character -- an
    # apostrophe is the realistic "special character in a real folder
    # name" case (e.g. a OneDrive-redirected "Alex's PC" folder).
    tricky = tmp_path / "Alex's Downloads"
    tricky.mkdir()
    config_path = tmp_path / "config.yaml"

    firstrun.ensure_app_config(config_path, downloads_resolver=lambda: tricky)

    assert _uncommented_source_dir(config_path.read_text(encoding="utf-8")) == str(tricky)
