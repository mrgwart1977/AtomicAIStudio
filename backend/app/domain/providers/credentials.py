"""C1 credential vault — DPAPI-protected secrets, never in the DB.

Storage: ``<data_dir>/credentials.vault`` — a JSON file of
``{provider_key: {field: <base64 CryptProtectData blob>}}``. Windows
DPAPI (CurrentUser scope) encrypts each secret field independently;
the vault file itself holds only ciphertext, so no plaintext secret
ever lands on disk, in SQLite, in backups (.atomicpkg), in logs, or
in diagnostics. The file is inside the data dir but contains no
recoverable plaintext and is not part of project exports.

Non-Windows platforms: the vault is honest about being unavailable —
writes raise VaultUnavailable so nothing silently degrades to
plaintext storage.

The API layer only ever receives the secret VALUE on set; reads back
``{configured, fields, set_at}`` — never secret material.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import threading
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"


class VaultError(Exception):
    """Beginner-readable vault failure."""


class VaultUnavailable(VaultError):
    """Secure storage isn't available on this platform."""


class SecretUnreadable(VaultError):
    """A stored secret cannot be decrypted (other user/machine state)."""


# ------------------------------------------------------------------
# DPAPI (Windows) — CryptProtectData / CryptUnprotectData, CurrentUser
# ------------------------------------------------------------------
if _IS_WINDOWS:
    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _protect(data: bytes) -> bytes:
        bin_ = _DATA_BLOB(len(data), ctypes.cast(data, ctypes.POINTER(ctypes.c_char)))
        bout = _DATA_BLOB()
        if not _crypt32.CryptProtectData(ctypes.byref(bin_), None, None,
                                         None, None, 0, ctypes.byref(bout)):
            raise VaultError("Windows could not protect the credential "
                             "(CryptProtectData failed)")
        try:
            return ctypes.string_at(bout.pbData, bout.cbData)
        finally:
            _kernel32.LocalFree(bout.pbData)

    def _unprotect(data: bytes) -> bytes:
        bin_ = _DATA_BLOB(len(data), ctypes.cast(data, ctypes.POINTER(ctypes.c_char)))
        bout = _DATA_BLOB()
        if not _crypt32.CryptUnprotectData(ctypes.byref(bin_), None, None,
                                           None, None, 0, ctypes.byref(bout)):
            raise SecretUnreadable(
                "Stored credential can't be decrypted on this account — "
                "it may have been saved under a different Windows user. "
                "Re-enter the credential.")
        try:
            return ctypes.string_at(bout.pbData, bout.cbData)
        finally:
            _kernel32.LocalFree(bout.pbData)


class CredentialVault:
    """Per-provider encrypted field store, file-backed under the data dir."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._state: dict[str, Any] | None = None
        # Serialize mutating ops — sync FastAPI endpoints run on a thread
        # pool; two concurrent set_fields must not race the tmp-file swap.
        self._mu = threading.Lock()

    # ---------- persistence ----------

    def _load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("credentials.vault unreadable — treating as empty")
                self._state = {"version": 1, "providers": {}}
        else:
            self._state = {"version": 1, "providers": {}}
        return self._state

    def _save(self) -> None:
        assert self._state is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- public API ----------

    def available(self) -> bool:
        return _IS_WINDOWS

    def set_fields(self, provider_key: str, fields: dict[str, str]) -> None:
        """Encrypt-and-store secret fields for a provider. Unknown/empty
        values are ignored — never store empty ciphertext."""
        if not _IS_WINDOWS:
            raise VaultUnavailable(
                "Secure credential storage requires Windows DPAPI; "
                "this platform cannot store credentials safely.")
        with self._mu:
            state = self._load()
            prov = state["providers"].setdefault(
                provider_key, {"fields": {}, "set_at": None})
            for name, value in fields.items():
                if value is None or value == "":
                    continue
                prov["fields"][name] = base64.b64encode(
                    _protect(value.encode("utf-8"))).decode("ascii")
            prov["set_at"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def get_fields(self, provider_key: str) -> dict[str, str]:
        """Decrypt all stored fields — backend-internal use only."""
        prov = self._load()["providers"].get(provider_key)
        if not prov:
            return {}
        out = {}
        for name, b64 in prov.get("fields", {}).items():
            try:
                out[name] = _unprotect(base64.b64decode(b64)).decode("utf-8")
            except SecretUnreadable:
                raise
            except Exception as e:
                raise SecretUnreadable(
                    f"Stored credential for {provider_key} is unreadable: {e}")
        return out

    def delete(self, provider_key: str) -> bool:
        """Remove a provider's secrets entirely. True if something was
        removed — secure deletion = ciphertext gone."""
        with self._mu:
            provs = self._load()["providers"]
            if provider_key in provs:
                del provs[provider_key]
                self._save()
                return True
            return False

    def status(self, provider_key: str) -> dict[str, Any]:
        """Metadata only — presence + which fields exist + set_at +
        decryptability probe. NEVER returns secret material."""
        prov = self._load()["providers"].get(provider_key)
        if not prov:
            return {"configured": False, "fields": [], "set_at": None,
                    "readable": False, "unreadable_reason": None}
        fields = list(prov.get("fields", {}).keys())
        readable, why = True, None
        if _IS_WINDOWS and fields:
            # probe decryption of the first field without surfacing it
            first = prov["fields"][fields[0]]
            try:
                _unprotect(base64.b64decode(first))
            except SecretUnreadable as e:
                readable, why = False, str(e)
            except Exception as e:
                readable, why = False, f"vault error: {e}"
        return {"configured": bool(fields), "fields": fields,
                "set_at": prov.get("set_at"), "readable": readable,
                "unreadable_reason": why}
