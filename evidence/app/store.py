"""SQLite store (D71). WAL, one writer thread, read-only connections for the API.

The store holds no business logic. Bundles are immutable: an UPDATE is refused by a trigger, and a DELETE is
refused for the 'opened' and 'resolved' bundles. At most MAX_PER_INCIDENT bundles are kept per incident:
when a new one would exceed the limit, the oldest 'refresh' bundles of that incident are deleted first.
Bundle ids come from an AUTOINCREMENT sequence and are never reused, also after deletions.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from .model import fmt_ts, parse_ts, validate_bundle

MAX_PER_INCIDENT = 30
SCHEMA_VERSION = 1
_ID = re.compile(r"^EVD-(\d{5,})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bundles (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id TEXT NOT NULL UNIQUE,
    incident_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('opened', 'refresh', 'resolved')),
    built_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bundles_incident ON bundles (incident_id, seq);
CREATE TRIGGER IF NOT EXISTS bundles_no_update BEFORE UPDATE ON bundles
BEGIN SELECT RAISE(ABORT, 'bundles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS bundles_keep_final BEFORE DELETE ON bundles
WHEN OLD.trigger <> 'refresh'
BEGIN SELECT RAISE(ABORT, 'opened and resolved bundles are kept'); END;
CREATE TABLE IF NOT EXISTS changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts TEXT NOT NULL,
    old_json TEXT NOT NULL,
    new_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS changes_service_ts ON changes (service, ts);
"""


class StoreError(RuntimeError):
    """The store refused an operation."""


def format_bundle_id(seq: int) -> str:
    return f"EVD-{seq:05d}"


def _seq_of(bundle_id: str) -> int:
    match = _ID.match(bundle_id or "")
    if not match:
        raise StoreError(f"bad bundle id {bundle_id!r}")
    return int(match.group(1))


class Store:
    """The writer. The first thread that writes owns it; any other thread is refused."""

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA busy_timeout=3000")
        self._con.executescript(SCHEMA)
        self._con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self._owner: int | None = None

    def _own(self) -> None:
        ident = threading.get_ident()
        if self._owner is None:
            self._owner = ident
        elif self._owner != ident:
            raise StoreError("the store has one writer thread; another thread tried to write")

    def close(self) -> None:
        self._con.close()

    def journal_mode(self) -> str:
        return self._con.execute("PRAGMA journal_mode").fetchone()[0]

    # ---- ids
    def next_bundle_id(self) -> str:
        row = self._con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'bundles'").fetchone()
        used = row[0] if row else 0
        top = self._con.execute("SELECT COALESCE(MAX(seq), 0) FROM bundles").fetchone()[0]
        return format_bundle_id(max(used, top) + 1)

    # ---- bundles
    def insert_bundle(self, bundle: dict) -> str:
        """Store one bundle. Its id must be the one next_bundle_id() returned."""
        self._own()
        problems = validate_bundle(bundle)
        if problems:
            raise StoreError("refusing an invalid bundle: " + "; ".join(problems))
        expected = self.next_bundle_id()
        if bundle["bundle_id"] != expected:
            raise StoreError(f"bundle id {bundle['bundle_id']} is not the next id {expected}")
        body = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
        self._con.execute("BEGIN IMMEDIATE")
        try:
            self._make_room(bundle["incident_id"])
            self._con.execute(
                "INSERT INTO bundles (seq, bundle_id, incident_id, trigger, built_at, body) VALUES (?, ?, ?, ?, ?, ?)",
                (_seq_of(expected), expected, bundle["incident_id"], bundle["trigger"], bundle["built_at"], body),
            )
            self._con.execute("COMMIT")
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        return expected

    def _make_room(self, incident_id: str) -> None:
        count = self._con.execute("SELECT COUNT(*) FROM bundles WHERE incident_id = ?", (incident_id,)).fetchone()[0]
        excess = count + 1 - MAX_PER_INCIDENT
        if excess <= 0:
            return
        rows = self._con.execute(
            "SELECT seq FROM bundles WHERE incident_id = ? AND trigger = 'refresh' ORDER BY seq LIMIT ?", (incident_id, excess)
        ).fetchall()
        if len(rows) < excess:
            raise StoreError(f"incident {incident_id} already holds {count} bundles that cannot be deleted")
        self._con.executemany("DELETE FROM bundles WHERE seq = ?", [(r[0],) for r in rows])

    # ---- changes
    def add_changes(self, records) -> int:
        self._own()
        rows = [(r.service, r.kind, fmt_ts(r.ts), json.dumps(r.old, sort_keys=True), json.dumps(r.new, sort_keys=True)) for r in records]
        if not rows:
            return 0
        self._con.execute("BEGIN IMMEDIATE")
        try:
            self._con.executemany("INSERT INTO changes (service, kind, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?)", rows)
            self._con.execute("COMMIT")
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        return len(rows)


class StoreReader:
    """Read-only access for the API and the build loop. A new read-only connection per call."""

    def __init__(self, path: str):
        self.path = str(path)

    def _connect(self) -> sqlite3.Connection:
        if not Path(self.path).exists():
            raise StoreError(f"database not found: {self.path}")
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        return con

    def _query(self, sql: str, args=()):
        con = self._connect()
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def get_bundle(self, bundle_id: str) -> dict | None:
        rows = self._query("SELECT body FROM bundles WHERE bundle_id = ?", (bundle_id,))
        return json.loads(rows[0]["body"]) if rows else None

    def latest_bundle(self, incident_id: str) -> dict | None:
        rows = self._query("SELECT body FROM bundles WHERE incident_id = ? ORDER BY seq DESC LIMIT 1", (incident_id,))
        return json.loads(rows[0]["body"]) if rows else None

    def list_bundles(self, incident_id: str) -> list[dict]:
        rows = self._query("SELECT bundle_id, trigger, built_at FROM bundles WHERE incident_id = ? ORDER BY seq", (incident_id,))
        return [{"bundle_id": r["bundle_id"], "trigger": r["trigger"], "built_at": r["built_at"]} for r in rows]

    def counts(self) -> dict:
        rows = self._query("SELECT trigger, COUNT(*) AS n FROM bundles GROUP BY trigger")
        out = {"opened": 0, "refresh": 0, "resolved": 0}
        for r in rows:
            out[r["trigger"]] = r["n"]
        out["total"] = sum(out.values())
        return out

    def list_changes(self, service: str | None = None, since: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT service, kind, ts, old_json, new_json FROM changes", []
        where = []
        if service:
            where.append("service = ?")
            args.append(service)
        if since:
            where.append("ts >= ?")
            args.append(fmt_ts(parse_ts(since)))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        rows = self._query(sql, args)
        return [{"service": r["service"], "kind": r["kind"], "ts": r["ts"], "old": json.loads(r["old_json"]), "new": json.loads(r["new_json"])} for r in rows]
