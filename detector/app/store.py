"""SQLite store (decision D57). No business logic here: it saves and loads what the correlator built.

One writer: the evaluation thread owns the Store object and writes everything of one cycle in one
transaction. The API threads never use it; they open a StoreReader (read-only connection, one per
request). "Single writer" is by design, not by a lock: a reader connection is opened with
mode=ro and cannot write at all.

The database lives in a NAMED volume mounted at /data (no Windows bind mount), journal
mode WAL. Not in the monitored postgres: F10 and F11 break that database and the incidents
describing them must still be written.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .model import (OPEN, SCHEMA_VERSION, DataGap, Incident, Symptom, iso_z, parse_iso)

BUSY_TIMEOUT_MS = 5000
_INCIDENT_ID = re.compile(r"^INC-(\d+)$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    seq                INTEGER PRIMARY KEY,
    status             TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    opened_at          TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    resolved_at        TEXT,
    reopen_count       INTEGER NOT NULL DEFAULT 0,
    severity           TEXT NOT NULL,
    title              TEXT NOT NULL,
    primary_service    TEXT,
    candidate_services TEXT NOT NULL,
    affected_services  TEXT NOT NULL
);
-- At most one open incident, enforced by the database itself.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident ON incidents (status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS incidents_opened_at ON incidents (opened_at);
CREATE TABLE IF NOT EXISTS symptoms (
    seq            INTEGER PRIMARY KEY,
    incident_seq   INTEGER NOT NULL REFERENCES incidents (seq),
    rule_id        TEXT NOT NULL,
    detector       TEXT NOT NULL,
    source         TEXT NOT NULL,
    service        TEXT,
    role           TEXT NOT NULL,
    severity       TEXT NOT NULL,
    opens_incident INTEGER NOT NULL,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    active         INTEGER NOT NULL,
    flaps          INTEGER NOT NULL,
    comparator     TEXT NOT NULL,
    threshold      REAL,
    peak_value     REAL,
    last_value     REAL,
    detail         TEXT NOT NULL,
    dependencies   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS symptoms_incident ON symptoms (incident_seq);
CREATE TABLE IF NOT EXISTS data_gaps (
    incident_seq INTEGER NOT NULL REFERENCES incidents (seq),
    source       TEXT NOT NULL,
    start_ts     TEXT NOT NULL,
    end_ts       TEXT,
    PRIMARY KEY (incident_seq, source, start_ts)
);
"""


class StoreError(Exception):
    pass


@dataclass
class StoreState:
    """What the loop needs after a (re)start."""
    latest: Optional[Incident]       # the newest incident, open or resolved
    next_incident_seq: int
    next_symptom_seq: int


def _symptom_from_row(row) -> Symptom:
    return Symptom(
        seq=row["seq"], rule_id=row["rule_id"], detector=row["detector"], source=row["source"],
        service=row["service"], role=row["role"], severity=row["severity"],
        opens_incident=bool(row["opens_incident"]), first_seen=parse_iso(row["first_seen"]),
        last_seen=parse_iso(row["last_seen"]), active=bool(row["active"]), flaps=row["flaps"],
        comparator=row["comparator"], threshold=row["threshold"], peak_value=row["peak_value"],
        last_value=row["last_value"], detail=row["detail"], dependencies=json.loads(row["dependencies"]))


def _incident_from_rows(conn: sqlite3.Connection, row) -> Incident:
    seq = row["seq"]
    symptoms = [_symptom_from_row(r) for r in
                conn.execute("SELECT * FROM symptoms WHERE incident_seq = ? ORDER BY seq", (seq,))]
    gaps = [DataGap(source=r["source"], start=parse_iso(r["start_ts"]),
                    end=parse_iso(r["end_ts"]) if r["end_ts"] else None)
            for r in conn.execute("SELECT * FROM data_gaps WHERE incident_seq = ? ORDER BY start_ts, source", (seq,))]
    return Incident(
        seq=seq, status=row["status"], opened_at=parse_iso(row["opened_at"]),
        updated_at=parse_iso(row["updated_at"]),
        resolved_at=parse_iso(row["resolved_at"]) if row["resolved_at"] else None,
        reopen_count=row["reopen_count"], severity=row["severity"], title=row["title"],
        primary_service=row["primary_service"], candidate_services=json.loads(row["candidate_services"]),
        affected_services=json.loads(row["affected_services"]), symptoms=symptoms, data_gaps=gaps)


# --------------------------------------------------------------------- writer

