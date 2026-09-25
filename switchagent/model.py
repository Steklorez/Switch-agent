"""Shared classification model used by scanner.py, extractor.py and the CLI.

One vocabulary for "what did we find", used consistently everywhere instead
of ad-hoc has_package/has_atmosphere flags scattered per format.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class ContentType(str, Enum):
    GAME_PACKAGE = "GAME_PACKAGE"
    ATMOSPHERE_MOD = "ATMOSPHERE_MOD"
    # A `switch/` folder copied verbatim onto the SD card -- a homebrew
    # port's .nro and its data (see switchagent/sd_files.py).
    SD_FILES = "SD_FILES"
    # emuiibo itself -- its sysmodule, overlay and the overlay's own files,
    # copied to where emuiibo's release lays them out (switchagent/emuiibo.py).
    EMUIIBO = "EMUIIBO"
    # Virtual amiibo for emuiibo: folders copied into emuiibo/amiibo/.
    AMIIBO = "AMIIBO"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ConflictState(str, Enum):
    NEW = "NEW"
    SAME = "SAME"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class ArchiveEntry:
    """One entry from an archive's own directory listing -- never means we
    extracted it, just that we read its declared metadata."""
    name: str  # normalized, forward-slash path as declared inside the archive
    size: int
    is_dir: bool
    is_symlink: bool
    crc32: Optional[int] = None


@dataclass
class ConflictEntry:
    relative_path: str
    state: ConflictState
    reason: Optional[str] = None


@dataclass
class AnalysisResult:
    content_type: ContentType
    status: str  # ANALYZED / NEEDS_REVIEW / ARCHIVE_PENDING / ERROR / SKIP_DUPLICATE
    suggested_action: Optional[str]      # INSTALL_VIA_DBI / COPY_MERGE / None
    suggested_target: Optional[str]      # SD_INSTALL / SD_CARD / None

    package_format: Optional[str] = None  # NSP/NSZ/XCI/XCZ
    title_id: Optional[str] = None
    title_id_source: Optional[str] = None
    title_id_confident: bool = False

    is_archive: bool = False
    archive_format: Optional[str] = None  # ZIP/7Z/RAR
    file_count: Optional[int] = None

    note: Optional[str] = None
    error: Optional[str] = None

    conflicts: list[ConflictEntry] = field(default_factory=list)

    def to_details_dict(self) -> dict:
        d = asdict(self)
        d["content_type"] = self.content_type.value
        d["conflicts"] = [
            {"relative_path": c.relative_path, "state": c.state.value, "reason": c.reason}
            for c in self.conflicts
        ]
        return d

    def to_details_json(self) -> str:
        return json.dumps(self.to_details_dict(), ensure_ascii=False)
