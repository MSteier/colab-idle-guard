"""Token generation, DPAPI storage and log redaction."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from colab_idle_guard.watchdog.logging_manager import log_event, redactor, setup_logging
from colab_idle_guard.watchdog.security import (
    TokenStore, dpapi_protect, dpapi_unprotect, generate_token, tokens_equal,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


def test_token_strength_and_uniqueness() -> None:
    tokens = {generate_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) >= 43 for t in tokens)  # 32 random bytes, base64url


def test_tokens_equal() -> None:
    t = generate_token()
    assert tokens_equal(t, t)
    assert not tokens_equal(t[:-1] + "x", t)
    assert not tokens_equal(None, t) and not tokens_equal("", t)


@windows_only
def test_dpapi_roundtrip() -> None:
    secret = b"hello-colab"
    blob = dpapi_protect(secret)
    assert secret not in blob
    assert dpapi_unprotect(blob) == secret


@windows_only
def test_token_store_dpapi_persists_and_is_encrypted(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.bin")
    token = store.load_or_create()
    assert store.protected
    assert token.encode() not in (tmp_path / "token.bin").read_bytes()
    assert TokenStore(tmp_path / "token.bin").load_or_create() == token


def test_token_store_plain_fallback_and_rotation(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.bin", use_dpapi=False)
    token = store.load_or_create()
    assert store.load_or_create() == token
    rotated = store.rotate()
    assert rotated != token and store.load_or_create() == rotated


def test_corrupt_token_file_regenerates(tmp_path: Path) -> None:
    path = tmp_path / "token.bin"
    path.write_bytes(b"garbage")
    token = TokenStore(path, use_dpapi=False).load_or_create()
    assert len(token) >= 43


def test_logs_never_contain_token(tmp_path: Path) -> None:
    token = generate_token()
    redactor.add_secret(token)
    log_file = tmp_path / "app.log"
    logger = setup_logging(log_file, "DEBUG")
    try:
        child = logging.getLogger("colab_idle_guard.test")
        child.info("token is %s", token)
        log_event(child, "request", authorization=f"Bearer {token}", headers={"Authorization": f"Bearer {token}"},
                  note=f"x{token}y", token=token)
        child.warning("Bearer abc.def-123")
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    text = log_file.read_text(encoding="utf-8")
    assert token not in text
    assert "abc.def-123" not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert records[1]["event"] == "request" and records[1]["token"] == "[REDACTED]"
