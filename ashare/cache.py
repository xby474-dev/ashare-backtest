"""SQLite content-addressed snapshots. Names are immutable and reads verify hashes."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .data import DataBundle, DataPortal, bundle_payload, bundle_from_payload, canonical_json


class SQLiteCache:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (
                name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL REFERENCES objects(fingerprint),
                created_at TEXT NOT NULL);
        """)

    def save(self, name: str, bundle: DataBundle) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Snapshot name must be nonempty")
        fingerprint = DataPortal(bundle).fingerprint()
        payload = canonical_json(bundle_payload(bundle))
        with self._connection:
            existing = self._connection.execute(
                "SELECT fingerprint FROM snapshots WHERE name=?", (name,)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise ValueError(f"Snapshot {name!r} is immutable; choose a new versioned name")
                return fingerprint
            self._connection.execute("INSERT OR IGNORE INTO objects VALUES (?, ?)",
                                     (fingerprint, payload))
            self._connection.execute("INSERT INTO snapshots VALUES (?, ?, ?)",
                                     (name, fingerprint, datetime.now(timezone.utc).isoformat()))
        return fingerprint

    def load(self, name: str) -> DataBundle:
        row = self._connection.execute(
            "SELECT fingerprint,payload FROM snapshots JOIN objects USING(fingerprint) WHERE name=?",
            (name,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown snapshot: {name}")
        digest, payload = row
        if sha256(payload.encode("utf-8")).hexdigest() != digest:
            raise ValueError("Snapshot content hash mismatch: cache may be corrupted")
        return bundle_from_payload(json.loads(payload))

    def list_snapshots(self) -> list[dict]:
        return [dict(zip(("name", "fingerprint", "created_at"), row)) for row in
                self._connection.execute("SELECT name,fingerprint,created_at FROM snapshots ORDER BY name")]

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
