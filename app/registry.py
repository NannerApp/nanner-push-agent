"""Agent-side device registry (SQLite).

The agent owns the per-device notification preferences, they never leave the user's network.
One row per APNs device token: the APNs environment, the app-local server id to echo back in pushes
(for deep-link routing), and the filters (severities, cameras, objects, allow_unlabeled).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class Device:
    device_token: str
    environment: str
    server_id: str
    severities: list[str]
    cameras: Optional[list[str]]
    objects: Optional[list[str]]
    allow_unlabeled: bool

    def wants(self, severity: str, camera: str, objects: list[str]) -> bool:
        if severity not in self.severities:
            return False
        if self.cameras is not None and camera not in self.cameras:
            return False
        # A review with no recognized object (audio/motion-only) is gated solely by allow_unlabeled.
        if not objects:
            return self.allow_unlabeled
        if self.objects is not None and not any(o in self.objects for o in objects):
            return False
        return True


class Registry:
    def __init__(self, path: str = "devices.db"):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        # This service has tiny transactions. WAL plus a bounded busy timeout avoids long event-loop
        # stalls without adding an async SQLite dependency or another worker thread.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        with self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_token    TEXT PRIMARY KEY,
                    environment     TEXT NOT NULL,
                    server_id       TEXT NOT NULL,
                    severities      TEXT NOT NULL,
                    cameras         TEXT,
                    objects         TEXT,
                    allow_unlabeled INTEGER NOT NULL DEFAULT 1,
                    last_seen       REAL NOT NULL
                )
                """
            )

    def upsert(
        self,
        device_token: str,
        environment: str,
        server_id: str,
        severities: list[str],
        cameras: Optional[list[str]],
        objects: Optional[list[str]],
        allow_unlabeled: bool,
    ) -> None:
        with self._lock:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO devices (device_token, environment, server_id, severities, cameras, objects, allow_unlabeled, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_token) DO UPDATE SET
                        environment=excluded.environment, server_id=excluded.server_id,
                        severities=excluded.severities, cameras=excluded.cameras,
                        objects=excluded.objects, allow_unlabeled=excluded.allow_unlabeled,
                        last_seen=excluded.last_seen
                    """,
                    (
                        device_token, environment, server_id,
                        json.dumps(severities),
                        json.dumps(cameras) if cameras is not None else None,
                        json.dumps(objects) if objects is not None else None,
                        1 if allow_unlabeled else 0,
                        time.time(),
                    ),
                )

    def remove(self, device_token: str) -> None:
        with self._lock:
            with self._db:
                self._db.execute("DELETE FROM devices WHERE device_token = ?", (device_token,))

    def all(self) -> list[Device]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM devices").fetchall()
        return [
            Device(
                device_token=r["device_token"],
                environment=r["environment"],
                server_id=r["server_id"],
                severities=json.loads(r["severities"]),
                cameras=json.loads(r["cameras"]) if r["cameras"] is not None else None,
                objects=json.loads(r["objects"]) if r["objects"] is not None else None,
                allow_unlabeled=bool(r["allow_unlabeled"]),
            )
            for r in rows
        ]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]

    def close(self) -> None:
        with self._lock:
            self._db.close()
