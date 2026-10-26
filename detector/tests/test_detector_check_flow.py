"""The whole scripts/detector_check.py (main) against a simulated world: fake clock, fake detector, Prometheus, Loki,
chaos.py and docker compose. No Docker, no network, no real waiting. The detector model here is deliberately simple
(it opens an incident some seconds after a fault starts, resolves it after a quiet time, freezes while a source is
down, reopens within the reopen window): it tests what the SCRIPT does with such a detector, not the detector."""
import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _bootstrap  # noqa: F401
from _bootstrap import RULES_JSON, REPO_DIR
from app.rules import load_rules

if "detector_check" in sys.modules:
    dc = sys.modules["detector_check"]
else:
    spec = importlib.util.spec_from_file_location("detector_check", REPO_DIR / "scripts" / "detector_check.py")
    dc = importlib.util.module_from_spec(spec)
    sys.modules["detector_check"] = dc
    spec.loader.exec_module(dc)

T0 = datetime(2026, 10, 21, 1, 0, 0, tzinfo=timezone.utc)
ALL_SERVICES = dc.M1_SERVICES + dc.OBSERVABILITY_SERVICES + [dc.DETECTOR_SERVICE]

EFFECTS = {   # fault -> [(rule id, service, seconds after the effect started when the symptom is there)]
    "F10": [("R02", "postgres", 10), ("R05", "gateway", 28), ("R06", "orders", 14)],
    "F13": [("R02", "redis", 10), ("R06", "inventory", 12), ("R07", "gateway", 30)],
    "F06": [("R01", "inventory", 10), ("R09", "inventory", 12), ("R05", "gateway", 30)],
}
EFFECTS.update({
    "F01": [("R03", "orders", 45)],
    "F02": [("R04", "payments", 35)],
    "F08": [("R06", "orders", 8), ("R05", "gateway", 16)],
    "F11": [("R07", "inventory", 18), ("R05", "gateway", 20)],
    # F18: the p95 of the gateway is there at once (R07 opens the incident), the request rate passes its threshold after about 50 s
    "F18": [("R07", "gateway", 3), ("R08", "gateway", 50), ("R03", "gateway", 52)],
})
PRIMARY = {"F10": "postgres", "F13": "redis", "F06": "inventory", "F01": "orders", "F02": "payments", "F08": None, "F11": None, "F18": None}
TARGET = {"F10": "postgres", "F13": "redis", "F06": "inventory", "F01": "orders", "F02": "payments", "F08": "orders", "F11": "postgres",
          "F18": "gateway"}
