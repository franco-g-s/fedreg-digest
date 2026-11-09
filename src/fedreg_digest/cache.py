"""A small SQLite cache of AI results, so re-running a digest costs nothing.

The cache is an optimisation, never a source of truth: delete the file and the
next run gives the same digest, it just calls the model again. Entries are
keyed by model and by a hash of the profile topic, so changing either one
invalidates them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


class Cache:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.conn: sqlite3.Connection | None = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS ai_results ("
                " kind TEXT, key TEXT, scope TEXT, value TEXT,"
                " created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
                " PRIMARY KEY (kind, key, scope))"
            )

    @staticmethod
    def scope(model: str, topic: str) -> str:
        return f"{model}:{hashlib.sha256(topic.encode()).hexdigest()[:12]}"

    def get(self, kind: str, key: str, scope: str) -> dict[str, Any] | None:
        if not self.conn:
            return None
        row = self.conn.execute(
            "SELECT value FROM ai_results WHERE kind=? AND key=? AND scope=?", (kind, key, scope)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind: str, key: str, scope: str, value: dict[str, Any]) -> None:
        if not self.conn:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO ai_results (kind, key, scope, value) VALUES (?, ?, ?, ?)",
            (kind, key, scope, json.dumps(value)),
        )
        self.conn.commit()

    def close(self) -> None:
        if self.conn:
            self.conn.close()
