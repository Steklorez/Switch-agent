from .base import DeviceInfo, MtpBackend, StorageInfo, TransferResult, TransferStatus
from .errors import (
    DestinationNotFoundError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    FileAlreadyExistsError,
    InvalidOperationError,
    MtpError,
    StorageNotFoundError,
    TransferFailedError,
)
from .mock import MockMtpBackend, MockStorage

# RealMtpBackend touches pywin32/Shell.Application at import time (module-
# level, inside its own methods -- lazily, not at package-import time), but
# it's still Windows-only by nature. Imported directly here since this
# project only ever runs on Windows (see CLAUDE.md) -- not worth a
# try/except ImportError shim for a platform that will never be anything
# else.
from .windows import RealMtpBackend

__all__ = [
    "MtpBackend",
    "DeviceInfo",
    "StorageInfo",
    "TransferResult",
    "TransferStatus",
    "MtpError",
    "DeviceNotFoundError",
    "DeviceDisconnectedError",
    "StorageNotFoundError",
    "DestinationNotFoundError",
    "FileAlreadyExistsError",
    "TransferFailedError",
    "InvalidOperationError",
    "MockMtpBackend",
    "MockStorage",
    "RealMtpBackend",
]
