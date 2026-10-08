"""Windows host adapter: DPAPI-protected secrets, job-object process control."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, Optional

from .base import Platform


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


class WindowsPlatform(Platform):
    name = "windows"

    def __init__(self) -> None:
        self._vault = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexFeishuBridge"

    # --- secret storage (DPAPI, per Windows user) ------------------------
    def _vault_path(self) -> Path:
        self._vault.mkdir(parents=True, exist_ok=True)
        return self._vault / "secrets.dat"

    def _read_vault(self) -> Dict[str, str]:
        path = self._vault_path()
        if not path.exists():
            return {}
        crypt32 = ctypes.windll.crypt32
        blob_in = _blob(path.read_bytes())
        blob_out = _DataBlob()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise OSError("CryptUnprotectData failed for the bridge secret vault")
        try:
            plain = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return json.loads(plain.decode("utf-8"))

    def _write_vault(self, values: Dict[str, str]) -> None:
        path = self._vault_path()
        plain = json.dumps(values, ensure_ascii=False).encode("utf-8")
        crypt32 = ctypes.windll.crypt32
        blob_in = _blob(plain)
        blob_out = _DataBlob()
        if not crypt32.CryptProtectData(
            ctypes.byref(blob_in), "codex-feishu-bridge", None, None, None, 0,
            ctypes.byref(blob_out),
        ):
            raise OSError("CryptProtectData failed for the bridge secret vault")
        try:
            protected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        path.write_bytes(protected)

    @staticmethod
    def _key(service: str, account: str) -> str:
        return f"{service}/{account}"

    def secret_get(self, service: str, account: str) -> Optional[str]:
        return self._read_vault().get(self._key(service, account))

    def secret_set(self, service: str, account: str, value: str) -> None:
        values = self._read_vault()
        values[self._key(service, account)] = value
        self._write_vault(values)

    def secret_delete(self, service: str, account: str) -> None:
        values = self._read_vault()
        values.pop(self._key(service, account), None)
        self._write_vault(values)

    # --- process control -------------------------------------------------
    def popen_kwargs(self) -> Dict[str, Any]:
        # CREATE_NEW_PROCESS_GROUP lets us deliver CTRL_BREAK/terminate to the
        # whole tree; CREATE_NO_WINDOW keeps the daemon headless.
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        }

    def terminate_tree(self, proc: subprocess.Popen, timeout: float = 10.0) -> None:
        if proc.poll() is not None:
            return
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass

    # --- environment -----------------------------------------------------
    def resolve_codex_path(self, configured: Optional[str]) -> str:
        if configured:
            candidate = Path(str(configured)).expanduser()
            if candidate.exists():
                return str(candidate)
            return str(configured)
        found = _which("codex.exe") or _which("codex")
        return found or "codex.exe"

    def fd_count(self, pid: int) -> Optional[int]:
        # Windows has no lsof equivalent in the base image; the age-based
        # watchdog still applies.
        return None


def _which(name: str) -> Optional[str]:
    from shutil import which

    return which(name)
