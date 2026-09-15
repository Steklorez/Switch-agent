"""Tests for switchagent/logging_setup.py."""

from __future__ import annotations

import logging
import logging.handlers

from switchagent import logging_setup


def _reset():
    logging_setup._configured_path = None
    logger = logging.getLogger()  # root -- see logging_setup's own docstring for why
    for handler in list(logger.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()


def test_configure_logging_creates_the_log_file(tmp_path):
    _reset()
    log_path = logging_setup.configure_logging(tmp_path)

    assert log_path == tmp_path / "switchagent.log"
    assert log_path.exists()


def test_configure_logging_writes_a_startup_line_with_version_and_mode(tmp_path):
    _reset()
    from switchagent import __version__

    logging_setup.configure_logging(tmp_path)

    content = (tmp_path / "switchagent.log").read_text(encoding="utf-8")
    assert __version__ in content
    assert "mode=dev" in content


def test_configure_logging_is_idempotent_for_the_same_directory(tmp_path):
    _reset()
    logging_setup.configure_logging(tmp_path)
    logging_setup.configure_logging(tmp_path)  # must not raise, must not duplicate handlers

    logger = logging.getLogger()
    file_handlers = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1


def test_a_switchagent_child_logger_message_reaches_the_configured_file(tmp_path):
    """switchagent.web / switchagent.mtp.windows / switchagent.queue_worker
    all propagate to the root logger this module configures -- a message
    logged through one of them must land in the same file."""
    _reset()
    logging_setup.configure_logging(tmp_path)

    logging.getLogger("switchagent.web").info("hello from a child logger")

    content = (tmp_path / "switchagent.log").read_text(encoding="utf-8")
    assert "hello from a child logger" in content


def test_an_unrelated_third_party_logger_also_reaches_the_configured_file(tmp_path):
    """The fix is attaching to the ROOT logger, not just "switchagent" --
    this is what lets uvicorn's own loggers ("uvicorn", "uvicorn.error",
    "uvicorn.access", none of which are switchagent's children) land
    somewhere safe too, instead of falling through to Python's
    logging.lastResort (a StreamHandler bound to sys.stderr, which is None
    in a windowed PyInstaller build with no inherited console -- see
    tests/test_desktop.py's uvicorn/log_config regression tests for the
    concrete crash this avoids)."""
    _reset()
    logging_setup.configure_logging(tmp_path)

    logging.getLogger("uvicorn.access").info("hello from an unrelated logger")

    content = (tmp_path / "switchagent.log").read_text(encoding="utf-8")
    assert "hello from an unrelated logger" in content
