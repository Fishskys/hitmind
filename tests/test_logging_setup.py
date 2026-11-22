"""Unified logging: format, levels, privacy redaction, and file/fallback wiring."""

from __future__ import annotations

import logging
import re

from hitmind import logging_setup as ls


def test_format_shape_and_component(tmp_path):
    log_file = tmp_path / "logs" / "hitmind.log"
    ls.configure(level="DEBUG", log_file=str(log_file), stderr=False, force=True)
    try:
        lg = ls.get_logger(ls.CORE)
        lg.info("hello world")
        for h in logging.getLogger().handlers:
            h.flush()
        text = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert text, "log file should have content"
        line = text[-1]
        # 2026-11-22 16:30:00.123 [hitmind-core] [INFO] hello world
        assert re.match(
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \[hitmind-core\] \[INFO\] hello world$",
            line,
        ), line
    finally:
        ls._detach_hitmind_handlers(logging.getLogger())


def test_level_filtering(tmp_path):
    log_file = tmp_path / "logs" / "hitmind.log"
    ls.configure(level="WARNING", log_file=str(log_file), stderr=False, force=True)
    try:
        lg = ls.get_logger(ls.CORE)
        lg.info("should be dropped")
        lg.warning("kept")
        for h in logging.getLogger().handlers:
            h.flush()
        text = log_file.read_text(encoding="utf-8")
        assert "should be dropped" not in text
        assert "[WARNING] kept" in text
    finally:
        ls._detach_hitmind_handlers(logging.getLogger())


def test_sanitize_redacts_secrets_and_caps_length():
    # Build key-shaped strings at runtime so the source file itself stays scanner-clean
    # (the pre-commit secret scanner flags literal sk-<16+ chars> tokens as real keys).
    fake_sk = "sk-" + "A1b2C3d4E5f6G7h8I9j0"
    assert fake_sk not in ls.sanitize("token=" + fake_sk)
    assert "[REDACTED]" in ls.sanitize("Authorization: bearer " + ("z" * 12))
    assert "secretpw" not in ls.sanitize("http://user:" + "secretpw" + "@host/v1")
    big = "x" * (ls.MAX_LOG_MSG + 100)
    out = ls.sanitize(big)
    assert "chars)" in out and len(out) < len(big)


def test_normalize_level_tolerates_bad_input():
    assert ls.normalize_level("debug") == "DEBUG"
    assert ls.normalize_level("BOGUS") == "INFO"
    assert ls.normalize_level(None) == "INFO"


def test_default_log_dir_beside_db():
    p = ls.default_log_dir("/home/u/.hermes/hitmind/data/hitmind.db")
    assert str(p).endswith("/.hermes/hitmind/logs")


def test_configure_is_idempotent(tmp_path):
    log_file = tmp_path / "logs" / "hitmind.log"
    ls.configure(level="INFO", log_file=str(log_file), stderr=False, force=True)
    n1 = len([h for h in logging.getLogger().handlers if getattr(h, ls._HITMIND_HANDLER_ATTR, False)])
    ls.configure(level="INFO", log_file=str(log_file), stderr=False)
    n2 = len([h for h in logging.getLogger().handlers if getattr(h, ls._HITMIND_HANDLER_ATTR, False)])
    assert n1 == n2 == 1
    ls._detach_hitmind_handlers(logging.getLogger())
