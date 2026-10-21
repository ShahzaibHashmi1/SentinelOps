import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

import _bootstrap  # noqa: F401
from fakes import T0, Driver
from app.correlator import Correlator
from app.model import OPEN, RESOLVED, RuleMeta
from app.store import Store, StoreError, StoreReader

METAS = [
    RuleMeta("A", "alpha", "high", "root_signal", True, 10),
    RuleMeta("B", "bravo", "medium", "effect", True, 10),
    RuleMeta("D", "dep", "medium", "dependency_effect", True, 10),
    RuleMeta("L", "logs", "low", "evidence", False, 10),
]


def t(seconds):
    return T0 + timedelta(seconds=seconds)


def build_incident():
    """An incident with 3 symptoms, a dependency and one open data gap, made by the real correlator."""
    d = Driver(METAS)
    for _ in range(3):
        d.step(d.obs("A", "postgres", "firing", value=0.0, comparator="==", threshold=0.0),
               d.obs("B", "gateway", "firing", value=9.19, threshold=5.0),
               d.obs("D", "orders", "firing", value=0.5, detail="dependency=postgres (0.5)", threshold=0.1))
    d.step(d.obs("A", "postgres", "firing", value=0.0, comparator="==", threshold=0.0),
           d.obs("L", "orders", "no_data", source="loki"))
    return d