class Store:
    """The writer. Created once, used by the evaluation thread only."""

    def __init__(self, path):
        self.path = str(path)
        # check_same_thread=False: the object is created in the main thread and then used by the
        # evaluation thread. Only that one thread writes (single writer by design).
        self._conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise StoreError(f"cannot switch {self.path} to WAL mode (got {mode!r}); "
                                 f"WAL needs a normal file system, not a network share")
            self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._check_schema_version()
        except BaseException:
            self._conn.close()          # do not leave the file locked (Windows) when the store is refused
            raise

    def _check_schema_version(self) -> None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif row["value"] != str(SCHEMA_VERSION):
            raise StoreError(f"{self.path} has schema_version {row['value']}, this detector needs {SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------- write

    def save(self, incidents) -> None:
        """Write the given incidents (with symptoms and data gaps) in ONE transaction."""
        incidents = sorted(incidents, key=lambda i: i.seq)
        if not incidents:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for inc in incidents:
                self._conn.execute(
                    """INSERT INTO incidents (seq, status, opened_at, updated_at, resolved_at, reopen_count,
                                              severity, title, primary_service, candidate_services, affected_services)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (seq) DO UPDATE SET
                           status = excluded.status, updated_at = excluded.updated_at,
                           resolved_at = excluded.resolved_at, reopen_count = excluded.reopen_count,
                           severity = excluded.severity, title = excluded.title,
                           primary_service = excluded.primary_service,
                           candidate_services = excluded.candidate_services,
                           affected_services = excluded.affected_services""",
                    (inc.seq, inc.status, iso_z(inc.opened_at), iso_z(inc.updated_at),
                     iso_z(inc.resolved_at) if inc.resolved_at else None, inc.reopen_count, inc.severity,
                     inc.title, inc.primary_service, json.dumps(inc.candidate_services),
                     json.dumps(inc.affected_services)))
                for s in inc.symptoms:
                    self._conn.execute(
                        """INSERT INTO symptoms (seq, incident_seq, rule_id, detector, source, service, role,
                                                 severity, opens_incident, first_seen, last_seen, active, flaps,
                                                 comparator, threshold, peak_value, last_value, detail, dependencies)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT (seq) DO UPDATE SET
                               last_seen = excluded.last_seen, active = excluded.active, flaps = excluded.flaps,
                               threshold = excluded.threshold, peak_value = excluded.peak_value,
                               last_value = excluded.last_value, detail = excluded.detail,
                               dependencies = excluded.dependencies""",
                        (s.seq, inc.seq, s.rule_id, s.detector, s.source, s.service, s.role, s.severity,
                         int(s.opens_incident), iso_z(s.first_seen), iso_z(s.last_seen), int(s.active), s.flaps,
                         s.comparator, s.threshold, s.peak_value, s.last_value, s.detail,
                         json.dumps(s.dependencies)))
                for g in inc.data_gaps:
                    self._conn.execute(
                        """INSERT INTO data_gaps (incident_seq, source, start_ts, end_ts) VALUES (?, ?, ?, ?)
                           ON CONFLICT (incident_seq, source, start_ts) DO UPDATE SET end_ts = excluded.end_ts""",
                        (inc.seq, g.source, iso_z(g.start), iso_z(g.end) if g.end else None))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # -------------------------------------------------------------- read

    def load_state(self) -> StoreState:
        """For the start of the loop: the newest incident and the next sequence numbers."""
        row = self._conn.execute("SELECT * FROM incidents ORDER BY seq DESC LIMIT 1").fetchone()
        latest = _incident_from_rows(self._conn, row) if row else None
        next_inc = (self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM incidents").fetchone()[0]) + 1
        next_sym = (self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM symptoms").fetchone()[0]) + 1
        return StoreState(latest, next_inc, next_sym)


# --------------------------------------------------------------------- reader

class StoreReader:
    """A read-only connection (uri mode=ro, query_only). One per API request; use with `with`."""

    def __init__(self, path):
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA query_only=ON")

    def __enter__(self) -> "StoreReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def journal_mode(self) -> str:
        return self._conn.execute("PRAGMA journal_mode").fetchone()[0]

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        match = _INCIDENT_ID.match(incident_id or "")
        if not match:
            return None
        row = self._conn.execute("SELECT * FROM incidents WHERE seq = ?", (int(match.group(1)),)).fetchone()
        return _incident_from_rows(self._conn, row) if row else None

    def list_incidents(self, status: Optional[str] = None, limit: int = 50, since: Optional[str] = None) -> list:
        """Newest first. since = an ISO time: only incidents opened at or after it."""
        sql = "SELECT * FROM incidents"
        where, args = [], []
        if status is not None:
            where.append("status = ?")
            args.append(status)
        if since is not None:
            where.append("opened_at >= ?")
            args.append(iso_z(parse_iso(since)))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(int(limit))
        return [_incident_from_rows(self._conn, r) for r in self._conn.execute(sql, args).fetchall()]

    def open_incident_id(self) -> Optional[str]:
        row = self._conn.execute("SELECT seq FROM incidents WHERE status = ?", (OPEN,)).fetchone()
        return f"INC-{row['seq']:04d}" if row else None

    def count_incidents(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
