"""Small, bounded client for Codex's stdio account API."""
from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path

from slop import __version__
from slop.config import load


class RpcError(Exception):
    def __init__(self, message: str, *, reauth: bool = False):
        super().__init__(message)
        self.reauth = reauth


def login_required(message: str) -> bool:
    message = message.lower()
    return any(part in message for part in (
        "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated",
        "invalid_grant", "refresh token has expired", "refresh token has already been used",
        "refresh token has been revoked", "refresh token is invalid", "please sign in again",
        "please log in again", "not logged in", "authentication required",
        "unauthorized", "401 unauthorized",
    ))


class CodexRPC:
    def __init__(self, home: Path, timeout: float = 45):
        self.home = home
        self.timeout = timeout
        self.seq = 0
        self.buffer = b""
        self.diagnostics = b""

    def __enter__(self):
        binary = shutil.which(load().launch.bin)
        if not binary:
            raise RpcError("codex is not on PATH")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.home)
        self.proc = subprocess.Popen(
            [binary, "app-server", "--stdio"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.selector.register(self.proc.stderr, selectors.EVENT_READ)
        try:
            self.call("initialize", {"clientInfo": {"name": "slop", "version": __version__}})
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def send(self, message: dict) -> None:
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RpcError("Codex app-server disconnected") from exc

    def call(self, method: str, params: dict | None = None) -> dict:
        self.seq += 1
        request = {"id": self.seq, "method": method}
        if params is not None:
            request["params"] = params
        self.send(request)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpcError("Codex account request timed out; will retry")
            if b"\n" not in self.buffer:
                events = self.selector.select(remaining)
                if not events:
                    raise RpcError("Codex account request timed out; will retry")
                for key, _ in events:
                    chunk = os.read(key.fd, 65536)
                    if key.fileobj is self.proc.stderr:
                        self.diagnostics = (self.diagnostics + chunk)[-32768:]
                        if not chunk:
                            self.selector.unregister(key.fileobj)
                    else:
                        if not chunk:
                            raise RpcError("Codex app-server exited before replying")
                        self.buffer += chunk
                if len(self.buffer) > 4 * 1024 * 1024:
                    raise RpcError("Codex account reply exceeded size limit")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                reply = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(reply, dict) or reply.get("id") != self.seq:
                continue
            if "error" in reply:
                # Never copy arbitrary server error bodies (which may contain secrets) to logs.
                reauth = login_required(json.dumps(reply["error"]))
                raise RpcError(
                    "Sign in again: saved credentials can no longer be renewed" if reauth
                    else f"Codex {method} failed; will retry", reauth=reauth,
                )
            result = reply.get("result")
            if not isinstance(result, dict):
                raise RpcError("Codex returned an invalid account reply")
            return result

    def renewal_error(self) -> RpcError:
        # Some Codex versions return account=null after logging refresh failures.
        # Only recognized permanent failures should trigger reauthorization.
        for key, _ in self.selector.select(0):
            if key.fileobj is self.proc.stderr:
                self.diagnostics = (self.diagnostics + os.read(key.fd, 65536))[-32768:]
        reauth = login_required(self.diagnostics.decode(errors="replace"))
        return RpcError("Sign in again: saved credentials can no longer be renewed" if reauth
                        else "Codex could not renew credentials; will retry", reauth=reauth)

    def __exit__(self, *_):
        self.selector.close()
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.proc.stderr.close()
