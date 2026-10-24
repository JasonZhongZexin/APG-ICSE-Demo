"""Temporary, browser-specific ICSE demo settings kept in server memory."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Dict, Tuple


SESSION_COOKIE = "cgh_icse_session"


class IcseDemoSettings:
    def __init__(self, ttl_s: int = 8 * 60 * 60, max_sessions: int = 128):
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._sessions: Dict[str, Tuple[float, Dict[str, str]]] = {}
        self._lock = threading.Lock()

    def resolve(self, token: str = "") -> Tuple[str, Dict[str, str]]:
        now = time.monotonic()
        with self._lock:
            self._sessions = {
                key: item for key, item in self._sessions.items() if item[0] > now
            }
            if token not in self._sessions:
                if len(self._sessions) >= self.max_sessions:
                    oldest = min(self._sessions, key=lambda key: self._sessions[key][0])
                    del self._sessions[oldest]
                token = secrets.token_urlsafe(32)
                self._sessions[token] = (now + self.ttl_s, {})
            _, configuration = self._sessions[token]
            self._sessions[token] = (now + self.ttl_s, configuration)
            return token, dict(configuration)

    def save(self, token: str, configuration: Dict[str, str]) -> None:
        with self._lock:
            self._sessions[token] = (time.monotonic() + self.ttl_s, dict(configuration))
