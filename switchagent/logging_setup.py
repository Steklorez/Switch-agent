"""Rotating file logging for the packaged desktop app (switchagent/desktop.py).

Not used by the CLI (switchagent/cli.py), which keeps its existing
console-only output unchanged -- this exists specifically because a
windowed/no-console packaged EXE has nowhere else useful to put log
output (Packaging Stage 4).

Attaches its rotating file handler to the ROOT logger, not just the
"switchagent" one -- this matters more than it looks. A PyInstaller
windowed build (console=False) launched normally (double-click, Start
Menu shortcut, or anything that doesn't explicitly redirect stdio) runs
with sys.stdout/sys.stderr set to None. If no handler is attached
anywhere in a given logger's hierarchy, Python's own fallback
(logging.lastResort, a StreamHandler bound to sys.stderr) tries to write
to that None stream and raises AttributeError the moment anything logs
at WARNING level or above -- this includes uvicorn's own loggers
("uvicorn", "uvicorn.error", "uvicorn.access"), which are NOT children of
"switchagent". Configuring the root logger here (desktop.py additionally
passes log_config=None to uvicorn.Config() to skip uvicorn's own
stdout-touching formatter setup) means every logger in the process,
including third-party ones, always has somewhere safe to go.

Deliberately logs only paths and the runtime mode, never a raw device_id
-- device-identifying log lines already go through
switchagent/mtp/windows.py's mask_device_id()/device_fingerprint(), which
this module does not touch or duplicate.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from . import __version__, config, paths

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_BYTES = 5_000_000
_BACKUP_COUNT = 3

_configured_path: Optional[Path] = None


def configure_logging(log_dir: Optional[Path] = None, *, level: int = logging.INFO) -> Path:
    """Configures the root "switchagent" logger (parent of every module
    logger in this package, e.g. "switchagent.web", "switchagent.mtp.windows")
    with a rotating file handler under log_dir (default: config.LOGS_DIR).
    Idempotent -- calling it again (e.g. from tests, or a second call in
    the same process) does not stack duplicate handlers, it just returns
    the already-configured path.
    """
    global _configured_path

    log_dir = log_dir if log_dir is not None else config.LOGS_DIR
    log_path = log_dir / "switchagent.log"

    if _configured_path == log_path:
        return log_path

    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()  # root -- see module docstring for why
    logger.setLevel(level)

    for existing in list(logger.handlers):
        if isinstance(existing, logging.handlers.RotatingFileHandler):
            logger.removeHandler(existing)
            existing.close()

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    _configured_path = log_path

    logger.info("SwitchAgent %s starting -- mode=%s", __version__, paths.runtime_mode())
    logger.info("resource_root=%s", paths.resource_root())
    logger.info("app_data_root=%s", paths.app_data_root())
    return log_path