ALL_FAULTS = ["F10", "F13", "F06", "F01", "F02", "F08", "F11", "F18"]
FAST = ["--skip-nodata", "--skip-persistence"]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class World:
    def __init__(self, **knobs):
        self.k = dict(effects=EFFECTS, primary=PRIMARY, resolve_s=65.0, reopen_within=120, warmup=30, loki_503_s=0.0,
                      spurious=None, spurious_no_data=None, start_open_incident_for=None, never_resolve_start_incident=False,
                      prometheus_warming_at_start=0.0, incident_on_nodata=False, health_fails=False, skew_s=0,
                      inject_takes_s=2.0, hold_extra_s=0.0, spurious_incident_at=None, nodata_delay_s=0.0,
                      restart_loses_incident=False, restart_noop=False, restart_increments_count=False, restart_extra_count=0,
                      candidates_override=None, restart_takes_s=3.0, restart_fails=False, restart_deactivates_symptoms=False)
        self.k.update(knobs)
        self.t = 0.0
        self.rules = [r.to_dict() for r in load_rules(RULES_JSON)]
        self.series = [(r["id"], s) for r in self.rules for s in r["expected_subjects"]]
        self.history = []                      # (t, {series: (state, value or None)})
        self.incidents = []                    # API dicts, oldest first
        self.down = {"prometheus": False, "loki": False}
        self.ready_at = {"prometheus": -1000.0, "loki": -1000.0}
        self.ready_prev = {"prometheus": True, "loki": True}
        self.warm_until = {"prometheus": -1.0, "loki": -1.0}
        self.down_since = {}
        self.state = {"prometheus": "ok", "loki": "ok"}
        if self.k["prometheus_warming_at_start"]:
            self.warm_until["prometheus"] = self.k["prometheus_warming_at_start"]
            self.state["prometheus"] = "warming_up"
        self.since = {"prometheus": T0, "loki": T0}
        self.fault = None
        self.last_resolved_t = None
        self.load_running = False
        self.compose_calls = []                # (profiles, args)
        self.chaos_calls = []
        self.load_starts = 0
        self.removed_load = 0
        self.health_log = []
        self.detector_up_at = 0.0
        self.started_counter = 0
        self.restart_count = 0
        self.manual_runs = {}
        self.tmp = tempfile.TemporaryDirectory()
        self.truth = Path(self.tmp.name) / "ground_truth.jsonl"
        self.run_counter = 0
        self.spawned = []
        if self.k["start_open_incident_for"] is not None:
            self._make_incident("F10", T0 - timedelta(seconds=300), start_open=True)
            self.start_open_until = self.k["start_open_incident_for"]
        else:
            self.start_open_until = None

    # ---------------------------------------------------------------- clock
    def utc(self):
        return T0 + timedelta(seconds=self.t)

    def monotonic(self):
        return 1000.0 + self.t

    def sleep(self, seconds):
        whole = int(seconds)
        for _ in range(whole):
            self._tick()
        frac = seconds - whole
        if frac > 0:
            self.t += frac

    # ---------------------------------------------------------------- detector model
    def _alive(self):
        return self.t >= self.detector_up_at

    def _ready(self, src):
        return self._alive() and not self.down[src] and self.t >= self.ready_at[src]

    def _tick(self):
        self.t += 1
        for src in ("prometheus", "loki"):
            ready = self._ready(src)
            if not ready and not self._alive():
                new = "no_data"                       # a detector process that is not running reports nothing
            elif not ready:
                late = src in self.down_since and self.t - self.down_since[src] < self.k["nodata_delay_s"]
                new = self.state[src] if late else "no_data"
            else:
                if not self.ready_prev[src]:
                    self.warm_until[src] = self.t + self.k["warmup"]
                new = "warming_up" if self.t < self.warm_until[src] else "ok"
            self.ready_prev[src] = ready
            if new != self.state[src]:
                self.state[src], self.since[src] = new, self.utc()
        if self.k["spurious_incident_at"] is not None and self.t == self.k["spurious_incident_at"]:
            self._make_incident("F10", self.utc())
        if all(s == "ok" for s in self.state.values()):
            self._incident_logic()
        elif self.k["incident_on_nodata"] and self.down["prometheus"] and not any(i["status"] == "open" for i in self.incidents) \
                and self.t % 7 == 0:
            self._make_incident("F10", self.utc())                     # a buggy detector
        self.history.append((self.t, {key: (self._state_of(*key), self._value_of(*key)) for key in self.series}))

    def _make_incident(self, fid, opened, start_open=False):
        number = len(self.incidents) + 1
        incident = {"incident_id": f"INC-{number:04d}", "schema_version": 1, "status": "open", "opened_at": iso(opened),
                    "updated_at": iso(opened), "resolved_at": None, "reopen_count": 0, "severity": "high",
                    "title": f"{fid} test incident", "primary_service": self.k["primary"].get(fid),
                    "candidate_services": [], "affected_services": [], "symptoms": [], "data_gaps": []}
        self.incidents.append(incident)
        return incident

    def _symptom(self, rule, service, first_seen):
        return {"symptom_id": "SYM-00001", "rule_id": rule, "detector": "threshold", "source": "prometheus", "service": service,
                "role": "root_signal", "severity": "high", "opens_incident": True, "first_seen": iso(first_seen),
                "last_seen": iso(first_seen), "active": True, "flaps": 0, "comparator": ">", "threshold": 1.0,
                "peak_value": 2.0, "last_value": 2.0, "detail": ""}

    def _open_incident(self):
        return next((i for i in self.incidents if i["status"] == "open"), None)

    def _incident_logic(self):
        now = self.utc()
        if self.start_open_until is not None:                           # an incident left by an earlier run
            if self.t >= self.start_open_until and not self.k["never_resolve_start_incident"]:
                self.incidents[0].update(status="resolved", resolved_at=iso(now))
                self.last_resolved_t = self.t
                self.start_open_until = None
            return
        f = self.fault
        if f is None:
            return
        if not f["cleared"]:
            due = [(r, s, off) for r, s, off in f["spec"] if self.t >= f["effect_start"] + off]
            if not due:
                return
            incident = self._open_incident()
            if incident is None:
                last = self.incidents[-1] if self.incidents else None
                if last is not None and last["status"] == "resolved" and self.t - self.last_resolved_t <= self.k["reopen_within"]:
                    last.update(status="open", resolved_at=None, reopen_count=last["reopen_count"] + 1)
                    incident = last
                else:
                    incident = self._make_incident(f["fid"], now)
                f["quiet_since"] = None
            for rule, service, off in due:
                if not any(s["rule_id"] == rule and s["service"] == service and s["active"] for s in incident["symptoms"]):
                    incident["symptoms"].append(self._symptom(rule, service, T0 + timedelta(seconds=f["effect_start"] + off)))
            incident["candidate_services"] = (self.k["candidates_override"] if self.k["candidates_override"] is not None
                                              else sorted({s["service"] for s in incident["symptoms"]}))
        else:
            incident = self._open_incident()
            if incident is None:
                return
            if self.t >= f["cleared_t"] + 5:
                for s in incident["symptoms"]:
                    s["active"] = False
                f.setdefault("quiet_since", None)
                if f["quiet_since"] is None:
                    f["quiet_since"] = self.t
                elif self.t - f["quiet_since"] >= self.k["resolve_s"]:
                    incident.update(status="resolved", resolved_at=iso(now))
                    self.last_resolved_t = self.t

    def _active_pairs(self):
        f = self.fault
        if f is None or f["cleared"]:
            return set()
        return {(r, s) for r, s, off in f["spec"] if self.t >= f["effect_start"] + off}

    def _source_of(self, rule_id):
        return next(r["source"] for r in self.rules if r["id"] == rule_id)

    def _state_of(self, rule_id, subject):
        if self.state[self._source_of(rule_id)] != "ok":
            return "no_data"
        sp = self.k["spurious"]
        if sp and sp[0] == rule_id and sp[1] == subject and sp[2] <= self.t < sp[2] + 12:
            return "firing"
        nd = self.k["spurious_no_data"]
        if nd and nd[0] == rule_id and nd[1] == subject and nd[2] <= self.t < nd[2] + 6:
            return "no_data"
        return "firing" if (rule_id, subject) in self._active_pairs() else "ok"

    def _value_of(self, rule_id, subject):
        state = self._state_of(rule_id, subject)
        if state == "no_data":
            return None
        return 99.0 if state == "firing" else (20.0 if self.load_running else 10.0)

    # ---------------------------------------------------------------- fake HTTP
    def http_get(self, url, timeout=10):
        if url.startswith(dc.PROMETHEUS + "/-/ready"):
            return (0, "connection refused", {}) if self.down["prometheus"] else (200, "ready", {})
        if url.startswith(dc.LOKI + "/ready"):
            if self.down["loki"]:
                return 0, "connection refused", {}
            return (200, "ready", {}) if self.t >= self.ready_at["loki"] else (503, "Ingester not ready", {})
        if url.startswith(dc.PROMETHEUS + "/api/v1/query"):
            return self._prometheus(url)
        if url.startswith(dc.DETECTOR) and not self._alive():
            return 0, "connection refused", {}
        if url.startswith(dc.DETECTOR + "/health"):
            fail = self.k["health_fails"] and self.fault is not None and not self.fault["cleared"]
            self.health_log.append(503 if fail else 200)
            skew = timedelta(seconds=self.k["skew_s"])
            stamp = (self.utc() + skew).strftime("%a, %d %b %Y %H:%M:%S GMT")
            return (503, "x", {}) if fail else (200, '{"status":"ok"}', {"Date": stamp})
        if url.startswith(dc.DETECTOR + "/ready"):
            return 200, '{"status":"ready"}', {}
        if url.startswith(dc.DETECTOR + "/internal/v1/status"):
            return 200, json.dumps(self._status()), {}
        if url.startswith(dc.DETECTOR + "/internal/v1/rules"):
            return 200, json.dumps({"rules": self.rules}), {}
        if url.startswith(dc.DETECTOR + "/internal/v1/incidents"):
            return 200, json.dumps({"incidents": [json.loads(json.dumps(i)) for i in reversed(self.incidents)]}), {}
        return 404, "not found", {}

    def _status(self):
        rules = []
        for rule in self.rules:
            subjects = [{"subject": s, "state": self._state_of(rule["id"], s), "value": self._value_of(rule["id"], s), "detail": ""}
                        for s in rule["expected_subjects"]]
            states = {x["state"] for x in subjects}
            state = "firing" if "firing" in states else ("no_data" if "no_data" in states else "ok")
            rules.append({"rule_id": rule["id"], "name": rule["name"], "source": rule["source"], "state": state, "subjects": subjects})
        openi = self._open_incident()
        return {"service": "detector", "version": "1.0.0",
                "sources": {n: {"state": self.state[n], "since": iso(self.since[n]), "detail": ""} for n in self.state},
                "rules_loaded": len(self.rules), "open_incident_id": openi["incident_id"] if openi else None,
                "rules": rules, "settings": {"reopen_within_s": self.k["reopen_within"], "warmup_s": self.k["warmup"]}}

    def _prometheus(self, url):
        query = re.search(r"query=([^&]+)", url).group(1)
        from urllib.parse import unquote
        query = unquote(query).replace("+", " ")
        if query == 'up{job="detector"}':
            return 200, self._vector([({"job": "detector"}, 1)]), {}
        match = re.match(r"(max|min)_over_time\((detector_rule_state|detector_rule_value)\[(\d+)s\]\)", query)
        if not match:
            return 200, self._vector([]), {}
        fn, metric, window = match.group(1), match.group(2), int(match.group(3))
        rows = [(t, snap) for t, snap in self.history if self.t - window < t <= self.t]
        out = []
        for key in self.series:
            values = []
            for _, snap in rows:
                state, value = snap[key]
                if metric == "detector_rule_state":
                    values.append({"firing": 1, "ok": 0, "no_data": -1}[state])
                elif value is not None:
                    values.append(value)
            if values:
                out.append(({"rule_id": key[0], "subject": key[1]}, (max if fn == "max" else min)(values)))
        return 200, self._vector(out), {}

    @staticmethod
    def _vector(items):
        return json.dumps({"status": "success", "data": {"resultType": "vector",
                "result": [{"metric": m, "value": [1760000000, str(v)]} for m, v in items]}})

    # ---------------------------------------------------------------- fake processes
    def compose(self, profiles, *args, timeout=None, env=None):
        self.compose_calls.append((list(profiles), list(args)))
        verb = args[0]
        if verb == "stop":
            self.down[args[1]] = True
            self.down_since[args[1]] = self.t
            return dc.Result(0, "", "")
        if verb == "up" and args[-1] == "detector":
            self.detector_up_at = min(self.detector_up_at, self.t)
            self.up_detector_calls = getattr(self, "up_detector_calls", 0) + 1
            return dc.Result(0, "", "")
        if verb == "up":
            service = args[-1]
            self.down[service] = False
            self.down_since.pop(service, None)
            self.ready_at[service] = self.t + (self.k["loki_503_s"] if service == "loki" else 0.0)
            return dc.Result(0, "", "")
        if verb == "restart":
            if self.k["restart_fails"]:
                return dc.Result(1, "", "Error response from daemon")
            if not self.k["restart_noop"]:
                self.sleep(self.k["restart_takes_s"])
                self.detector_up_at = self.t
                self.started_counter += 1
                self.restart_count += (1 if self.k["restart_increments_count"] else 0) + self.k["restart_extra_count"]
                for src in ("prometheus", "loki"):
                    self.ready_prev[src] = False
                    self.state[src] = "no_data"
                if self.k["restart_deactivates_symptoms"]:  # a buggy detector that restores the incident but not its active symptoms
                    for incident in self.incidents:
                        for sym in incident["symptoms"]:
                            sym["active"] = False
                    self.fault["effect_start"] = 10 ** 9      # and never sees the fault again
                if self.k["restart_loses_incident"]:      # a buggy detector that forgets its incident
                    for incident in self.incidents:
                        if incident["status"] == "open":
                            incident.update(status="resolved", resolved_at=iso(self.utc()))
                    self.last_resolved_t = -10000.0
            return dc.Result(0, "", "")
        if verb == "ps" and "--format" in args:
            return dc.Result(0, json.dumps(self.ps_rows()), "")
        return dc.Result(0, "abc123\n", "")

    @staticmethod
    def ps_rows():
        return [{"Service": s, "State": "running", "Health": "" if s in ("loki", "alloy") else "healthy"} for s in ALL_SERVICES]

    def chaos(self, *args):
        self.chaos_calls.append(args)
        if args[:1] == ("status",):
            return dc.Result(0, "no active faults\n", "")
        if args[:1] == ("inject",):
            fid = args[1]
            self.run_counter += 1
            run_id = f"{fid}-{self.run_counter:08x}"
            inject_ts = self.utc()
            self.sleep(self.k["inject_takes_s"])
            self.fault = {"fid": fid, "spec": self.k["effects"][fid], "effect_start": self.t - self.k["inject_takes_s"] + 1,
                          "cleared": False, "cleared_t": None}
            self.manual_runs[fid] = (run_id, inject_ts)
            return dc.Result(0, f"injected {fid} on {TARGET[fid]} (run id {run_id})\n", "")
        if len(args) == 2 and args[0] == "clear" and args[1] != "--all":
            fid = args[1]
            if fid not in self.manual_runs:
                return dc.Result(1, "", f"{fid} is not active")
            run_id, inject_ts = self.manual_runs.pop(fid)
            clear_ts = self.utc()
            self.fault["cleared"], self.fault["cleared_t"] = True, self.t
            self.sleep(8)
            base = {"run_id": run_id, "fault_id": fid, "category": "x", "target": TARGET[fid], "params": {},
                    "expected_root_cause": {"service": TARGET[fid], "type": "x"}, "expected_action": "restart_service",
                    "acceptable_actions": ["restart_service"]}
            with open(self.truth, "ab") as handle:
                for kind, ts, extra in (("inject", inject_ts, {}), ("clear", clear_ts, {}), ("recovered", self.utc(), {"recovery_s": 8.0})):
                    handle.write((json.dumps({**base, "event": kind, "ts": iso(ts), **extra}) + "\n").encode("utf-8"))
            return dc.Result(0, f"cleared {fid} (run id {run_id})\nrecovered 8.0 s after the clear\n", "")
        if args[:2] == ("clear", "--all"):
            if self.fault and not self.fault["cleared"]:
                self.fault["cleared"], self.fault["cleared_t"] = True, self.t
            return dc.Result(0, "no active faults; the stack is healthy\n", "")
        return dc.Result(0, "", "")

    def stream(self, command, on_line, poll, poll_every_s, timeout_s, env=None, started=None):
        fid = command[command.index("run") + 1]
        hold = int(command[command.index("--hold") + 1])
        self.spawned.append(command)
        child = SimpleNamespace(poll=lambda: None, terminate=lambda: None)
        if started is not None:
            started.append(child)
        self.run_counter += 1
        run_id = f"{fid}-{self.run_counter:08x}"
        inject_ts = self.utc()
        lines = [f"run {fid}: warm-up 0 s, hold {hold} s, cool-down 0 s"]
        self.sleep(self.k["inject_takes_s"])
        self.fault = {"fid": fid, "spec": self.k["effects"][fid], "effect_start": self.t - self.k["inject_takes_s"] + 1,
                      "cleared": False, "cleared_t": None}
        text = f"injected {fid} on {TARGET[fid]} (run id {run_id})"
        lines.append(text)
        on_line(self.utc(), text)
        end = self.t + hold + self.k["hold_extra_s"]
        while self.t < end:
            self.sleep(min(poll_every_s, end - self.t))
            poll()
        clear_ts = self.utc()
        self.fault["cleared"], self.fault["cleared_t"] = True, self.t
        self.sleep(8)
        recovered_ts = self.utc()
        base = {"run_id": run_id, "fault_id": fid, "category": "x", "target": TARGET[fid], "params": {},
                "expected_root_cause": {"service": TARGET[fid], "type": "x"}, "expected_action": "restart_service",
                "acceptable_actions": ["restart_service"]}
        with open(self.truth, "ab") as handle:
            for kind, ts, extra in (("inject", inject_ts, {}), ("clear", clear_ts, {}), ("recovered", recovered_ts, {"recovery_s": 8.0})):
                handle.write((json.dumps({**base, "event": kind, "ts": iso(ts), **extra}) + "\n").encode("utf-8"))
        lines.append("recovered 8.0 s after the clear")
        return 0, lines

    def start_load(self, duration):
        self.load_running = True
        self.load_starts += 1
        return {"USERS": "10", "SPAWN_RATE": "2", "DURATION": f"{int(duration)}s"}

    def remove_load(self):
        was = self.load_running
        self.load_running = False
        self.removed_load += was
        return was


