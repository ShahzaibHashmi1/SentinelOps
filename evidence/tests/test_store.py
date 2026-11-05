import json
import os
import sqlite3
import tempfile
import threading
import unittest

import _bootstrap  # noqa: F401
import fakes
from fakes import at, change

import test_bundle
from app.model import ChangeRecord, fmt_ts
from app.store import MAX_PER_INCIDENT, Store, StoreError, StoreReader, format_bundle_id


def make_bundle(store, incident_id="INC-0001", trigger="refresh", **kw):
    s, _ = fakes.scenario_f10()
    inc = test_bundle.incident(inc_id=incident_id)
    return test_bundle.build(services=s, hint_inc=inc, bundle_id=store.next_bundle_id(), trigger=trigger, **kw)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "evidence.db")
        self.store = Store(self.path)
        self.reader = StoreReader(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def put(self, **kw):
        return self.store.insert_bundle(make_bundle(self.store, **kw))


class BundleStoreTests(StoreCase):
    def test_creates_folder_wal_and_schema_version(self):
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.store.journal_mode().lower(), "wal")
        con = sqlite3.connect(self.path)
        self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "1")
        con.close()

    def test_ids_are_sequential_and_zero_padded(self):
        self.assertEqual(self.store.next_bundle_id(), "EVD-00001")
        self.assertEqual(self.put(trigger="opened"), "EVD-00001")
        self.assertEqual(self.put(), "EVD-00002")
        self.assertEqual(self.store.next_bundle_id(), "EVD-00003")
        self.assertEqual(format_bundle_id(123456), "EVD-123456")

    def test_round_trip_is_exact(self):
        bundle = make_bundle(self.store, trigger="opened")
        self.store.insert_bundle(bundle)
        self.assertEqual(self.reader.get_bundle("EVD-00001"), bundle)
        self.assertEqual(self.reader.latest_bundle("INC-0001"), bundle)

    def test_unknown_ids(self):
        self.assertIsNone(self.reader.get_bundle("EVD-99999"))
        self.assertIsNone(self.reader.latest_bundle("INC-0404"))
        self.assertEqual(self.reader.list_bundles("INC-0404"), [])

    def test_latest_and_list(self):
        self.put(trigger="opened")
        self.put()
        self.put(incident_id="INC-0002", trigger="opened")
        self.put(trigger="resolved")
        listing = self.reader.list_bundles("INC-0001")
        self.assertEqual([(x["bundle_id"], x["trigger"]) for x in listing], [("EVD-00001", "opened"), ("EVD-00002", "refresh"), ("EVD-00004", "resolved")])
        self.assertEqual(self.reader.latest_bundle("INC-0001")["bundle_id"], "EVD-00004")
        self.assertEqual(self.reader.latest_bundle("INC-0002")["bundle_id"], "EVD-00003")

    def test_counts(self):
        self.put(trigger="opened")
        self.put()
        self.put()
        self.assertEqual(self.reader.counts(), {"opened": 1, "refresh": 2, "resolved": 0, "total": 3})

    def test_wrong_id_is_refused(self):
        bundle = make_bundle(self.store)
        bundle["bundle_id"] = "EVD-00042"
        with self.assertRaises(StoreError):
            self.store.insert_bundle(bundle)

    def test_duplicate_insert_is_refused(self):
        bundle = make_bundle(self.store)
        self.store.insert_bundle(bundle)
        with self.assertRaises(StoreError):
            self.store.insert_bundle(bundle)

    def test_invalid_bundle_is_refused(self):
        bundle = make_bundle(self.store)
        bundle["trigger"] = "whenever"
        with self.assertRaises(StoreError):
            self.store.insert_bundle(bundle)
        self.assertEqual(self.reader.counts()["total"], 0)

    def test_bundles_are_immutable(self):
        self.put(trigger="opened")
        con = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            con.execute("UPDATE bundles SET body = '{}' WHERE bundle_id = 'EVD-00001'")
        self.assertIn("immutable", str(ctx.exception))
        con.close()
        self.assertEqual(self.reader.get_bundle("EVD-00001")["bundle_id"], "EVD-00001")

    def test_opened_and_resolved_cannot_be_deleted_but_refresh_can(self):
        self.put(trigger="opened")
        self.put()
        self.put(trigger="resolved")
        con = sqlite3.connect(self.path, isolation_level=None)
        for bid in ("EVD-00001", "EVD-00003"):
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("DELETE FROM bundles WHERE bundle_id = ?", (bid,))
        con.execute("DELETE FROM bundles WHERE bundle_id = 'EVD-00002'")
        con.close()
        self.assertEqual(self.reader.counts()["total"], 2)

    def test_ids_are_never_reused_after_a_delete(self):
        self.put(trigger="opened")
        self.put()
        con = sqlite3.connect(self.path, isolation_level=None)
        con.execute("DELETE FROM bundles WHERE bundle_id = 'EVD-00002'")  # the newest row, a refresh
        con.close()
        self.assertEqual(self.store.next_bundle_id(), "EVD-00003")

    def test_max_30_per_incident_oldest_refresh_goes_first(self):
        self.put(trigger="opened")
        for _ in range(MAX_PER_INCIDENT + 9):
            self.put()
        ids = [x["bundle_id"] for x in self.reader.list_bundles("INC-0001")]
        self.assertEqual(len(ids), MAX_PER_INCIDENT)
        self.assertEqual(ids[0], "EVD-00001")  # the opened bundle stays
        self.assertNotIn("EVD-00002", ids)  # the oldest refresh was deleted
        self.assertEqual(ids[-1], f"EVD-{MAX_PER_INCIDENT + 10:05d}")

    def test_resolved_is_kept_when_the_limit_is_reached(self):
        self.put(trigger="opened")
        for _ in range(MAX_PER_INCIDENT):
            self.put()
        last = self.put(trigger="resolved")
        listing = self.reader.list_bundles("INC-0001")
        self.assertEqual(len(listing), MAX_PER_INCIDENT)
        self.assertEqual(listing[0]["trigger"], "opened")
        self.assertEqual(listing[-1]["bundle_id"], last)
        self.assertEqual(listing[-1]["trigger"], "resolved")

    def test_limit_is_per_incident(self):
        for _ in range(MAX_PER_INCIDENT):
            self.put()
        self.put(incident_id="INC-0002")
        self.assertEqual(len(self.reader.list_bundles("INC-0001")), MAX_PER_INCIDENT)
        self.assertEqual(len(self.reader.list_bundles("INC-0002")), 1)

    def test_persistence_across_reopen(self):
        self.put(trigger="opened")
        self.put()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.next_bundle_id(), "EVD-00003")
        self.assertEqual(self.reader.counts()["total"], 2)

    def test_ids_continue_after_reopen_even_when_the_newest_row_was_deleted(self):
        self.put(trigger="opened")
        self.put()
        self.store.close()
        con = sqlite3.connect(self.path, isolation_level=None)
        con.execute("DELETE FROM bundles WHERE bundle_id = 'EVD-00002'")
        con.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.next_bundle_id(), "EVD-00003")

    def test_body_is_stored_as_compact_utf8_json(self):
        self.put(trigger="opened")
        con = sqlite3.connect(self.path)
        body = con.execute("SELECT body FROM bundles").fetchone()[0]
        con.close()
        self.assertNotIn(", ", body.split('"sources"')[0])
        self.assertEqual(json.loads(body)["bundle_id"], "EVD-00001")


