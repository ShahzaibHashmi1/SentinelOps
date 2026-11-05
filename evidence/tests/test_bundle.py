import copy
import json
import unittest
from datetime import timedelta

import _bootstrap  # noqa: F401
import fakes
from fakes import GRAPH, RANKING, SIGNALS, T0, TOPOLOGY, at, box, change, logs, svc

from app import bundle as bd
from app.model import BUNDLE_KEYS, BUNDLE_MAX_BYTES, LogEvidence, fmt_ts, parse_ts, validate_bundle

W = SIGNALS.window
OK_SOURCES = {k: ("ok", "") for k in ("detector", "prometheus", "loki", "docker", "topology")}


def incident(first=0, last=100, status="open", resolved_at=None, inc_id="INC-0001", primary="postgres", extra=()):
    sym = [{"rule_id": "R02", "service": "postgres", "role": "root_signal", "first_seen": fmt_ts(at(first)), "last_seen": fmt_ts(at(last))},
           {"rule_id": "R09", "service": "orders", "role": "evidence", "first_seen": fmt_ts(at(first + 5)), "last_seen": fmt_ts(at(last))}]
    sym.extend(extra)
    out = {"incident_id": inc_id, "status": status, "opened_at": fmt_ts(at(first + 10)), "primary_service": primary,
           "candidate_services": ["orders", "postgres"], "symptoms": sym}
    if resolved_at is not None:
        out["resolved_at"] = fmt_ts(at(resolved_at))
    return out


def build(services=None, hint_inc=None, **over):
    s, h = fakes.scenario_f10()
    inc = hint_inc or incident()
    args = dict(
        bundle_id="EVD-00001", incident=inc, trigger="opened", built_at=at(130), build_ms=842,
        window=bd.compute_window(inc, [], at(130), W), source_states=OK_SOURCES, source_names=SIGNALS.sources, services=services or s,
        topology=TOPOLOGY, graph=GRAPH, ranking_cfg=RANKING, window_cfg=W,
    )
    args.update(over)
    return bd.build_bundle(**args)


class WindowTests(unittest.TestCase):
    def test_open_incident_window(self):
        w = bd.compute_window(incident(first=1000), [], at(1200), W)
        self.assertEqual(w.start, at(1000 - 120))
        self.assertEqual(w.end, at(1200))
        self.assertEqual(w.baseline_start, at(1000 - 120 - 300))
        self.assertEqual(w.baseline_end, w.start)
        self.assertTrue(w.baseline_clean)

    def test_resolved_incident_window_ends_60_s_after_last_seen(self):
        w = bd.compute_window(incident(first=1000, last=1100, status="resolved", resolved_at=1165), [], at(5000), W)
        self.assertEqual(w.end, at(1160))

    def test_earliest_symptom_decides_the_start(self):
        extra = [{"rule_id": "R07", "service": "gateway", "role": "effect", "first_seen": fmt_ts(at(950)), "last_seen": fmt_ts(at(1100))}]
        w = bd.compute_window(incident(first=1000, extra=extra), [], at(1200), W)
        self.assertEqual(w.start, at(950 - 120))

    def test_overlapping_incident_makes_the_baseline_unclean(self):
        other = incident(first=500, last=900, status="resolved", resolved_at=965, inc_id="INC-0000")  # ends inside the baseline range
        w = bd.compute_window(incident(first=1000), [other], at(1200), W)
        self.assertFalse(w.baseline_clean)

    def test_open_other_incident_overlaps(self):
        other = incident(first=700, last=1500, inc_id="INC-0000")
        self.assertFalse(bd.compute_window(incident(first=1000), [other], at(1600), W).baseline_clean)

    def test_old_incident_does_not_overlap(self):
        other = incident(first=0, last=100, status="resolved", resolved_at=165, inc_id="INC-0000")
        self.assertTrue(bd.compute_window(incident(first=1000), [other], at(1200), W).baseline_clean)

    def test_the_incident_itself_is_not_its_own_overlap(self):
        me = incident(first=1000)
        self.assertTrue(bd.compute_window(me, [me], at(1200), W).baseline_clean)

    def test_incident_without_symptoms(self):
        with self.assertRaises(bd.BundleError):
            bd.compute_window({"incident_id": "INC-1", "status": "open", "symptoms": []}, [], at(0), W)

    def test_window_never_ends_before_it_starts(self):
        w = bd.compute_window(incident(first=1000), [], at(800), W)
        self.assertEqual(w.end, w.start)