def run_main(argv=None, **knobs):
    world = World(**knobs)
    out = io.StringIO()
    patches = dict(http_get=world.http_get, compose=world.compose, chaos=world.chaos, stream=world.stream, start_load=world.start_load,
                   remove_load=world.remove_load, load_status=lambda: "running" if world.load_running else "missing",
                   detector_restarts=lambda: (world.restart_count, "running"),
                   detector_started_at=lambda: f"2026-10-21T00:00:{world.started_counter:02d}.000000000Z", utcnow=world.utc,
                   time=SimpleNamespace(monotonic=world.monotonic, sleep=world.sleep),
                   GROUND_TRUTH_PATH=world.truth)
    dc.results.clear()
    dc.warnings.clear()
    dc.summary_rows.clear()
    dc.baseline_rows.clear()
    dc.persist_rows.clear()
    try:
        with mock.patch.multiple(dc, **patches), contextlib.redirect_stdout(out):
            code = dc.main(argv or [])
    finally:
        world.tmp.cleanup()
    return code, out.getvalue(), world


class FlowTestCase(unittest.TestCase):
    def assertLine(self, output, prefix, text):
        lines = [l for l in output.splitlines() if l.startswith(prefix) and text in l]
        self.assertTrue(lines, f"no {prefix} line with {text!r} in:\n{output[-3000:]}")
        return lines[0]


