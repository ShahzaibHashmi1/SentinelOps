"""The evaluation loop writes a changed incident once, retries a failed write, and never writes an incident again
after a successful write (a retry list that is never cleared would rewrite every old incident in every cycle)."""
import io
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from fakes import ALL6, Harness, by, scalar
from app.logging_setup import configure
from app.loop import EvaluationLoop, LoopState
from app.metrics import Metrics
from app.model import RESOLVED
from app.store import Store, StoreReader


class LoopWriteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)                     # runs last: the store is closed first (Windows cannot delete an open file)
        self.db = str(Path(tmp.name) / "d.db")
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.logger = configure("DEBUG", "test", io.StringIO())
        self.addCleanup(lambda: [self.logger.removeHandler(h) for h in list(self.logger.handlers)])
        self.h = Harness()
        clock = self.h.clock
        self.loop = EvaluationLoop([self.h.detector], [self.h.prom, self.h.loki], self.h.correlator, self.store, 5.0,
                                   LoopState(), Metrics(), self.logger, mono=lambda: clock.mono, utc=lambda: clock.utc)
        self.saves = []                                  # one entry per store.save call: [(incident id, status), ...]
        real = self.store.save

        def recording(incidents):
            self.saves.append([(i.incident_id, i.status) for i in incidents])
            return real(incidents)
        self.store.save = recording

    def cycle(self, n=1):
        for _ in range(n):
            self.loop.run_cycle()
            self.h.clock.advance(5)

    def fault_and_recovery(self):
        self.h.set("R02", by("service", {s: 1 for s in ALL6 if s != "postgres"}), guard=scalar(1))
        self.cycle(3)                                    # the incident opens
        self.h.healthy()
        self.cycle(16)                                   # it resolves

    def test_nothing_is_written_while_nothing_changes(self):
        self.cycle(5)
        self.assertEqual(self.saves, [])

    def test_a_resolved_incident_is_not_written_again_after_the_write_that_resolved_it(self):
        self.fault_and_recovery()
        self.assertEqual(self.h.correlator.incident.status, RESOLVED)
        self.assertEqual(self.saves[-1], [("INC-0001", RESOLVED)])         # the write of the resolve
        count = len(self.saves)
        self.cycle(6)                                    # nothing changes any more
        self.assertEqual(len(self.saves), count, self.saves[count:])
        self.assertEqual(self.loop._unsaved, {})

    def test_an_older_incident_is_not_dragged_into_the_writes_of_a_newer_one(self):
        self.fault_and_recovery()
        self.cycle(30)                                   # well past the reopen window (120 s)
        self.h.set("R02", by("service", {s: 1 for s in ALL6 if s != "redis"}), guard=scalar(1))
        self.cycle(40)                                   # a second incident opens (new number)
        self.assertEqual(self.h.correlator.incident.incident_id, "INC-0002")
        later = self.saves[3:]
        self.assertTrue(later)
        for entry in [e for e in later if ("INC-0002", "open") in e]:
            self.assertNotIn(("INC-0001", RESOLVED), entry)

    def test_a_failed_write_of_the_resolve_is_retried_although_nothing_changes_any_more(self):
        state = {"failed": 0}

        def fail_once_on_resolve(incidents):
            self.saves.append([(i.incident_id, i.status) for i in incidents])
            if state["failed"] == 0 and any(i.status == RESOLVED for i in incidents):
                state["failed"] += 1
                raise OSError("database is locked")
            return Store.save(self.store, incidents)
        self.store.save = fail_once_on_resolve
        self.h.set("R02", by("service", {s: 1 for s in ALL6 if s != "postgres"}), guard=scalar(1))
        self.cycle(3)                                    # the incident opens and is stored
        self.h.healthy()
        for _ in range(40):                              # stop in the very cycle that resolves it
            self.cycle(1)
            if self.h.correlator.incident.status == RESOLVED:
                break
        self.assertEqual(self.h.correlator.incident.status, RESOLVED)
        self.assertEqual(state["failed"], 1)             # the write of the resolve failed
        with StoreReader(self.db) as reader:
            self.assertEqual(reader.list_incidents()[0].status, "open")          # the resolve is not stored yet
        self.cycle(1)                                    # nothing changed in this cycle: only the retry writes it
        with StoreReader(self.db) as reader:
            self.assertEqual(reader.list_incidents()[0].status, RESOLVED)
        count = len(self.saves)
        self.cycle(4)
        self.assertEqual(len(self.saves), count)         # and then it is not written again

    def test_a_failed_write_is_kept_and_written_once_when_it_works_again(self):
        state = {"fail": True}

        def flaky(incidents):
            self.saves.append([(i.incident_id, i.status) for i in incidents])
            if state["fail"]:
                raise OSError("database is locked")
            return Store.save(self.store, incidents)
        self.store.save = flaky
        self.h.set("R02", by("service", {s: 1 for s in ALL6 if s != "postgres"}), guard=scalar(1))
        self.cycle(3)                                    # the incident opens; every write fails
        self.assertTrue(self.saves and all(entry[0][0] == "INC-0001" for entry in self.saves))
        failures = len(self.saves)
        state["fail"] = False
        self.cycle(1)                                    # the next cycle writes it
        self.assertEqual(len(self.saves), failures + 1)
        with StoreReader(self.db) as reader:
            self.assertEqual([i.incident_id for i in reader.list_incidents()], ["INC-0001"])
        self.assertEqual(self.loop._unsaved, {})


if __name__ == "__main__":
    unittest.main()