class HintTests(unittest.TestCase):
    def test_compact_hint(self):
        h = bd.make_hint(incident(first=10))
        self.assertEqual(h.primary_service, "postgres")
        self.assertEqual(h.candidate_services, ("orders", "postgres"))
        self.assertEqual([s.rule_id for s in h.symptoms], ["R02", "R09"])
        self.assertEqual(set(h.to_dict()["symptoms"][0]), {"rule_id", "service", "role", "first_seen"})

    def test_bad_symptom(self):
        with self.assertRaises(bd.BundleError):
            bd.make_hint({"symptoms": [{"rule_id": "R02"}]})


class BuildTests(unittest.TestCase):
    def test_top_level_keys_and_order(self):
        b = build()
        self.assertEqual(tuple(b), BUNDLE_KEYS)
        self.assertEqual(validate_bundle(b), [])

    def test_basic_fields(self):
        b = build()
        self.assertEqual(b["bundle_id"], "EVD-00001")
        self.assertEqual(b["schema_version"], 1)
        self.assertEqual(b["incident_id"], "INC-0001")
        self.assertEqual(b["trigger"], "opened")
        self.assertEqual(b["built_at"], fmt_ts(at(130)))
        self.assertEqual(b["build_ms"], 842)
        self.assertEqual(list(b["sources"]), ["detector", "prometheus", "loki", "docker", "topology"])
        self.assertEqual(b["truncated"], [])
        self.assertEqual(b["data_gaps"], [])

    def test_suspects_and_graph(self):
        b = build()
        self.assertEqual(b["suspects"][0]["service"], "postgres")
        self.assertEqual(b["suspects"][0]["rank"], 1)
        self.assertFalse(b["no_single_root"])
        self.assertEqual(b["graph"]["shared_dependencies"]["postgres"], ["inventory", "orders", "payments"])
        self.assertEqual(b["graph"]["depth"]["gateway"], 3)

    def test_services_sorted_and_shaped(self):
        b = build()
        self.assertEqual(list(b["services"]), sorted(b["services"]))
        self.assertEqual(list(b["services"]["orders"]), ["tier", "signals", "logs", "container", "changes"])
        self.assertEqual(b["services"]["postgres"]["tier"], "infra")
        self.assertIsNone(b["services"]["postgres"]["container"])
        self.assertEqual(b["services"]["orders"]["logs"]["window_count"], 200)

    def test_container_section_has_only_the_contract_fields(self):
        s, _ = fakes.scenario_f10()
        s["orders"] = svc("orders", s["orders"].signals, container=box("orders", "running", started_at="2026-11-03T09:00:00Z", restart_count=0,
                                                                         oom_killed=False, health="healthy", memory_limit=268435456, nano_cpus=500000000,
                                                                         image="sentinelops/orders:1", config_hash="abcdef123456"))
        c = build(services=s)["services"]["orders"]["container"]
        self.assertEqual(list(c), ["state", "started_at", "restart_count", "oom_killed", "health", "limits"])
        self.assertEqual(c["limits"], {"memory_bytes": 268435456, "nano_cpus": 500000000})
        self.assertNotIn("image", json.dumps(c))

    def test_changes_in_window_plus_120_s(self):
        inc = incident(first=1000)
        w = bd.compute_window(inc, [], at(1200), W)  # window starts at 880
        s, _ = fakes.scenario_f10()
        s["orders"] = svc("orders", s["orders"].signals, changes=[
            change("orders", "recreated", 700), change("orders", "recreated", 760), change("orders", "config_changed", 900), change("orders", "recreated", 1300)])
        out = build(services=s, hint_inc=inc, window=w, built_at=at(1200))["services"]["orders"]["changes"]
        self.assertEqual([c["ts"] for c in out], [fmt_ts(at(760)), fmt_ts(at(900))])  # 760 = start - 120; 700 too old; 1300 after the end

    def test_signals_sorted_by_name_and_label(self):
        out = build()["services"]["gateway"]["signals"]
        self.assertEqual([s["name"] for s in out], sorted(s["name"] for s in out))
        self.assertEqual(out[0]["name"], "dependency_error_rate:orders")

    def test_data_gaps_from_signals_and_sources(self):
        s, _ = fakes.scenario_f10()
        s["inventory"] = svc("inventory", s["inventory"].signals, gaps=[("latency_p95", "timeout after 3 s")])
        states = dict(OK_SOURCES, docker=("off", "EVIDENCE_DOCKER_SOURCE=off"), loki=("warming_up", "back 10 s ago"))
        b = build(services=s, source_states=states)
        keys = [(g["source"], g["service"], g["signal"]) for g in b["data_gaps"]]
        self.assertIn(("docker", None, None), keys)
        self.assertIn(("loki", None, None), keys)
        self.assertIn(("prometheus", "inventory", "latency_p95"), keys)
        self.assertEqual(b["sources"]["docker"]["state"], "off")

    def test_bad_trigger_and_unknown_service_and_bad_source(self):
        with self.assertRaises(bd.BundleError):
            build(trigger="sometimes")
        s, _ = fakes.scenario_f10()
        s["mystery"] = svc("orders")
        with self.assertRaises(bd.BundleError):
            build(services=s)
        with self.assertRaises(bd.BundleError):
            build(source_states=dict(OK_SOURCES, loki=("great", "")))
        bad = dict(OK_SOURCES)
        del bad["docker"]
        with self.assertRaises(bd.BundleError):
            build(source_states=bad)

    def test_samples_are_redacted_and_marker_lines_never_arrive(self):
        s, _ = fakes.scenario_f10()
        s["orders"] = svc("orders", s["orders"].signals, logs=LogEvidence("ok", 5, 0, (("db_timeout", 4),), ("login password=hunter2 failed", "application_name=chaos-F11 waiting"), ("req-1",)))
        b = build(services=s)
        text = json.dumps(b)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("chaos", text.lower())
        self.assertTrue(any("withheld" in t for t in b["truncated"]))

    def test_bundle_is_plain_json(self):
        b = build()
        self.assertEqual(json.loads(json.dumps(b)), b)