class HappyPathTests(FlowTestCase):
    def test_a_full_run_passes(self):
        code, out, world = run_main()
        self.assertEqual(code, 0, out[-3500:])
        self.assertIn("DETECTOR CHECK PASSED", out)
        for fid in ALL_FAULTS:
            self.assertLine(out, "[OK]", f"{fid} exactly one new incident for the run, none reopened")
            self.assertLine(out, "[OK]", f"{fid} primary_service is {PRIMARY[fid] or 'null'}")
        self.assertEqual([c[3] for c in world.spawned], ALL_FAULTS)
        self.assertEqual([i["incident_id"] for i in world.incidents], [f"INC-{n:04d}" for n in range(1, 10)])   # 8 faults + persistence
        self.assertTrue(all(i["status"] == "resolved" and i["reopen_count"] == 0 for i in world.incidents))
        self.assertEqual(world.started_counter, 1)                                # one planned restart of the detector
        self.assertIn("Persistence (F13)", out)
        self.assertTrue(world.incidents[0]["opened_at"].startswith("2026-10-21"))

    def test_the_delay_is_counted_from_the_end_of_the_inject(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"])
        self.assertEqual(code, 0, out[-3000:])
        # effect starts 1 s after the inject ts; the first symptom is there 10 s later; opening is in the same second
        self.assertLine(out, "[OK]", "F10 opened within 50 s of the end of the inject")
        measured = self.assertLine(out, "    measured:", "INC-0001 opened")
        match = re.search(r"opened ([0-9.]+) s after the end of the inject \(([0-9.]+) s after the inject ts\)", measured)
        end_of_inject, from_ts = float(match.group(1)), float(match.group(2))
        self.assertAlmostEqual(from_ts - end_of_inject, 2.0, delta=0.6)       # the inject took 2 s
        self.assertGreater(end_of_inject, 8)
        self.assertLess(end_of_inject, 14)

    def test_the_baseline_prints_real_durations_and_the_highest_values(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"])
        self.assertIn("baseline idle: 180 s (3.0 min), no load ...", out)
        self.assertIn("baseline 10 users: 300 s (5.0 min) with 10 load-test users ...", out)
        self.assertIn("Baseline durations: idle 180 s, 10 users 300 s", out)
        self.assertIn("highest value per rule and subject over the last 180 s", out)
        self.assertRegex(out, r"R03 +cpu_high +gateway +10 +\d+ % +> 85")
        self.assertLine(out, "[OK]", "baseline idle: no rule fired")
        self.assertLine(out, "[OK]", "baseline 10 users: no rule reported no_data")
        self.assertLine(out, "[OK]", "baseline 10 users: no new incident")
        self.assertLine(out, "[OK]", "baseline idle: Prometheus holds detector_rule_state for 36 rule/subject series")

    def test_quick_mode_shortens_the_phases(self):
        code, out, world = run_main(["--quick", "--only", "F13", "--skip-nodata", "--skip-persistence"])
        self.assertEqual(code, 0, out[-3000:])
        self.assertIn("baseline idle: 60 s (1.0 min)", out)
        self.assertIn("baseline 10 users: 120 s (2.0 min)", out)
        self.assertEqual(world.spawned[0][world.spawned[0].index("--hold") + 1], "75")
        self.assertEqual(len(world.spawned), 1)

    def test_every_fault_goes_through_chaos_and_compose_calls_carry_their_profile(self):
        code, out, world = run_main()
        self.assertEqual(code, 0, out[-3000:])
        for cmd in world.spawned:
            self.assertEqual(cmd[2], "run")
            self.assertEqual(cmd[cmd.index("--warmup") + 1], "0")
        self.assertIn(("inject", "F13"), world.chaos_calls)                      # the persistence step injects through chaos.py too
        self.assertIn(("clear", "F13"), world.chaos_calls)
        for profiles, args in world.compose_calls:
            if args[0] in ("stop", "up") and args[-1] in ("prometheus", "loki"):
                self.assertEqual(profiles, ["observability"], (profiles, args))
            if args[0] == "restart":
                self.assertEqual((profiles, args), (["detector"], ["restart", "detector"]))
            if args[0] == "ps" and "--format" in args:
                self.assertTrue(set(profiles) <= {"observability", "detector"})
        verbs = {args[0] for _, args in world.compose_calls}
        self.assertTrue(verbs <= {"stop", "up", "ps", "restart"}, verbs)
        self.assertEqual([args for _, args in world.compose_calls if args[0] == "stop"], [["stop", "prometheus"], ["stop", "loki"]])
        self.assertEqual([args for _, args in world.compose_calls if args[0] == "restart"], [["restart", "detector"]])

    def test_the_background_load_runs_from_the_second_baseline_phase_to_the_end(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"])
        self.assertEqual(world.load_starts, 1)
        self.assertFalse(world.load_running)
        self.assertLine(out, "[OK]", "background load removed")

    def test_the_wait_between_faults_respects_the_reopen_window(self):
        code, out, world = run_main(["--only", "F10", "F13", "--skip-nodata", "--skip-persistence"])
        self.assertEqual(code, 0, out[-3000:])
        wait = self.assertLine(out, "waiting ", "after the last incident resolved")
        self.assertIn("130 s after the last incident resolved", wait)
        first, second = world.incidents[0], world.incidents[1]
        gap = (datetime.fromisoformat(second["opened_at"].replace("Z", "+00:00"))
               - datetime.fromisoformat(first["resolved_at"].replace("Z", "+00:00"))).total_seconds()
        self.assertGreater(gap, 120)

    def test_the_final_table(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"])
        self.assertIn("Detection per fault", out)
        row = self.assertLine(out, "  F10", "INC-0001")
        self.assertIn("R02 R05 R06", row)
        self.assertIn("postgres", row)
        self.assertTrue(row.rstrip().endswith("PASS"))

    def test_the_clock_difference_is_reported(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], skew_s=0)
        self.assertIn("clock of the detector container minus this host: +0.0 s", out)
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], skew_s=8)
        self.assertLine(out, "[WARN]", "differs from this host by +8.0 s")


class FaultFailureTests(FlowTestCase):
    def test_a_slow_incident_fails_with_the_numbers(self):
        slow = {**EFFECTS, "F10": [("R02", "postgres", 70), ("R05", "gateway", 75)]}
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], effects=slow)
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "F10 opened within 50 s of the end of the inject")
        self.assertRegex(line, r"(69|70)\.\d s after the end of the inject, (71|72)\.\d s after the inject ts; limit 50 s")
        self.assertIn("FAILED: F10 opened within 50 s of the end of the inject", out)
        self.assertNotIn("DETECTOR CHECK PASSED", out)
        self.assertRegex(out, r"F10 +INC-0001 +(69|70)\.\d +50 .*FAIL")

    def test_a_wrong_primary_service_fails(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], primary={**PRIMARY, "F10": None})
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "F10 primary_service is postgres")
        self.assertIn("primary_service is None", out)

    def test_a_missing_rule_fails(self):
        thin = {**EFFECTS, "F13": [("R02", "redis", 10)]}
        code, out, world = run_main(["--only", "F13", "--skip-nodata", "--skip-persistence"], effects=thin)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "F13 the symptoms include R02 (redis), R06")
        self.assertIn("missing R06", out)

    def test_no_incident_at_all_fails(self):
        none = {**EFFECTS, "F06": []}
        code, out, world = run_main(["--only", "F06", "--skip-nodata", "--skip-persistence"], effects=none)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "F06 exactly one new incident for the run, none reopened")

    def test_the_incident_that_does_not_resolve_in_time_fails(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], resolve_s=400.0)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "F10 resolved within 180 s after the recovered event")

    def test_a_health_failure_during_the_fault_is_reported(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], health_fails=True)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "F10 detector /health stayed 200 during the fault")

    def test_without_the_wait_the_next_fault_would_reopen_the_old_incident(self):
        """Proof that the reopen guard matters: with it removed the simulated detector reopens INC-0001 and the script says so."""
        with mock.patch.object(dc, "REOPEN_MARGIN_S", -1000), mock.patch.object(dc, "SETTLE_S", 1):
            code, out, world = run_main(["--only", "F10", "F13", "--skip-nodata", "--skip-persistence"])
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "F13 exactly one new incident for the run, none reopened")
        self.assertIn("reopened: INC-0001", out)

    def test_a_failed_chaos_run_is_reported_and_cleared(self):
        world_holder = {}

        def failing_stream(self_, *a, **k):
            return 1, ["error: could not inject"]
        with mock.patch.object(World, "stream", failing_stream):
            code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"])
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "F10 chaos.py run")
        self.assertIn(("clear", "--all"), world.chaos_calls)


