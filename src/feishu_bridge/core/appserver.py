"""JSON-RPC client for a locally supervised ``codex app-server``.

The bridge owns this process: it starts it, talks to it over stdio and recycles
it when it leaks descriptors or lives too long. It never touches the Codex
desktop client's own app-server.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..platform.base import Platform


class AppServerError(RuntimeError):
    """Raised when the app-server answers with a JSON-RPC error."""


class AppServer:
    def __init__(
        self,
        codex_path: str,
        event_queue: "queue.Queue[Dict[str, Any]]",
        codex_home: Path,
        platform: Platform,
    ) -> None:
        self.codex_path = codex_path
        self.event_queue = event_queue
        self.codex_home = codex_home
        self.platform = platform
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.RLock()
        self.pending: Dict[int, "queue.Queue[Dict[str, Any]]"] = {}
        self.next_id = 100
        self.initialized = False
        self.started_at = 0.0

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self.initialized = False
            command = [self.codex_path, "app-server"]
            env = self.platform.child_env(self.codex_home)
            local_bin = str(Path.home() / ".local" / "bin")
            if local_bin not in env.get("PATH", ""):
                env["PATH"] = os.pathsep.join(
                    part for part in (local_bin, env.get("PATH", "")) if part
                )
            self.proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                **self.platform.popen_kwargs(),
            )
            self.started_at = time.monotonic()
            threading.Thread(target=self._read_stdout, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_feishu_bridge",
                    "title": "Codex Feishu Bridge",
                    "version": "0.1.0",
                },
                "capabilities": {},
            },
            timeout=30,
        )
        self.notify("initialized", {})
        self.initialized = True
        logging.info("codex app-server ready pid=%s", self.proc.pid if self.proc else None)

    def stop(self) -> None:
        with self.lock:
            proc = self.proc
            self.proc = None
            self.initialized = False
            self.started_at = 0.0
            pending = list(self.pending.values())
            self.pending.clear()
        for destination in pending:
            destination.put({"error": {"message": "Codex app-server restarted"}})
        if not proc:
            return
        self.platform.terminate_tree(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    def restart(self) -> None:
        self.stop()
        self.start()

    # --- introspection ---------------------------------------------------
    def busy(self) -> bool:
        with self.lock:
            return bool(self.pending)

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None and self.initialized)

    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc and self.proc.poll() is None else None

    def fd_count(self) -> Optional[int]:
        pid = self.pid()
        if not pid:
            return None
        return self.platform.fd_count(pid)

    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at) if self.started_at else 0.0

    # --- transport -------------------------------------------------------
    def _send(self, message: Dict[str, Any]) -> None:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
                raise AppServerError("codex app-server is not running")
            self.proc.stdin.write(raw + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            destination: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
            self.pending[request_id] = destination
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except AppServerError:
            with self.lock:
                self.pending.pop(request_id, None)
            raise
        try:
            message = destination.get(timeout=timeout)
        except queue.Empty:
            with self.lock:
                self.pending.pop(request_id, None)
            raise TimeoutError(f"{method} timed out after {timeout}s") from None
        if "error" in message:
            error = message["error"] or {}
            raise AppServerError(str(error.get("message") or error))
        return message.get("result") or {}

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, request_id: Any, result: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    # --- readers ---------------------------------------------------------
    def _read_stdout(self) -> None:
        stream = self.proc.stdout if self.proc else None
        if stream is None:
            return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logging.debug("Unparsed app-server output: %s", line[:400])
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self.lock:
                    destination = self.pending.pop(int(message["id"]), None)
                if destination is not None:
                    try:
                        destination.put_nowait(message)
                    except queue.Full:
                        pass
                continue
            self.event_queue.put(message)
        pid = self.proc.pid if self.proc else None
        logging.info("codex app-server stdout closed pid=%s", pid)

    def _read_stderr(self) -> None:
        stream = self.proc.stderr if self.proc else None
        if stream is None:
            return
        for line in stream:
            line = line.rstrip()
            if line:
                logging.debug("app-server: %s", line)
