"""Bounded local output shared by WPD and the mock; never touches a device."""
from pathlib import Path
import stat
import time

from .errors import OperationCancelledError, TransferFailedError


def is_link_or_reparse(path):
    """Reject every Windows reparse type, including junctions on Python 3.11."""
    info = Path(path).lstat()
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def receive_stream(read, destination, *, size=None, progress=None, cancel=None,
                   check_session=None, chunk_size=1024 * 1024):
    destination = Path(destination)
    # Exclusive creation also refuses existing symlinks; reject parent reparse points.
    for parent in (destination.parent, *destination.parent.parents):
        if is_link_or_reparse(parent):
            raise TransferFailedError('destination traverses a link')
    created = False
    received = 0
    last_report = time.monotonic()
    try:
        with destination.open('xb') as handle:
            created = True
            while True:
                if check_session:
                    check_session()
                if cancel and cancel():
                    raise OperationCancelledError('download cancelled')
                data = read(chunk_size)
                if not data:
                    break
                received += len(data)
                if size is not None and received > size:
                    raise TransferFailedError('source exceeded its reported size')
                handle.write(data)
                if progress and time.monotonic() - last_report >= 0.25:
                    progress(received, size)
                    last_report = time.monotonic()
            if size is not None and received != size:
                raise TransferFailedError(f'short source: expected {size}, received {received}')
            if check_session:
                check_session()
            if cancel and cancel():
                raise OperationCancelledError('download cancelled')
        if progress:
            progress(received, size)
        return received
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