class BaselineFailureTests(FlowTestCase):
    def test_a_rule_that_fires_in_the_baseline_fails(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], spurious=("R05", "gateway", 140))
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "baseline idle: no rule fired")
        self.assertIn("R05 gateway", line)

    def test_a_rule_that_reports_no_data_in_the_baseline_fails(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], spurious_no_data=("R03", "orders", 400))
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "baseline 10 users: no rule reported no_data")
        self.assertIn("R03 orders", line)

    def test_a_new_incident_in_the_baseline_fails(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], spurious_incident_at=100)
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "baseline idle: no new incident")
        self.assertIn("INC-0001 was created", line)


class PreflightTests(FlowTestCase):
    def test_an_open_incident_of_an_earlier_run_is_given_time_to_resolve(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], start_open_incident_for=150)
        self.assertEqual(code, 0, out[-3000:])
        self.assertIn("INC-0001 is still open (an earlier run?): waiting up to 200 s", out)
        self.assertLine(out, "[OK]", "the open incident INC-0001 resolves within 200 s")

    def test_an_incident_that_never_resolves_stops_the_check_with_a_clear_message(self):
        code, out, world = run_main(["--only", "F10"], start_open_incident_for=150, never_resolve_start_incident=True)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "the open incident INC-0001 resolves within 200 s")
        self.assertIn("It did not resolve", out)
        self.assertIn("stopped: INC-0001 is still open", out)
        self.assertEqual(world.spawned, [])                     # nothing was injected
        self.assertIn(("clear", "--all"), world.chaos_calls)    # cleaned up

    def test_the_detector_must_have_both_sources_ok_and_waits_for_the_warm_up(self):
        code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"], prometheus_warming_at_start=40)
        self.assertEqual(code, 0, out[-3000:])
        line = self.assertLine(out, "[OK]", "both data sources of the detector are ok")
        self.assertIn("after waiting", line)

    def test_a_source_that_never_becomes_ok_stops_the_check(self):
        world_kwargs = dict(prometheus_warming_at_start=100000)
        code, out, world = run_main(["--only", "F10"], **world_kwargs)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "both data sources of the detector are ok")
        self.assertEqual(world.spawned, [])


