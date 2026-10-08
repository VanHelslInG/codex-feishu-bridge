"""macOS host adapter: Keychain secrets, process-group control."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .base import Platform


class MacOSPlatform(Platform):
    name = "macos"

    def secret_get(self, service: str, account: str) -> Optional[str]:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def secret_set(self, service: str, account: str, value: str) -> None:
        subprocess.run(
            [
                "/usr/bin/security", "add-generic-password", "-s", service,
                "-a", account, "-w", value, "-U",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def secret_delete(self, service: str, account: str) -> None:
        subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            text=True,
        )

    def popen_kwargs(self) -> Dict[str, Any]:
        return {"start_new_session": True}

    def terminate_tree(self, proc: subprocess.Popen, timeout: float = 10.0) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def fd_count(self, pid: int) -> Optional[int]:
        import re

        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", str(pid), "-Ff"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return sum(1 for line in result.stdout.splitlines() if re.fullmatch(r"f\d+", line))
