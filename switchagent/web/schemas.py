"""Pydantic request models for mutating endpoints only. GET responses are
plain dicts built by services.py -- FastAPI serializes those directly, and
a response schema would just duplicate services.py's dict shape for no
real benefit at this project's size (see docs/WEB-UI.md).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class CreateJobsRequest(BaseModel):
    library_item_ids: list[int] = Field(min_length=1)
    target_device_id: str = Field(min_length=1)


class RenameDeviceRequest(BaseModel):
    friendly_name: Optional[str] = None


class ConflictPolicyRequest(BaseModel):
    policy: Literal["skip", "override"]


class ScanTriggerResponse(BaseModel):
    started: bool


class HistoryVerificationRequest(BaseModel):
    """UI-003: the user's own console-result confirmation for a
    DONE_UNVERIFIED install_history row. `outcome: null` (or omitting the
    field) clears a previous confirmation back to unconfirmed -- both mean
    the same thing here, there is no separate "leave unchanged" case in
    this API."""
    outcome: Optional[Literal["SUCCESS", "FAILED"]] = None


class DeviceStorageMappingRequest(BaseModel):
    """UI-007 manual override. Further validated in services.py
    (logical_name restricted to SD_CARD/SD_INSTALL, raw_storage_name
    restricted to a storage this device is currently known to actually
    report) -- this schema only enforces "both fields are non-empty
    strings", not the business rules."""
    raw_storage_name: str = Field(min_length=1)
    logical_name: str = Field(min_length=1)


class DeviceStorageMappingClearRequest(BaseModel):
    raw_storage_name: str = Field(min_length=1)


class LibraryDirRequest(BaseModel):
    """W3-002: raw user text-input path, further validated in services.py
    (exists / is a directory / readable / resolved to a canonical path).

    `path` is deliberately NOT constrained to a non-empty string here.
    `paths: []` is a meaningful request -- "remove my last Library folder"
    -- and it has no sensible primary path to carry alongside it; rejecting
    it at the schema would produce FastAPI's structured 422 rather than a
    sentence anyone can read. services.py still refuses an empty path when
    `paths` is absent, with its own "a folder path is required"."""
    path: str = ""
    paths: list[str] | None = None


class PreferencesRequest(BaseModel):
    auto_scan: bool
    scan_interval: int = Field(ge=10, le=3600)
    covers: bool