class NoDataStepTests(FlowTestCase):
    def test_the_step_passes_and_checks_everything_it_promises(self):
        code, out, world = run_main(["--only", "F06", "--skip-persistence"])
        self.assertEqual(code, 0, out[-3500:])
        for source in ("prometheus", "loki"):
            self.assertLine(out, "[OK]", f"the detector reports {source} no_data within 30 s")
            self.assertLine(out, "[OK]", f"no incident was created or resolved during the {source} outage")
            self.assertLine(out, "[OK]", f"after the restart the status of {source} goes through warming_up to ok")
            self.assertLine(out, "[OK]", f"90 s after {source} returned there is still no new incident")
            self.assertLine(out, "[OK]", f"{source} is running again")
        self.assertEqual(len(world.incidents), 1)

    def test_an_incident_created_during_the_outage_fails_the_step(self):
        code, out, world = run_main(["--only", "F06", "--skip-persistence"], incident_on_nodata=True)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "no incident was created or resolved during the prometheus outage")

    def test_a_detector_that_reports_the_outage_too_late_fails_the_step(self):
        code, out, world = run_main(["--only", "F06", "--skip-persistence"], nodata_delay_s=45.0)
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "the detector reports prometheus no_data within 30 s")
        self.assertRegex(line, r"\(3\d s\)")

    def test_loki_503_for_minutes_after_the_start_is_retried(self):
        code, out, world = run_main(["--only", "F06", "--skip-persistence"], loki_503_s=150.0)
        self.assertEqual(code, 0, out[-3500:])
        line = self.assertLine(out, "[OK]", "loki is running again")
        self.assertRegex(line, r"attempt \d+ of 36")

    def test_a_loki_that_never_comes_back_fails_but_the_script_still_finishes(self):
        code, out, world = run_main(["--only", "F06", "--skip-persistence"], loki_503_s=100000.0)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "loki is running again")
        self.assertIn("FAILED:", out)

    def test_skip_nodata_never_touches_prometheus_or_loki(self):
        code, out, world = run_main(["--only", "F06", "--skip-nodata", "--skip-persistence"])
        self.assertEqual([a for _, a in world.compose_calls if a[0] in ("stop", "up")], [])

    def test_ctrl_c_during_the_outage_restarts_the_service_and_cleans_up(self):
        original = World.http_get
        state = {"n": 0}

        def interrupt(self_, url, timeout=10):
            if url.startswith(dc.DETECTOR + "/ready") and self_.down["prometheus"]:
                state["n"] += 1
                if state["n"] == 2:
                    raise KeyboardInterrupt
            return original(self_, url, timeout)
        with mock.patch.object(World, "http_get", interrupt):
            code, out, world = run_main(["--only", "F06", "--skip-persistence"])
        self.assertEqual(code, 130)
        self.assertFalse(world.down["prometheus"])
        self.assertFalse(world.down["loki"])
        ups = [(p, a) for p, a in world.compose_calls if a[0] == "up"]
        self.assertTrue(ups and all(p == ["observability"] for p, _ in ups))
        self.assertIn(("clear", "--all"), world.chaos_calls)
        self.assertFalse(world.load_running)
        self.assertIn("interrupted", out)


class InterruptTests(FlowTestCase):
    def test_ctrl_c_during_a_fault_ends_the_child_clears_and_removes_the_load(self):
        def interrupted(self_, command, on_line, poll, poll_every_s, timeout_s, env=None, started=None):
            child = SimpleNamespace(poll=lambda: None, terminate=lambda: setattr(self_, "terminated", True))
            started.append(child)
            raise KeyboardInterrupt
        with mock.patch.object(World, "stream", interrupted):
            code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"])
        self.assertEqual(code, 130)
        self.assertTrue(getattr(world, "terminated", False))
        self.assertIn(("clear", "--all"), world.chaos_calls)
        self.assertFalse(world.load_running)

    def test_an_unexpected_error_cleans_up_too(self):
        def boom(self_, *a, **k):
            raise RuntimeError("bug")
        with mock.patch.object(World, "stream", boom):
            code, out, world = run_main(["--only", "F10", "--skip-nodata", "--skip-persistence"])
        self.assertEqual(code, 1)
        self.assertIn("unexpected error: RuntimeError: bug", out)
        self.assertIn(("clear", "--all"), world.chaos_calls)
        self.assertFalse(world.load_running)


# ----------------------------------------------------------------------------- CP4: F01, F02, F08, F11, F18

