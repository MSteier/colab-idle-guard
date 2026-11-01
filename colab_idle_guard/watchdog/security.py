"""Local API token generation and protected storage.

The token is 256 bits from :mod:`secrets`. On Windows it is stored encrypted with
DPAPI (CryptProtectData, current-user scope), so other Windows accounts and
offline copies of the file cannot read it. Elsewhere (CI, tests) a plain file
with restrictive permissions is used and flagged as unprotected.
"""

from __future__ import annotations

import ctypes
import hmac
import os
import secrets
import sys
from ctypes import wintypes
from pathlib import Path

TOKEN_BYTES = 32
_DPAPI_MAGIC = b"CIGDPAPI1"
_PLAIN_MAGIC = b"CIGPLAIN1"
_ENTROPY = b"colab-idle-guard/api-token/v1"


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def tokens_equal(supplied: str | None, expected: str) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class DpapiError(OSError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_available() -> bool:
    return sys.platform == "win32"


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _crypt(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    func.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    func.restype = wintypes.BOOL
    in_blob, _in_buf = _blob(data)
    entropy_blob, _ent_buf = _blob(_ENTROPY)
    out_blob = _DataBlob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    description = ctypes.c_wchar_p("Colab Idle Guard API token") if protect else None
    ok = func(ctypes.byref(in_blob), description, ctypes.byref(entropy_blob), None, None,
              CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob))
    if not ok:
        raise DpapiError(ctypes.get_last_error(), "DPAPI call failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def dpapi_protect(data: bytes) -> bytes:
    return _crypt(data, protect=True)


def dpapi_unprotect(data: bytes) -> bytes:
    return _crypt(data, protect=False)


class TokenStore:
    """Loads, creates and rotates the API token file."""

    def __init__(self, path: Path, use_dpapi: bool | None = None) -> None:
        self._path = path
        self._use_dpapi = dpapi_available() if use_dpapi is None else use_dpapi

    @property
    def protected(self) -> bool:
        return self._use_dpapi

    def load_or_create(self) -> str:
        if self._path.exists():
            try:
                return self._read()
            except (OSError, ValueError, UnicodeDecodeError):
                # Corrupt or written by another Windows user: issue a new token.
                # Integrations will need re-pairing; this is logged by the caller.
                pass
        return self.rotate()

    def rotate(self) -> str:
        token = generate_token()
        self._write(token)
        return token

    def _read(self) -> str:
        raw = self._path.read_bytes()
        if raw.startswith(_DPAPI_MAGIC):
            token = dpapi_unprotect(raw[len(_DPAPI_MAGIC):]).decode("utf-8")
        elif raw.startswith(_PLAIN_MAGIC):
            token = raw[len(_PLAIN_MAGIC):].decode("utf-8")
        else:
            raise ValueError("unrecognised token file format")
        if len(token) < 32:
            raise ValueError("token too short")
        return token

    def _write(self, token: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = token.encode("utf-8")
        payload = _DPAPI_MAGIC + dpapi_protect(data) if self._use_dpapi else _PLAIN_MAGIC + data
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        if not self._use_dpapi:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, self._path)
