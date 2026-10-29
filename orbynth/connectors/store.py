"""The credential vault.

Tokens for a customer's mail, drive, CRM and code host are the most valuable data this product
touches. Two honest options exist for a local desktop app:

* **Windows** — DPAPI (``CryptProtectData``), which ties the ciphertext to the logged-in Windows
  account. A copied file is useless on another machine or another user. This is the supported
  path and the one the installer uses.
* **Everywhere else** — a file with mode ``0600`` in the app's home directory, and
  :attr:`Vault.at_rest_protected` reports ``False`` so nothing in the UI, the docs or a support
  call can claim otherwise.

Storing a value is deliberately boring: one JSON file, atomic replace, never a log line, never an
event payload. :func:`redact` is the only way a credential is allowed to appear in output.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

from .base import Connection

DPAPI_PREFIX = "dpapi:"
FILE_NAME = "connectors.vault.json"


def redact(value: str, keep: int = 4) -> str:
    """The only safe rendering of a secret: length and last few characters."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep:
        return "*" * len(text)
    return f"{'*' * max(4, len(text) - keep)}{text[-keep:]}"


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Wrap CryptProtectData/CryptUnprotectData without adding a dependency.

    Raises OSError with the Win32 code when the call fails, so a corrupt or foreign-user vault is
    reported as such instead of silently returning an empty credential set.
    """
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = ctypes.c_wchar_p("ORBYNTH")
    ok = fn(
        ctypes.byref(blob_in),
        description if protect else None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(f"DPAPI call failed with error {kernel32.GetLastError()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class Vault:
    """Reads and writes the connected-account store."""

    def __init__(self, home: str | Path = ""):
        base = Path(home) if home else Path(os.environ.get("ORBYNTH_HOME", Path.home() / ".orbynth"))
        self.path = base / FILE_NAME
        self._cache: dict[str, Connection] | None = None

    # -- capabilities -----------------------------------------------------------------
    @property
    def at_rest_protected(self) -> bool:
        """True only when the OS itself protects the file (DPAPI on Windows)."""
        return _dpapi_available()

    @property
    def protection_note(self) -> str:
        if self.at_rest_protected:
            return "Encrypted with Windows DPAPI for this Windows account."
        return (
            "Stored as a 0600 file without OS-level encryption on this platform. On Windows the "
            "installer uses DPAPI; anywhere else, treat this file like a password."
        )

    # -- io ---------------------------------------------------------------------------
    def _read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise OSError(f"cannot read the credential store at {self.path}: {exc}") from exc
        if raw.startswith(DPAPI_PREFIX.encode()):
            payload = _dpapi(base64.b64decode(raw[len(DPAPI_PREFIX) :]), protect=False)
        else:
            payload = raw
        try:
            data = json.loads(payload.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise OSError(
                f"the credential store at {self.path} is unreadable ({exc}). Move it aside and "
                "reconnect your accounts; nothing else in ORBYNTH depends on it."
            ) from exc
        return data if isinstance(data, dict) else {}

    def _write_raw(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        if _dpapi_available():
            payload = DPAPI_PREFIX.encode() + base64.b64encode(_dpapi(payload, protect=True))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as handle:
            handle.write(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    # -- api --------------------------------------------------------------------------
    def load(self) -> dict[str, Connection]:
        if self._cache is not None:
            return dict(self._cache)
        data = self._read_raw()
        out: dict[str, Connection] = {}
        for name, row in data.items():
            if not isinstance(row, dict):
                continue
            out[str(name)] = Connection(
                name=str(name),
                values={str(k): str(v) for k, v in (row.get("values") or {}).items()},
                meta={str(k): str(v) for k, v in (row.get("meta") or {}).items()},
                created_at=float(row.get("created_at", 0) or 0),
                verified_at=float(row.get("verified_at", 0) or 0),
            )
        self._cache = out
        return dict(out)

    def get(self, name: str) -> Connection | None:
        return self.load().get(str(name))

    def save(self, connection: Connection) -> Connection:
        data = self._read_raw()
        data[connection.name] = {
            "values": dict(connection.values),
            "meta": dict(connection.meta),
            "created_at": connection.created_at,
            "verified_at": connection.verified_at,
        }
        self._write_raw(data)
        self._cache = None
        return connection

    def remove(self, name: str) -> bool:
        data = self._read_raw()
        if str(name) not in data:
            return False
        data.pop(str(name), None)
        self._write_raw(data)
        self._cache = None
        return True

    def mark_verified(self, name: str, account: str = "") -> None:
        connection = self.get(name)
        if connection is None:
            return
        import time

        connection.verified_at = time.time()
        if account:
            connection.meta["account"] = account
        self.save(connection)

    def summary(self) -> list[dict[str, Any]]:
        """Safe rendering for the CLI and the HUD: keys, never values."""
        rows = []
        for connection in sorted(self.load().values(), key=lambda c: c.name):
            rows.append(
                {
                    "name": connection.name,
                    "account": connection.meta.get("account", ""),
                    "keys": sorted(connection.values),
                    "verified_at": connection.verified_at,
                    "at_rest_protected": self.at_rest_protected,
                }
            )
        return rows