class NewFaultTests(FlowTestCase):
    def run_one(self, fid, **knobs):
        return run_main(["--quick", "--only", fid, *FAST], **knobs)

    def test_each_new_fault_passes_alone(self):
        for fid in ("F01", "F02", "F08", "F11", "F18"):
            code, out, world = self.run_one(fid)
            self.assertEqual(code, 0, (fid, out[-3000:]))
            self.assertLine(out, "[OK]", f"{fid} exactly one new incident for the run, none reopened")
            self.assertLine(out, "[OK]", f"{fid} primary_service is {PRIMARY[fid] or 'null'}")
            self.assertEqual([c[3] for c in world.spawned], [fid])
            self.assertEqual(len(world.incidents), 1)

    def test_the_hold_is_the_limit_plus_a_margin_for_every_fault(self):
        for fid, limit in (("F01", 110), ("F02", 140), ("F08", 80), ("F11", 110), ("F18", 110)):
            code, out, world = self.run_one(fid)
            cmd = world.spawned[0]
            self.assertEqual(cmd[cmd.index("--hold") + 1], str(limit + 25), fid)     # quick: limit + 25 s
        code, out, world = run_main(["--only", "F02", *FAST])
        self.assertEqual(world.spawned[0][world.spawned[0].index("--hold") + 1], "180")   # normal: limit + 40 s, within the 600 s cap of chaos.py

    def test_f18_prints_the_first_symptom_the_opening_rule_and_the_order(self):
        code, out, world = self.run_one("F18")
        self.assertEqual(code, 0, out[-3000:])
        line = self.assertLine(out, "    opened by", "R07 gateway")
        self.assertRegex(line, r"first symptom \+\d+\.\d s from the end of the inject")
        self.assertRegex(line, r"R07 gateway \+\d+\.\d s, R08 gateway \+\d+\.\d s, R03 gateway \+\d+\.\d s")
        self.assertLine(out, "[OK]", "F18 at least 2 distinct rule ids among the symptoms")
        self.assertLine(out, "[OK]", "F18 candidate_services contains gateway")
        row = self.assertLine(out, "  F18", "INC-0001")
        self.assertIn("R07 gateway", row)
        self.assertIn("R03 R07 R08", row)

    def test_f18_r08_may_open_first_and_the_script_says_so(self):
        flipped = {**EFFECTS, "F18": [("R08", "gateway", 3), ("R07", "gateway", 40)]}
        code, out, world = self.run_one("F18", effects=flipped)
        self.assertEqual(code, 0, out[-3000:])
        self.assertLine(out, "    opened by", "R08 gateway")

    def test_f18_with_one_rule_id_fails(self):
        code, out, world = self.run_one("F18", effects={**EFFECTS, "F18": [("R08", "gateway", 10)]})
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "F18 at least 2 distinct rule ids among the symptoms")
        self.assertLine(out, "[FAIL]", "F18 the symptoms include R08 (gateway), R07 (gateway)")

    def test_f18_without_the_gateway_among_the_candidates_fails(self):
        code, out, world = self.run_one("F18", candidates_override=["orders"])
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "F18 candidate_services contains gateway")
        self.assertIn("['orders']", line)

    def test_a_root_service_where_the_table_says_null_fails(self):
        for fid in ("F08", "F11", "F18"):
            code, out, world = self.run_one(fid, primary={**PRIMARY, fid: "postgres"})
            self.assertEqual(code, 1, fid)
            line = self.assertLine(out, "[FAIL]", f"{fid} primary_service is null")
            self.assertIn("'postgres'", line)

    def test_a_missing_root_service_where_the_table_names_one_fails(self):
        for fid, name in (("F01", "orders"), ("F02", "payments")):
            code, out, world = self.run_one(fid, primary={**PRIMARY, fid: None})
            self.assertEqual(code, 1, fid)
            self.assertLine(out, "[FAIL]", f"{fid} primary_service is {name}")

    def test_each_limit_is_enforced_with_the_numbers(self):
        for fid, limit in (("F01", 110), ("F02", 140), ("F08", 80), ("F11", 110), ("F18", 110)):
            slow = {**EFFECTS, fid: [(r, svc, limit + 6) if i == 0 else (r, svc, limit + 8)
                                     for i, (r, svc, _) in enumerate(EFFECTS[fid])]}
            code, out, world = self.run_one(fid, effects=slow)
            self.assertEqual(code, 1, fid)
            line = self.assertLine(out, "[FAIL]", f"{fid} opened within {limit} s of the end of the inject")
            self.assertRegex(line, rf"\b{limit + 5}\.\d s after the end of the inject")
            self.assertIn(f"limit {limit} s", line)

    def test_an_incident_just_inside_the_limit_passes(self):
        for fid, limit in (("F02", 140), ("F08", 80)):
            fast = {**EFFECTS, fid: [(r, svc, limit - 2) if i == 0 else (r, svc, limit - 1) for i, (r, svc, _) in enumerate(EFFECTS[fid])]}
            code, out, world = self.run_one(fid, effects=fast)
            self.assertEqual(code, 0, (fid, out[-2500:]))

    def test_f08_and_f11_need_both_rules(self):
        code, out, world = self.run_one("F08", effects={**EFFECTS, "F08": [("R06", "orders", 8)]})
        self.assertEqual(code, 1)
        self.assertIn("missing R05", out)
        code, out, world = self.run_one("F11", effects={**EFFECTS, "F11": [("R07", "gateway", 18), ("R05", "gateway", 20)]})
        self.assertEqual(code, 1)
        self.assertIn("missing R07 (inventory)", out)

    def test_the_wait_between_two_new_faults_respects_the_reopen_window(self):
        code, out, world = run_main(["--only", "F01", "F02", "F18", *FAST])
        self.assertEqual(code, 0, out[-3000:])
        self.assertEqual(len(world.incidents), 3)
        for older, newer in zip(world.incidents, world.incidents[1:]):
            gap = (datetime.fromisoformat(newer["opened_at"].replace("Z", "+00:00"))
                   - datetime.fromisoformat(older["resolved_at"].replace("Z", "+00:00"))).total_seconds()
            self.assertGreater(gap, 120)
        self.assertTrue(all(i["reopen_count"] == 0 for i in world.incidents))

    def test_the_only_option_keeps_the_table_order(self):
        code, out, world = run_main(["--only", "F18", "F01", *FAST])
        self.assertEqual([c[3] for c in world.spawned], ["F01", "F18"])


# ----------------------------------------------------------------------------- CP4: the persistence step

