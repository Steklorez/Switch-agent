"""MTP-layer error types.

One base class so calling code can catch broadly (`except MtpError`) when it
doesn't care about the specifics, and specific subclasses so it can when it
does. Every MtpBackend implementation -- mock now, a real Windows driver
later -- must raise these, not backend-internal exceptions, so callers never
need to know or care which backend they're talking to.
"""

from __future__ import annotations


class MtpError(Exception):
    """Base class for every MTP-layer error."""


class DeviceNotFoundError(MtpError):
    """No Switch/DBI MTP Responder could be found to connect to."""


class DeviceDisconnectedError(MtpError):
    """The device was connected but is no longer reachable -- either the
    connection dropped between calls, or it dropped mid-operation."""


class StorageNotFoundError(MtpError):
    """The requested storage (e.g. "SD_CARD"/"SD_INSTALL") does not exist on
    this device, or isn't visible in the current connection."""


class DestinationNotFoundError(MtpError):
    """The requested destination path does not exist on the target storage,
    and this operation does not create it (see MtpBackend.ensure_directory
    for the operation that does)."""


class FileAlreadyExistsError(MtpError):
    """The destination file already exists and the caller did not ask to
    overwrite it. Mirrors the project's "never overwrite silently" rule --
    callers decide, the backend never guesses."""


class TransferFailedError(MtpError):
    """A transfer did not complete successfully -- covers corruption
    detected on verification, a simulated/real device error, or any other
    reason bytes did not arrive intact. Never raised for a transfer that
    actually succeeded."""


class InvalidOperationError(MtpError):
    """The operation was called in a state that doesn't make sense for it --
    e.g. before connect(), or with a malformed path."""


class UnsupportedOperationError(MtpError):
    """The connected adapter cannot safely perform this operation."""


class SourceNotFoundError(MtpError):
    """A requested source object no longer exists."""


class AmbiguousPathError(MtpError):
    """Multiple objects match a path; selecting one would be unsafe."""


class StaleSessionError(MtpError):
    """A reference belongs to an expired connection."""


class OperationCancelledError(MtpError):
    """The caller cancelled an operation."""


class ReadAccessDeniedError(MtpError):
    """Device refused access to the requested source."""