class ParseIsoTests(unittest.TestCase):
    def test_a_time_without_a_zone_is_utc_whatever_the_machine_zone_is(self):
        from datetime import datetime, timezone
        from app.model import parse_iso
        expected = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_iso("2026-10-18T12:00:00"), expected)
        self.assertEqual(parse_iso("2026-10-18T12:00:00Z"), expected)
        self.assertEqual(parse_iso("2026-10-18T17:00:00+05:00"), expected)
        if hasattr(time, "tzset"):                              # POSIX: pretend the machine runs on Pakistan time
            old = os.environ.get("TZ")
            os.environ["TZ"] = "PKT-5"
            time.tzset()
            try:
                self.assertEqual(parse_iso("2026-10-18T12:00:00"), expected)
            finally:
                if old is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = old
                time.tzset()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sentinel store ")   # a space in the path on purpose
        self.path = Path(self.tmp.name) / "incidents.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ schema

    def test_wal_and_schema_version(self):
        with StoreReader(self.path) as r:
            self.assertEqual(r.journal_mode().lower(), "wal")
        conn = sqlite3.connect(self.path)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        conn.close()
        self.assertTrue({"meta", "incidents", "symptoms", "data_gaps"} <= tables)
        self.assertEqual(version, "1")

    def test_reopening_an_existing_database_keeps_the_data(self):
        d = build_incident()
        self.store.save([d.incident])
        self.store.close()
        self.store = Store(self.path)
        with StoreReader(self.path) as r:
            self.assertEqual(r.count_incidents(), 1)

    def test_a_database_with_another_schema_version_is_refused(self):
        self.store.close()
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(StoreError):
            Store(self.path)
        self.store = Store(Path(self.tmp.name) / "other.db")   # so that tearDown has something to close

    # --------------------------------------------------------------- round trip

    def test_incident_round_trip_is_exact(self):
        d = build_incident()
        before = d.incident.to_api()
        self.store.save([d.incident])
        with StoreReader(self.path) as r:
            loaded = r.get_incident("INC-0001")
        self.assertEqual(loaded.to_api(), before)
        self.assertEqual(loaded.symptoms[2].dependencies, ["postgres"])      # internal field survives too
        self.assertEqual(len(loaded.data_gaps), 1)
        self.assertIsNone(loaded.data_gaps[0].end)

    def test_saving_again_updates_in_place(self):
        d = build_incident()
        self.store.save([d.incident])
        d.step(d.obs("A", "postgres", "firing", value=0.0, comparator="==", threshold=0.0),
               d.obs("L", "orders", "ok", source="loki", value=0.0))            # closes the loki gap
        self.store.save([d.incident])
        with StoreReader(self.path) as r:
            loaded = r.get_incident("INC-0001")
            self.assertEqual(r.count_incidents(), 1)
        self.assertEqual(len(loaded.symptoms), 3)
        self.assertIsNotNone(loaded.data_gaps[0].end)
        self.assertEqual(loaded.to_api(), d.incident.to_api())

    def test_save_of_nothing_is_a_no_op(self):
        self.store.save([])
        with StoreReader(self.path) as r:
            self.assertEqual(r.count_incidents(), 0)

    # --------------------------------------------------------------- transactions

    def test_one_cycle_is_one_transaction(self):
        good = build_incident().incident
        bad = build_incident().incident
        bad.seq = 2
        bad.status = RESOLVED
        bad.symptoms[0].rule_id = None            # violates NOT NULL: the whole save must roll back
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save([good, bad])
        with StoreReader(self.path) as r:
            self.assertEqual(r.count_incidents(), 0)
        self.store.save([good])                    # the store is usable after the failure
        with StoreReader(self.path) as r:
            self.assertEqual(r.count_incidents(), 1)

    def test_the_database_allows_only_one_open_incident(self):
        a = build_incident().incident
        b = build_incident().incident
        b.seq = 2
        for i, s in enumerate(b.symptoms):
            s.seq = 10 + i
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save([a, b])
        a.status = RESOLVED
        a.resolved_at = t(100)
        self.store.save([a, b])                    # one resolved + one open is fine
        with StoreReader(self.path) as r:
            self.assertEqual(r.open_incident_id(), "INC-0002")

    # ------------------------------------------------------------------ reading

    def test_list_filters_order_and_limit(self):
        d = Driver(METAS, resolve_after_s=60, reopen_within_s=1)
        incidents = []
        for round_ in range(3):                    # three incidents, the first two resolved
            for _ in range(3):
                d.step(d.obs("A", "orders", "firing", value=90.0))
            incidents.append(d.incident)
            if round_ < 2:
                for _ in range(14):
                    d.step(d.obs("A", "orders", "ok", value=1.0))
                for _ in range(2):
                    d.step(d.obs("A", "orders", "ok", value=1.0))
        self.store.save(incidents)
        with StoreReader(self.path) as r:
            self.assertEqual([i.incident_id for i in r.list_incidents()], ["INC-0003", "INC-0002", "INC-0001"])
            self.assertEqual([i.incident_id for i in r.list_incidents(status="resolved")], ["INC-0002", "INC-0001"])
            self.assertEqual([i.incident_id for i in r.list_incidents(status=OPEN)], ["INC-0003"])
            self.assertEqual([i.incident_id for i in r.list_incidents(limit=1)], ["INC-0003"])
            since = incidents[1].opened_at.isoformat().replace("+00:00", "Z")
            self.assertEqual([i.incident_id for i in r.list_incidents(since=since)], ["INC-0003", "INC-0002"])
            self.assertEqual(r.list_incidents(status="resolved", since=incidents[2].opened_at.isoformat()), [])

    def test_get_incident_unknown_and_malformed_ids(self):
        with StoreReader(self.path) as r:
            self.assertIsNone(r.get_incident("INC-0099"))
            self.assertIsNone(r.get_incident("nonsense"))
            self.assertIsNone(r.get_incident("INC-1; DROP TABLE incidents"))
            self.assertIsNone(r.open_incident_id())

    def test_a_reader_sees_committed_data_while_the_writer_is_open(self):
        with StoreReader(self.path) as r:
            self.assertEqual(r.count_incidents(), 0)
            self.store.save([build_incident().incident])
            self.assertEqual(r.count_incidents(), 1)

    def test_a_reader_cannot_write(self):
        with StoreReader(self.path) as r:
            with self.assertRaises(sqlite3.OperationalError):
                r._conn.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")

    def test_a_second_process_cannot_write_through_a_reader(self):
        code = (
            "import sys, sqlite3\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from app.store import StoreReader\n"
            "r = StoreReader(sys.argv[2])\n"
            "try:\n"
            "    r._conn.execute(\"INSERT INTO meta (key, value) VALUES ('x', 'y')\")\n"
            "except sqlite3.OperationalError as e:\n"
            "    print('refused:', e); sys.exit(0)\n"
            "print('WRITE SUCCEEDED'); sys.exit(1)\n")
        out = subprocess.run([sys.executable, "-c", code, str(_bootstrap.DETECTOR_DIR), str(self.path)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("refused:", out.stdout)

    # ------------------------------------------------------------- restart state

    def test_load_state_of_an_empty_database(self):
        state = self.store.load_state()
        self.assertEqual((state.latest, state.next_incident_seq, state.next_symptom_seq), (None, 1, 1))

    def test_an_open_incident_survives_a_restart_and_is_not_duplicated(self):
        d = build_incident()
        self.store.save([d.incident])
        self.store.close()

        store = Store(self.path)                   # "restart"
        self.store = store
        state = store.load_state()
        self.assertEqual((state.next_incident_seq, state.next_symptom_seq), (2, 4))
        self.assertEqual(state.latest.status, OPEN)
        c = Correlator(METAS, next_incident_seq=state.next_incident_seq, next_symptom_seq=state.next_symptom_seq)
        c.restore(state.latest)
        clock = d.clock
        # first cycles after the restart: the same rules keep firing (the symptoms are already active)
        for _ in range(3):
            observations = [d.obs("A", "postgres", "firing", value=0.0, comparator="==", threshold=0.0),
                            d.obs("B", "gateway", "firing", value=9.5, threshold=5.0)]
            res = c.process(observations, clock.mono, clock.utc)
            clock.advance(5)
            d.clock = clock
        store.save(res.changed)
        with StoreReader(self.path) as r:
            self.assertEqual(r.count_incidents(), 1)
            loaded = r.get_incident("INC-0001")
        self.assertEqual(len(loaded.symptoms), 3)
        self.assertTrue(all(s.flaps == 0 for s in loaded.symptoms))
        self.assertEqual(loaded.symptoms[1].last_value, 9.5)

    def test_the_gap_that_was_open_at_the_restart_is_closed_by_the_first_cycle_with_data(self):
        d = build_incident()                       # loki gap open
        self.store.save([d.incident])
        state = self.store.load_state()
        c = Correlator(METAS, next_incident_seq=state.next_incident_seq, next_symptom_seq=state.next_symptom_seq)
        c.restore(state.latest)
        res = c.process([d.obs("A", "postgres", "firing", value=0.0, comparator="==", threshold=0.0)],
                        d.clock.mono, d.clock.utc)
        self.assertEqual(res.changed[0].data_gaps[0].end, d.clock.utc)

    def test_new_incident_after_restart_gets_the_next_number(self):
        d = Driver(METAS, reopen_within_s=1)
        for _ in range(3):
            d.step(d.obs("A", "orders", "firing", value=90.0))
        for _ in range(16):
            d.step(d.obs("A", "orders", "ok", value=1.0))
        self.assertEqual(d.incident.status, RESOLVED)
        self.store.save([d.incident])
        state = self.store.load_state()
        c = Correlator(METAS, reopen_within_s=1, next_incident_seq=state.next_incident_seq,
                       next_symptom_seq=state.next_symptom_seq)
        c.restore(state.latest)
        res = None
        for _ in range(3):
            res = c.process([d.obs("A", "orders", "firing", value=90.0)], d.clock.mono, d.clock.utc)
            d.clock.advance(5)
        self.assertEqual(res.changed[0].incident_id, "INC-0002")
        self.assertEqual(res.changed[0].symptoms[0].symptom_id, "SYM-00002")
        self.store.save(res.changed)
        with StoreReader(self.path) as r:
            self.assertEqual(r.count_incidents(), 2)


if __name__ == "__main__":
    unittest.main()