class PersistenceStepTests(FlowTestCase):
    def run_persistence(self, extra=None, **knobs):
        return run_main(["--quick", "--only", "F10", "--skip-nodata", *(extra or [])], **knobs)

    def test_the_step_passes(self):
        code, out, world = self.run_persistence()
        self.assertEqual(code, 0, out[-3500:])
        self.assertLine(out, "[OK]", "persistence: the incident is open with the expected rules before the restart")
        self.assertLine(out, "[OK]", "persistence: docker compose --profile detector restart detector")
        self.assertLine(out, "[OK]", "persistence: detector /ready answers 200 after the restart")
        self.assertLine(out, "[OK]", "persistence: the detector container was really restarted (StartedAt changed)")
        for when in ("right after the restart", "after the warm-up"):
            self.assertLine(out, "[OK]", f"persistence: {when} the same incident INC-0002 is still open")
            self.assertLine(out, "[OK]", f"persistence: {when} none of its symptoms was lost")
            self.assertLine(out, "[OK]", f"persistence: {when} no second incident appeared")
        self.assertLine(out, "[OK]", "persistence: both data sources are ok again after the warm-up")
        self.assertLine(out, "[OK]", "persistence: the symptom of the running fault is still active after the restart")
        self.assertLine(out, "[OK]", "persistence: exactly one new incident for the run, none reopened")
        self.assertLine(out, "[OK]", "persistence: resolved within 180 s after the recovered event")
        self.assertLine(out, "[OK]", "the detector restarted only as planned")
        self.assertIn("Persistence (F13): the detector was restarted while INC-0002 was open: PASS", out)
        self.assertEqual([i["incident_id"] for i in world.incidents], ["INC-0001", "INC-0002"])
        self.assertTrue(all(i["status"] == "resolved" and i["reopen_count"] == 0 for i in world.incidents))
        self.assertEqual(world.started_counter, 1)

    def test_the_restart_happens_while_the_incident_is_open_and_the_fault_keeps_running(self):
        calls = []
        original = World.compose

        def watching(self_, profiles, *args, timeout=None, env=None):
            if args[0] == "restart":
                open_ids = [i["incident_id"] for i in self_.incidents if i["status"] == "open"]
                calls.append((open_ids, self_.fault is not None and not self_.fault["cleared"]))
            return original(self_, profiles, *args, timeout=timeout, env=env)
        with mock.patch.object(World, "compose", watching):
            code, out, world = self.run_persistence()
        self.assertEqual(calls, [(["INC-0002"], True)])

    def test_the_waiting_rules_apply_before_the_persistence_fault(self):
        code, out, world = self.run_persistence()
        gap = (datetime.fromisoformat(world.incidents[1]["opened_at"].replace("Z", "+00:00"))
               - datetime.fromisoformat(world.incidents[0]["resolved_at"].replace("Z", "+00:00"))).total_seconds()
        self.assertGreater(gap, 120)

    def test_skip_persistence_never_injects_or_restarts(self):
        code, out, world = self.run_persistence(["--skip-persistence"])
        self.assertEqual(code, 0, out[-2000:])
        self.assertNotIn(("inject", "F13"), world.chaos_calls)
        self.assertEqual([a for _, a in world.compose_calls if a[0] == "restart"], [])
        self.assertNotIn("Persistence", out)
        self.assertEqual(world.started_counter, 0)

    def test_a_detector_that_forgets_its_incident_fails_the_step(self):
        code, out, world = self.run_persistence(restart_loses_incident=True)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "persistence: right after the restart the same incident INC-0002 is still open")
        self.assertLine(out, "[FAIL]", "persistence: exactly one new incident for the run, none reopened")
        self.assertIn("Persistence (F13): the detector was restarted while", out)
        self.assertIn(": FAIL", out)

    def test_a_second_incident_after_the_restart_is_reported(self):
        code, out, world = self.run_persistence(restart_loses_incident=True)
        self.assertTrue(any(i["incident_id"] == "INC-0003" for i in world.incidents))      # the buggy detector opened another one
        self.assertLine(out, "[FAIL]", "persistence: after the warm-up no second incident appeared")

    def test_a_restart_that_did_not_happen_is_detected(self):
        code, out, world = self.run_persistence(restart_noop=True)
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "persistence: the detector container was really restarted (StartedAt changed)")
        self.assertIn("2026-10-21T00:00:00", line)

    def test_a_failed_restart_command_fails_and_the_fault_is_still_cleared(self):
        code, out, world = self.run_persistence(restart_fails=True)
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "persistence: docker compose --profile detector restart detector")
        self.assertIn(("clear", "F13"), world.chaos_calls)
        self.assertFalse(world.fault is not None and not world.fault["cleared"])

    def test_restart_count_may_rise_by_the_planned_restart_only(self):
        code, out, world = self.run_persistence(restart_increments_count=True)
        self.assertEqual(code, 0, out[-2500:])
        self.assertLine(out, "[OK]", "the detector restarted only as planned")
        code, out, world = self.run_persistence(restart_increments_count=True, restart_extra_count=1)
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "the detector restarted only as planned")
        self.assertIn("RestartCount 0 -> 2 (1 planned restart(s))", line)

    def test_symptoms_that_are_not_active_after_the_restart_fail_the_step(self):
        code, out, world = self.run_persistence(restart_deactivates_symptoms=True)
        self.assertEqual(code, 1)
        line = self.assertLine(out, "[FAIL]", "persistence: the symptom of the running fault is still active after the restart")
        self.assertIn("R02 (redis)", line)

    def test_the_incident_is_watched_after_the_warm_up_for_the_observation_time(self):
        times = {}
        original_compose, original_chaos = World.compose, World.chaos

        def compose(self_, profiles, *args, timeout=None, env=None):
            result = original_compose(self_, profiles, *args, timeout=timeout, env=env)
            if args[0] == "restart":
                times["restart_done"] = self_.t
            return result

        def chaos(self_, *args):
            if args[:2] == ("clear", "F13"):
                times["clear"] = self_.t
            return original_chaos(self_, *args)
        with mock.patch.object(World, "compose", compose), mock.patch.object(World, "chaos", chaos):
            code, out, world = self.run_persistence()
        self.assertEqual(code, 0, out[-2500:])
        warmup, observe = 30, dc.QUICK_PERSIST_OBSERVE_S
        self.assertGreaterEqual(times["clear"] - times["restart_done"], warmup + observe)     # warm-up, then the observation

    def test_a_slow_restart_is_waited_for(self):
        code, out, world = self.run_persistence(restart_takes_s=40.0)
        self.assertEqual(code, 0, out[-2500:])

    def test_ctrl_c_after_the_restart_started_makes_sure_the_detector_runs_and_clears_the_fault(self):
        original = World.http_get
        state = {"seen": False}

        def interrupt(self_, url, timeout=10):
            if url.startswith(dc.DETECTOR + "/ready") and self_.started_counter == 1 and not state["seen"]:
                state["seen"] = True
                raise KeyboardInterrupt
            return original(self_, url, timeout)
        with mock.patch.object(World, "http_get", interrupt):
            code, out, world = self.run_persistence()
        self.assertEqual(code, 130)
        self.assertIn(("clear", "F13"), world.chaos_calls)
        self.assertIn(("clear", "--all"), world.chaos_calls)
        ups = [(p, a) for p, a in world.compose_calls if a[:1] == ["up"] and a[-1] == "detector"]
        self.assertEqual(ups, [(["detector"], ["up", "-d", "detector"])])
        self.assertFalse(world.load_running)

    def test_a_persistence_step_that_cannot_inject_stops_cleanly(self):
        original = World.chaos

        def failing(self_, *args):
            if args[:1] == ("inject",):
                return dc.Result(1, "", "refused")
            return original(self_, *args)
        with mock.patch.object(World, "chaos", failing):
            code, out, world = self.run_persistence()
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "persistence: chaos.py inject F13")
        self.assertEqual([a for _, a in world.compose_calls if a[0] == "restart"], [])

    def test_the_finish_check_sees_the_planned_start_but_catches_another_one(self):
        original = World.chaos

        def extra_restart(self_, *args):
            if args[:1] == ("status",) and self_.started_counter == 1 and not getattr(self_, "extra_done", False):
                self_.extra_done = True
                self_.started_counter += 1                  # the detector was started again behind the script's back
            return original(self_, *args)
        with mock.patch.object(World, "chaos", extra_restart):
            code, out, world = self.run_persistence()
        self.assertEqual(code, 1)
        self.assertLine(out, "[FAIL]", "the detector was not started again after the last planned start (StartedAt)")


class BannerTests(FlowTestCase):
    def banner(self, argv):
        code, out, world = run_main(argv)
        return out.splitlines()[0]

    def test_the_banner_matches_the_estimate(self):
        full = dc.estimate_run_s(list(dc.FAULT_ORDER), False, False, False)
        most = dc.estimate_run_s(list(dc.FAULT_ORDER), False, False, False, at_most=True)
        line = self.banner([])
        self.assertIn("F10, F13, F06, F01, F02, F08, F11, F18 + no-data step + persistence step", line)
        self.assertIn(f"about {-(-full // 60)} minutes (typical, from the measured times), at most about {-(-most // 60)}", line)
        self.assertIn("about 75 minutes", line)
        self.assertIn("at most about 87", line)

    def test_the_banner_follows_the_options(self):
        self.assertIn("F01 | quick mode", self.banner(["--quick", "--only", "F01", *FAST]))
        self.assertIn("F01 + persistence step | quick mode", self.banner(["--quick", "--only", "F01", "--skip-nodata"]))
        self.assertIn("F01 + no-data step | quick mode", self.banner(["--quick", "--only", "F01", "--skip-persistence"]))
        text = self.banner(["--quick", "--only", "F01", *FAST])
        self.assertIn("about 10 minutes", text)

    def test_the_real_run_is_not_longer_than_the_estimate_in_the_simulated_world(self):
        """The simulated detector resolves after 65 s instead of the measured 120 s, so it must stay under the typical estimate."""
        code, out, world = run_main(["--quick", "--only", "F10", *FAST])
        self.assertEqual(code, 0)
        self.assertLess(world.t, dc.estimate_run_s(["F10"], True, True, True, at_most=True))


if __name__ == "__main__":
    unittest.main()