class SizeTests(unittest.TestCase):
    def fat_services(self, lines=40):
        s, _ = fakes.scenario_f10()
        big = tuple(("x" * 300 + str(i)) for i in range(lines))
        for name in list(s):
            sigs = tuple(fakes.sig("dependency_error_rate", label=f"d{i}", anomalous=False) for i in range(60)) + s[name].signals
            s[name] = svc(name, sigs, logs=LogEvidence("ok", 99, 0, tuple((f"event_{i}", 99 - i) for i in range(5)), big, tuple(f"r{i}" for i in range(3))),
                          changes=[change(name, "recreated", 100 + i) for i in range(30)])
        return s

    def test_small_bundle_is_not_truncated(self):
        b = build()
        self.assertEqual(b["truncated"], [])
        self.assertLessEqual(bd.size_of(b), BUNDLE_MAX_BYTES)

    def test_large_bundle_is_cut_and_says_so(self):
        b = build(services=self.fat_services(), max_bytes=20_000)
        self.assertLessEqual(bd.size_of(b), 20_000)
        self.assertIn("services.*.logs.samples", b["truncated"])
        self.assertEqual(validate_bundle(b), [])
        self.assertEqual(b["suspects"][0]["service"], "postgres")

    def test_cuts_are_applied_in_order_until_it_fits(self):
        full = build(services=self.fat_services(), max_bytes=10 ** 9)
        self.assertEqual(full["truncated"], [])
        size = bd.size_of(full)
        # just under the full size: only the first cut (samples) is needed
        b = build(services=self.fat_services(), max_bytes=size - 100)
        self.assertEqual(b["truncated"], ["services.*.logs.samples"])

    def test_anomalous_signals_survive_the_cut(self):
        b = build(services=self.fat_services(), max_bytes=12_000)
        kept = {s["name"] for s in b["services"]["gateway"]["signals"]}
        self.assertIn("dependency_error_rate:orders", kept)

    def test_too_large_after_all_cuts_is_an_error(self):
        with self.assertRaises(bd.BundleError):
            build(services=self.fat_services(), max_bytes=500)

    def test_default_limit_is_64_kib(self):
        self.assertEqual(BUNDLE_MAX_BYTES, 65536)

    def test_size_counts_utf8_bytes(self):
        self.assertGreater(bd.size_of({"a": "é" * 10}), len(json.dumps({"a": "é" * 10}, ensure_ascii=False)))


class ValidateTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_bundle(build()), [])

    def mutate(self, fn):
        b = copy.deepcopy(build())
        fn(b)
        return validate_bundle(b)

    def test_problems_are_found(self):
        cases = {
            "id": lambda b: b.update(bundle_id="X-1"),
            "version": lambda b: b.update(schema_version=2),
            "trigger": lambda b: b.update(trigger="later"),
            "built_at": lambda b: b.update(built_at="yesterday"),
            "build_ms": lambda b: b.update(build_ms=-1),
            "window key": lambda b: b["window"].pop("baseline_clean"),
            "window order": lambda b: b["window"].update(start="2030-01-01T00:00:00.000Z"),
            "source state": lambda b: b["sources"]["loki"].update(state="fine"),
            "service tier": lambda b: b["services"]["orders"].update(tier="edge"),
            "signal keys": lambda b: b["services"]["orders"]["signals"][0].pop("unit"),
            "container keys": lambda b: b["services"]["orders"].update(container={"state": "x"}),
            "suspect rank": lambda b: b["suspects"][0].update(rank=5),
            "suspect score": lambda b: b["suspects"][0].update(score=101),
            "no_single_root": lambda b: b.update(no_single_root="yes"),
            "extra key": lambda b: b.update(extra=1),
            "graph": lambda b: b["graph"].pop("depth"),
            "hint role": lambda b: b["detector_hint"]["symptoms"][0].update(role="boss"),
        }
        for label, fn in cases.items():
            with self.subTest(label):
                self.assertTrue(self.mutate(fn), label)

    def test_not_a_dict(self):
        self.assertTrue(validate_bundle([]))


class TimeTests(unittest.TestCase):
    def test_format_and_parse(self):
        self.assertEqual(fmt_ts(T0), "2026-11-03T10:00:00.000Z")
        self.assertEqual(parse_ts("2026-11-03T10:00:00.123Z").microsecond, 123000)
        self.assertEqual(parse_ts("2026-11-03T12:00:00+02:00"), T0)
        self.assertEqual(parse_ts("2026-11-03T10:00:00"), T0)  # no zone counts as UTC

    def test_round_trip(self):
        t = T0 + timedelta(milliseconds=7)
        self.assertEqual(parse_ts(fmt_ts(t)), t)

    def test_bad_values(self):
        from app.model import ModelError
        for bad in ("", "nonsense", None, 5):
            with self.subTest(bad), self.assertRaises(ModelError):
                parse_ts(bad)


if __name__ == "__main__":
    unittest.main()
