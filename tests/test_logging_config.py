"""Tests for claude_swap.logging_config."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from claude_swap.logging_config import _LazyDirRotatingFileHandler, setup_logging


def test_setup_does_not_create_dir(tmp_path: Path):
    """Calling setup_logging must not materialize the log directory.

    The log dir lives under the cswap backup root; pre-creating it laid down
    cache/log artifacts that later tripped the legacy → XDG migration
    collision check (see paths.migrate_legacy_backup_dir).
    """
    log_dir = tmp_path / "should-not-exist"
    logger = setup_logging(log_dir)
    try:
        assert not log_dir.exists()
        # File handler is registered but stays unopened until first emit.
        assert logger.handlers
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def test_dir_is_created_on_first_log(tmp_path: Path):
    log_dir = tmp_path / "lazy"
    logger = setup_logging(log_dir)
    try:
        assert not log_dir.exists()
        logger.warning("trigger")
        for handler in logger.handlers:
            handler.flush()
        assert log_dir.is_dir()
        assert (log_dir / "claude-swap.log").exists()
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def test_blocked_rollover_keeps_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A rollover the OS refuses must not lose records.

    On Windows, os.rename raises a sharing violation while another process
    (the installed service holds the log open; concurrent CLIs open the same
    file) has the log open, so every rollover attempt failed and each record
    after it was dropped — the decision log went silently dark. The handler
    must swallow the failure and keep appending past the size cap.
    """

    def fail_rename(src: str, dst: str) -> None:
        raise PermissionError("sharing violation")

    monkeypatch.setattr(os, "rename", fail_rename)
    log_file = tmp_path / "logs" / "claude-swap.log"
    handler = _LazyDirRotatingFileHandler(
        log_file, maxBytes=1, backupCount=3, delay=True
    )
    logger = logging.getLogger("claude-swap-rollover-test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info("first record")
        logger.info("second record")
        handler.flush()
        text = log_file.read_text(encoding="utf-8")
        assert "first record" in text
        assert "second record" in text
        assert not (tmp_path / "logs" / "claude-swap.log.1").exists()
    finally:
        handler.close()
        logger.removeHandler(handler)


def test_rollover_tolerates_second_handle_on_real_os(tmp_path: Path):
    """Rollover behavior with a concurrent handle, against the real OS.

    test_blocked_rollover_keeps_logging fakes os.rename; this one holds a
    second open handle on the log — the production shape of the engine and
    a concurrent CLI sharing the file — and lets the OS decide. On Windows
    the rename raises a real sharing violation, so no backup may appear and
    every record must still land in the main file. On POSIX renaming an
    open file is legal, so the size cap must produce a normal rollover.
    """
    log_file = tmp_path / "logs" / "claude-swap.log"
    handler = _LazyDirRotatingFileHandler(
        log_file, maxBytes=1024, backupCount=3, delay=True
    )
    logger = logging.getLogger("claude-swap-real-rollover-test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info("prime the file so it exists before the second handle")
        with open(log_file, encoding="utf-8"):
            # ~100 bytes per formatted record and maxBytes=1024: several
            # rollover attempts happen while the handle is held.
            for i in range(100):
                logger.info("record %03d: %s", i, "x" * 64)
            handler.flush()

        backup = tmp_path / "logs" / "claude-swap.log.1"
        text = log_file.read_text(encoding="utf-8")
        if sys.platform == "win32":
            # Sharing violation path: rollover is refused, yet no record may
            # be dropped — the handler reopens and keeps appending.
            assert not backup.exists()
            assert "record 000" in text
            assert "record 099" in text
        else:
            # flock-free POSIX rename: the cap must actually rotate, and the
            # newest record lives in the fresh main file.
            assert backup.exists()
            assert "record 099" in text
    finally:
        handler.close()
        logger.removeHandler(handler)


def test_debug_adds_console_handler(tmp_path: Path):
    log_dir = tmp_path / "dbg"
    logger = setup_logging(log_dir, debug=True)
    try:
        assert logger.level == logging.DEBUG
        assert any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in logger.handlers
        )
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
