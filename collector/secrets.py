"""Windows DPAPI secrets, bound to the current Windows user. Never exported."""

from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path

KEYS = (
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
)


def _dpapi(data: bytes, decrypt=False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [
        ctypes.POINTER(Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    # LocalFree must accept a pointer on 64-bit Windows.
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(
            ctypes.byref(source), "SCUBA Collector", None, None, None, 1, ctypes.byref(output)
        )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)


class SecretStore:
    def __init__(self, home: Path):
        self.path = Path(home) / "credentials.dpapi"
        self.values = {}
        if os.name == "nt" and self.path.exists():
            self.values = json.loads(_dpapi(base64.b64decode(self.path.read_bytes()), True))

    def get(self, key: str) -> str:
        return os.environ.get(key, self.values.get(key, ""))

    def save(self, values: dict) -> None:
        self.values = {k: str(v).strip() for k, v in values.items() if k in KEYS}
        if os.name == "nt":
            raw = json.dumps(self.values).encode("utf-8")
            temporary = self.path.with_suffix(".tmp")
            temporary.write_bytes(base64.b64encode(_dpapi(raw)))
            temporary.replace(self.path)
