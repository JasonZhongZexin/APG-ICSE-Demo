"""A dedicated Bolt worker with a client-enforced read deadline."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path


class DatabaseQueryTimeout(TimeoutError):
    pass


class DeadlineBoltClient:
    def __init__(self, uri, user, password, database="default"):
        self.database = database
        self._configuration = dict(uri=uri, user=user, password=password, database=database)
        self._process = None
        self._lock = threading.Lock()

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        root = str(Path(__file__).resolve().parents[2])
        environment = dict(os.environ)
        environment["PYTHONPATH"] = root + os.pathsep + environment.get("PYTHONPATH", "")
        self._process = subprocess.Popen(
            [sys.executable, "-m", "codegraphhunter.web.database_client"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=root, env=environment,
        )
        # Credentials travel through the private pipe, never the process arguments.
        self._process.stdin.write(json.dumps(self._configuration).encode() + b"\n")
        self._process.stdin.flush()

    def _stop(self):
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()

    def run(self, cypher, params=None, timeout_s=30):
        deadline = time.monotonic() + float(timeout_s)
        if not self._lock.acquire(timeout=max(0, float(timeout_s))):
            raise DatabaseQueryTimeout("Timed out waiting for the database connection.")
        try:
            self._start()
            command = dict(cypher=cypher, params=params or {}, timeout_s=float(timeout_s))
            self._process.stdin.write(json.dumps(command).encode() + b"\n")
            self._process.stdin.flush()
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(self._process.stdout, selectors.EVENT_READ)
                while b"\n" not in output:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise DatabaseQueryTimeout(f"Database query exceeded {float(timeout_s):g} seconds.")
                    chunk = os.read(self._process.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("The database worker stopped before returning a result.")
                    output.extend(chunk)
            payload = json.loads(output)
            if not payload.get("ok"):
                if payload.get("timeout"):
                    raise DatabaseQueryTimeout(payload.get("error", "Database query timed out."))
                raise RuntimeError(payload.get("error", "Database query failed."))
            return payload["result"]
        except Exception:
            self._stop()
            raise
        finally:
            self._lock.release()

    def close(self):
        with self._lock:
            self._stop()


def _worker():
    from codegraphhunter.datasource.bolt_client import BoltClient

    try:
        configuration = json.loads(sys.stdin.buffer.readline())
        client = BoltClient(**configuration)
    except Exception as exc:
        sys.stdout.buffer.write(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")
        sys.stdout.buffer.flush()
        return
    try:
        for line in sys.stdin.buffer:
            try:
                command = json.loads(line)
                payload = {"ok": True, "result": client.run(**command)}
            except Exception as exc:
                code = str(getattr(exc, "code", "")).lower()
                payload = {"ok": False, "error": str(exc),
                           "timeout": isinstance(exc, TimeoutError) or "timedout" in code or "timeout" in code}
            sys.stdout.buffer.write(json.dumps(payload, default=str).encode() + b"\n")
            sys.stdout.buffer.flush()
    finally:
        client.close()


if __name__ == "__main__":
    _worker()