class ReaderTests(StoreCase):
    def test_reader_connect_is_read_only(self):
        con = self.reader._connect()
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
        con.close()

    def test_reader_sees_new_writes(self):
        self.assertEqual(self.reader.counts()["total"], 0)
        self.put(trigger="opened")
        self.assertEqual(self.reader.counts()["total"], 1)

    def test_missing_database(self):
        with self.assertRaises(StoreError):
            StoreReader(os.path.join(self.tmp.name, "none.db")).counts()


class WriterThreadTests(StoreCase):
    def test_a_second_thread_cannot_write(self):
        self.put(trigger="opened")
        errors = []

        def other():
            try:
                self.store.add_changes([ChangeRecord("orders", "recreated", at(0), {}, {})])
            except StoreError as exc:
                errors.append(exc)

        t = threading.Thread(target=other)
        t.start()
        t.join()
        self.assertEqual(len(errors), 1)

    def test_the_owner_thread_can_keep_writing(self):
        self.put(trigger="opened")
        self.put()
        self.assertEqual(self.reader.counts()["total"], 2)

    def test_other_threads_can_read(self):
        self.put(trigger="opened")
        out = []
        t = threading.Thread(target=lambda: out.append(self.reader.counts()["total"]))
        t.start()
        t.join()
        self.assertEqual(out, [1])


class ChangeStoreTests(StoreCase):
    def rec(self, service, kind, seconds, **new):
        return change(service, kind, seconds, **new)

    def test_add_and_list_newest_first(self):
        self.assertEqual(self.store.add_changes([self.rec("orders", "recreated", 10, created="2026-11-03T10:00:10Z"), self.rec("payments", "stopped", 20, status="exited")]), 2)
        out = self.reader.list_changes()
        self.assertEqual([c["service"] for c in out], ["payments", "orders"])
        self.assertEqual(out[1]["new"], {"created": "2026-11-03T10:00:10Z"})
        self.assertEqual(out[0]["ts"], fmt_ts(at(20)))

    def test_filters(self):
        self.store.add_changes([self.rec("orders", "recreated", 10), self.rec("orders", "restarted", 30), self.rec("payments", "stopped", 40)])
        self.assertEqual(len(self.reader.list_changes(service="orders")), 2)
        self.assertEqual(len(self.reader.list_changes(since=fmt_ts(at(25)))), 2)
        self.assertEqual(len(self.reader.list_changes(service="orders", since=fmt_ts(at(25)))), 1)
        self.assertEqual(len(self.reader.list_changes(limit=1)), 1)

    def test_empty_add(self):
        self.assertEqual(self.store.add_changes([]), 0)

    def test_limit_is_clamped(self):
        self.store.add_changes([self.rec("orders", "recreated", 10)])
        self.assertEqual(len(self.reader.list_changes(limit=0)), 1)
        self.assertEqual(len(self.reader.list_changes(limit=10 ** 9)), 1)

    def test_change_fields_outside_the_allow_list_are_refused_by_the_model(self):
        from app.model import ModelError
        with self.assertRaises(ModelError):
            ChangeRecord("orders", "recreated", at(0), {}, {"env": {"PASSWORD": "x"}})
        with self.assertRaises(ModelError):
            ChangeRecord("orders", "exploded", at(0), {}, {})
        with self.assertRaises(ModelError):
            ChangeRecord("orders", "config_changed", at(0), {"config_hash": "a" * 64}, {"config_hash": "b" * 12})
        ChangeRecord("orders", "config_changed", at(0), {"config_hash": "a" * 12}, {"config_hash": "b" * 12})


if __name__ == "__main__":
    unittest.main()
