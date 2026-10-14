"""Unit tests for the chaos tool (module M3): pure logic, plus command sequences with a fake docker.

Run from the repo root (host Python, standard library only):
    python -m unittest discover -s chaos/tests

No real docker call happens here. A few tests use a fake command runner to check the
command sequences of F10 and F13.
"""

import base64
import copy
import json
import re
import signal
import sys
import tempfile
import unittest
from pathlib import Path

CHAOS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHAOS_DIR))

import chaos      # noqa: E402  (needs the sys.path line above)
import faults     # noqa: E402

CATALOGUE = json.loads((CHAOS_DIR / "catalogue.json").read_text(encoding="utf-8"))
EXPECTED_IDS = ["F01", "F02", "F06", "F08", "F10", "F11", "F13", "F18"]


def catalogue_dict():
    return {fault["id"]: fault for fault in copy.deepcopy(CATALOGUE)}


def row(service, state="running", health="healthy"):
    return {"Service": service, "State": state, "Health": health}


def healthy_rows():
    return [row(name) for name in faults.M1_SERVICES]


# ---------------------------------------------------------------------------
class CatalogueTests(unittest.TestCase):
    def test_real_catalogue_is_valid_and_has_the_8_faults(self):
        self.assertEqual(chaos.validate_catalogue(CATALOGUE), [])
        self.assertEqual([f["id"] for f in CATALOGUE], EXPECTED_IDS)

    def test_load_catalogue_returns_dict_in_file_order(self):
        self.assertEqual(list(chaos.load_catalogue(chaos.CATALOGUE_PATH)), EXPECTED_IDS)

    def test_every_fault_has_a_symptom_with_valid_condition(self):
        for fault in CATALOGUE:
            self.assertTrue(fault["expected_symptoms"], fault["id"])
            for symptom in fault["expected_symptoms"]:
                self.assertRegex(symptom["condition"], chaos.CONDITION_RE)

    def test_missing_field_is_reported(self):
        bad = copy.deepcopy(CATALOGUE)
        del bad[0]["expected_action"]
        self.assertTrue(any("missing fields" in e and "expected_action" in e for e in chaos.validate_catalogue(bad)))

    def test_bad_values_are_reported(self):
        cases = [
            ("category", "Network", "category"),
            ("max_duration_s", 601, "max_duration_s"),
            ("default_target", "gateway", "default_target"),
            ("expected_action", "reboot_host", "expected_action"),
            ("acceptable_actions", ["scale_service"], "acceptable_actions"),
            ("allowed_targets", ["nginx"], "allowed_targets"),
        ]
        for key, value, word in cases:
            with self.subTest(key=key):
                bad = copy.deepcopy(CATALOGUE)
                bad[0][key] = value
                errors = chaos.validate_catalogue(bad)
                self.assertTrue(any(word in e for e in errors), errors)

    def test_bad_root_cause_type_param_range_and_symptom(self):
        bad = copy.deepcopy(CATALOGUE)
        bad[0]["expected_root_cause"]["type"] = "gremlins"
        bad[0]["params"][0]["default"] = 99          # outside min..max
        bad[0]["expected_symptoms"][0]["condition"] = "about 5"
        bad[0]["expected_symptoms"][0]["within_s"] = 0
        bad[0]["expected_symptoms"][0]["source"] = "grafana"
        errors = " | ".join(chaos.validate_catalogue(bad))
        for word in ("expected_root_cause.type", "min <= default <= max", "condition", "within_s", "source"):
            self.assertIn(word, errors)

    def test_duplicate_id_and_non_list(self):
        bad = copy.deepcopy(CATALOGUE) + [copy.deepcopy(CATALOGUE[0])]
        self.assertTrue(any("duplicate id" in e for e in chaos.validate_catalogue(bad)))
        self.assertTrue(chaos.validate_catalogue({"id": "F01"}))

    def test_hard_cap_600_for_every_fault(self):
        self.assertTrue(all(f["max_duration_s"] <= 600 for f in CATALOGUE))


# ---------------------------------------------------------------------------
class ArgumentValidationTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue_dict()
        self.built = {"F10", "F13"}

    def validate(self, fid, **kwargs):
        return chaos.validate_request(self.cat, fid, implemented=self.built, planned=faults.PLANNED, **kwargs)

    def test_defaults(self):
        request = self.validate("F10")
        self.assertEqual((request.fault_id, request.target, request.duration_s, request.params),
                         ("F10", "postgres", None, {}))

    def test_lower_case_id_is_accepted(self):
        self.assertEqual(self.validate("f13").fault_id, "F13")

    def test_unknown_fault_is_refused(self):
        with self.assertRaisesRegex(chaos.ValidationError, "unknown fault"):
            self.validate("F99")

    def test_target_not_allowed_is_refused(self):
        with self.assertRaisesRegex(chaos.ValidationError, "not allowed"):
            self.validate("F10", target="redis")

    def test_duration_over_cap_is_refused_even_for_a_fault_not_built_yet(self):
        with self.assertRaisesRegex(chaos.ValidationError, "above the cap"):
            self.validate("F01", duration=9999)

    def test_duration_on_a_fault_without_own_duration_is_refused(self):
        with self.assertRaisesRegex(chaos.ValidationError, "no duration of its own"):
            self.validate("F10", duration=30)

    def test_duration_must_be_positive(self):
        with self.assertRaisesRegex(chaos.ValidationError, "at least 1"):
            self.validate("F01", duration=0)

    def test_valid_duration_and_params_for_a_built_fault_in_the_future(self):
        built = self.built | {"F02"}
        request = chaos.validate_request(self.cat, "F02", duration=300, param_items=["step_mib=16"],
                                         implemented=built)
        self.assertEqual((request.duration_s, request.params), (300, {"step_mib": 16, "interval_s": 2}))

    def test_bad_params_are_refused(self):
        built = {"F02"}
        for items, word in ((["nope=1"], "unknown parameter"), (["step_mib=abc"], "whole number"),
                            (["step_mib=999"], "outside the allowed range"), (["step_mib=0"], "outside"),
                            (["step_mib"], "name=value"), (["step_mib=1", "step_mib=2"], "twice")):
            with self.subTest(items=items):
                with self.assertRaisesRegex(chaos.ValidationError, word):
                    chaos.validate_request(self.cat, "F02", param_items=items, implemented=built)

    def test_param_on_fault_without_params_is_refused(self):
        with self.assertRaisesRegex(chaos.ValidationError, "unknown parameter"):
            self.validate("F10", param_items=["x=1"])

    def test_fault_not_built_yet_is_refused_with_the_planned_checkpoint(self):
        for fid in ("F11", "F18"):
            with self.assertRaisesRegex(chaos.ValidationError, "not built yet.*CP4"):
                chaos.validate_request(self.cat, fid, implemented=faults.IMPLEMENTED, planned=faults.PLANNED)

    def test_the_built_faults(self):
        self.assertEqual(sorted(faults.IMPLEMENTED), ["F01", "F02", "F06", "F08", "F10", "F13"])
        request = chaos.validate_request(self.cat, "F01", param_items=["processes=3"],
                                         implemented=faults.IMPLEMENTED, planned=faults.PLANNED)
        self.assertEqual((request.target, request.duration_s, request.params), ("orders", 180, {"processes": 3}))

    def test_f06_f08_allow_three_targets_only(self):
        self.assertEqual(self.cat["F06"]["allowed_targets"], ["orders", "payments", "inventory"])
        with self.assertRaises(chaos.ValidationError):
            chaos.validate_request(self.cat, "F08", target="gateway")

    def test_run_options(self):
        fault = self.cat["F10"]
        self.assertEqual(chaos.validate_run_options(fault, 60, None, 60), (60, 60, 60))
        self.assertEqual(chaos.validate_run_options(self.cat["F18"], 0, None, 0), (0, 120, 0))
        for args in ((-1, 10, 0), (0, 0, 0), (0, 601, 0), (0, 10, 601)):
            with self.subTest(args=args):
                with self.assertRaises(chaos.ValidationError):
                    chaos.validate_run_options(fault, *args)


# ---------------------------------------------------------------------------
class GroundTruthTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue_dict()

    def record(self, fid="F10", target="postgres"):
        return {"run_id": f"{fid}-abcd1234", "fault_id": fid, "target": target, "params": {}}

    def test_now_iso_format(self):
        self.assertRegex(chaos.now_iso(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_field_order_and_values_of_an_inject_event(self):
        event = chaos.make_event("inject", self.record(), self.cat["F10"], "2026-10-11T10:00:00.000Z")
        self.assertEqual(list(event), ["run_id", "event", "fault_id", "category", "target", "params", "ts",
                                       "expected_root_cause", "expected_action", "acceptable_actions"])
        self.assertEqual(event["expected_root_cause"], {"service": "postgres", "type": "database_unavailable"})
        self.assertEqual(event["expected_action"], "restart_service")

    def test_target_placeholder_is_resolved(self):
        event = chaos.make_event("inject", self.record("F06", "payments"), self.cat["F06"], "2026-10-11T10:00:00.000Z")
        self.assertEqual(event["expected_root_cause"], {"service": "payments", "type": "misconfiguration"})

    def test_null_service_stays_null_for_f18(self):
        event = chaos.make_event("inject", self.record("F18", "gateway"), self.cat["F18"], "2026-10-11T10:00:00.000Z")
        self.assertEqual(event["expected_root_cause"], {"service": None, "type": "capacity_limit"})

    def test_extra_fields(self):
        recovered = chaos.make_event("recovered", self.record(), self.cat["F10"], "2026-10-11T10:00:09.000Z",
                                     recovery_s=8.5)
        aborted = chaos.make_event("aborted", self.record(), self.cat["F10"], "2026-10-11T10:00:09.000Z",
                                   reason="interrupted by Ctrl+C")
        self.assertEqual(recovered["recovery_s"], 8.5)
        self.assertEqual(aborted["reason"], "interrupted by Ctrl+C")

    def test_line_is_one_json_line_ending_in_newline(self):
        event = chaos.make_event("clear", self.record(), self.cat["F10"], "2026-10-11T10:00:00.000Z")
        line = chaos.format_ground_truth_line(event)
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(json.loads(line), event)

    def test_writer_appends_utf8_with_unix_line_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "ground_truth.jsonl"       # parent folder does not exist yet
            event = chaos.make_event("inject", self.record(), self.cat["F10"], "2026-10-11T10:00:00.000Z")
            for _ in range(2):
                chaos.append_line(path, chaos.format_ground_truth_line(event))
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw)
            lines = raw.decode("utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                self.assertEqual(json.loads(line)["fault_id"], "F10")

    def test_password_is_masked_in_a_ground_truth_line(self):
        record = self.record()
        record["params"] = {"note": "postgresql://shop:hunter2pw@postgres:5432/shop"}
        event = chaos.make_event("inject", record, self.cat["F10"], "2026-10-11T10:00:00.000Z")
        line = chaos.format_ground_truth_line(event, lambda text: chaos.mask_secrets(text, ["hunter2pw"]))
        self.assertNotIn("hunter2pw", line)
        json.loads(line)        # still valid JSON


# ---------------------------------------------------------------------------
class MaskingTests(unittest.TestCase):
    def test_known_secret_is_replaced(self):
        self.assertEqual(chaos.mask_secrets("login with s3cr3tpw now", ["s3cr3tpw"]), "login with *** now")

    def test_password_inside_url_is_masked_without_knowing_it(self):
        text = chaos.mask_secrets("DATABASE_URL=postgresql://shop:abc123xyz@postgres:5432/shop")
        self.assertNotIn("abc123xyz", text)
        self.assertIn("postgresql://shop:***@postgres", text)

    def test_key_value_pairs(self):
        self.assertEqual(chaos.mask_secrets("POSTGRES_PASSWORD=abc"), "POSTGRES_PASSWORD=***")
        self.assertEqual(chaos.mask_secrets("grafana_admin_password: xyz"), "grafana_admin_password: ***")

    def test_text_without_secrets_is_unchanged(self):
        self.assertEqual(chaos.mask_secrets("docker compose stop postgres"), "docker compose stop postgres")

    def test_empty_secret_is_ignored(self):
        self.assertEqual(chaos.mask_secrets("abc", [""]), "abc")

    def test_load_env_secrets_reads_only_secret_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nPOSTGRES_USER=shop\nPOSTGRES_PASSWORD=pw_value_1\n"
                           "GRAFANA_ADMIN_PASSWORD='pw_value_2'\nUSERS=20\n", encoding="utf-8")
            self.assertEqual(chaos.load_env_secrets(env), ["pw_value_1", "pw_value_2"])
        self.assertEqual(chaos.load_env_secrets(Path("does-not-exist")), [])

    def test_docker_echo_and_errors_are_masked(self):
        echoed = []

        def runner(args, timeout):
            return faults.CmdResult(1, "", "boom hunter2pw")

        dk = faults.Docker(runner=runner, echo=echoed.append,
                           mask=lambda t: chaos.mask_secrets(t, ["hunter2pw"]), repo_root=Path("."))
        with self.assertRaises(faults.FaultError) as ctx:
            dk.compose("stop", "postgres", "hunter2pw")
        self.assertNotIn("hunter2pw", echoed[0])
        self.assertNotIn("hunter2pw", str(ctx.exception))


# ---------------------------------------------------------------------------
class StateTests(unittest.TestCase):
    def test_missing_file_means_no_active_faults(self):
        self.assertEqual(chaos.read_state(Path("no-such-folder") / "active.json"), {})

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "active.json"
            state = {"F10": {"run_id": "F10-1", "fault_id": "F10", "target": "postgres"}}
            chaos.write_state(state, path)
            self.assertEqual(chaos.read_state(path), state)
            self.assertFalse(path.with_name("active.json.tmp").exists())
            chaos.write_state({}, path)
            self.assertEqual(chaos.read_state(path), {})

    def test_corrupt_file_gives_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "active.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(chaos.StateError, "cannot be read"):
                chaos.read_state(path)
            path.write_text('{"other": 1}', encoding="utf-8")
            with self.assertRaises(chaos.StateError):
                chaos.read_state(path)

    def test_format_age(self):
        self.assertEqual([chaos.format_age(s) for s in (4, 65, 3725)], ["4s", "1m05s", "1h02m"])


class StaleDecisionTests(unittest.TestCase):
    def test_f10_is_stale_when_postgres_runs_again(self):
        record = {"fault_id": "F10", "target": "postgres"}
        self.assertEqual(chaos.fault_status(record, {"postgres", "redis"}), "stale")
        self.assertEqual(chaos.fault_status(record, {"redis"}), "active")

    def test_f13_follows_redis(self):
        record = {"fault_id": "F13", "target": "redis"}
        self.assertEqual(chaos.fault_status(record, {"redis"}), "stale")
        self.assertEqual(chaos.fault_status(record, {"postgres"}), "active")

    def test_unknown_fault_is_never_called_stale(self):
        self.assertFalse(faults.effect_gone("F11", "postgres", {"postgres"}))

    def test_process_faults_follow_the_marker_process_count(self):
        running = {"orders", "payments"}
        self.assertFalse(faults.effect_gone("F01", "orders", running, {"F01": 2}))
        self.assertTrue(faults.effect_gone("F01", "orders", running, {"F01": 0}))
        self.assertTrue(faults.effect_gone("F02", "payments", running, {"F02": 0}))
        self.assertTrue(faults.effect_gone("F01", "orders", {"payments"}, {}))      # container not running
        self.assertFalse(faults.effect_gone("F01", "orders", running, {}))          # could not count: unknown
        self.assertFalse(faults.effect_gone("F01", "orders", running, None))


# ---------------------------------------------------------------------------
class ComposePsTests(unittest.TestCase):
    def test_parses_one_object_per_line(self):
        text = "\n".join(json.dumps(r) for r in healthy_rows()) + "\n"
        self.assertEqual(len(faults.parse_compose_ps(text)), 6)

    def test_parses_a_json_array_and_empty_output(self):
        self.assertEqual(len(faults.parse_compose_ps(json.dumps(healthy_rows()))), 6)
        self.assertEqual(faults.parse_compose_ps("  \n"), [])

    def test_garbage_is_an_error(self):
        with self.assertRaises(faults.FaultError):
            faults.parse_compose_ps("not json at all\n")

    def test_not_healthy_reports_each_reason(self):
        rows = [r for r in healthy_rows() if r["Service"] != "gateway"]       # gateway has no container
        rows[0] = row("postgres", "exited", "")
        rows[1] = row("redis", "running", "starting")
        states = faults.service_states(rows + [row("loki", "running", "")])    # loki is not an M1 service
        self.assertEqual(faults.not_healthy(states),
                         ["postgres (exited)", "redis (health: starting)", "gateway (no container)"])
        self.assertEqual(faults.not_healthy(faults.service_states(healthy_rows())), [])
        self.assertEqual(faults.running_services(states), {"redis", "inventory", "payments", "orders"})


# ---------------------------------------------------------------------------
REAL_URL = "postgresql://shop:realpw-secret@postgres:5432/shop"       # the fake stack's "real" password


class FakeStack:
    """Pretends to be docker: remembers stopped services, marker processes, files and container
    environments; records every command. The F06/F08 fault files are really read from disk."""

    def __init__(self):
        self.stopped = set()
        self.commands = []
        self.procs = {}              # service -> number of fault processes running inside
        self.files = set()           # (service, path)
        self.started = []            # (program, arguments) of every detached `exec -d`
        self.on_start = None         # callback(service) after `docker compose start <service>`
        self.exec_fails = False
        self.memory_limit = 268435456
        self.f02_state = "growing"
        self.base_env = {name: ["PATH=/usr/bin", f"SERVICE_NAME={name}", "DATABASE_URL=" + REAL_URL]
                         for name in ("orders", "payments", "inventory")}
        self.env = {name: list(env) for name, env in self.base_env.items()}
        self.removed = set()         # services without any container
        self.crash = set()           # services in a crash loop
        self.reset_works = True      # False: the '!reset' tag does not remove the variable
        self.repo_root = CHAOS_DIR.parent

    # -- helpers --------------------------------------------------------------------------------
    def _service_of(self, cid):
        return cid[len("cid-"):]

    def _fault_env(self, service, files):
        """Environment the service gets when compose merges the given -f files (only fault files matter here)."""
        env = list(self.base_env.get(service, []))
        for name in files:
            if not name.startswith("chaos/overrides/"):
                continue
            target = name.rsplit("/", 1)[1][:-4].split("-", 1)[1]
            if target != service:
                continue
            text = (Path(self.repo_root) / name).read_text(encoding="utf-8")
            if "!reset" in text:
                if self.reset_works:
                    env = [e for e in env if not e.startswith("DATABASE_URL=")]
            else:
                value = re.search(r'DATABASE_URL: "(.*)"', text).group(1)
                value = value.replace("${POSTGRES_USER:-shop}", "shop").replace("${POSTGRES_DB:-shop}", "shop")
                env = [e for e in env if not e.startswith("DATABASE_URL=")] + ["DATABASE_URL=" + value]
        return env

    @staticmethod
    def _files(args):
        return [args[i + 1] for i, a in enumerate(args) if a == "-f"]

    def _exec(self, args):
        service = args[args.index("-T") + 1]
        if self.exec_fails or service in self.stopped:
            return faults.CmdResult(1, "", f'service "{service}" is not running')
        at = args.index("-c")
        code = base64.b64decode(args[at + 2]).decode()
        extra = args[at + 3:]
        if "-d" in args:
            self.procs[service] = self.procs.get(service, 0) + 1
            self.started.append((code, extra))
            return faults.CmdResult(0, "", "")
        if code == faults.PY_SCAN:
            return faults.CmdResult(0, f"{self.procs.get(service, 0)}\n", "")
        if code == faults.PY_TOUCH:
            self.files.add((service, extra[0]))
            self.procs[service] = 0                      # the processes see the stop file and end
        elif code == faults.PY_REMOVE:
            for path in extra:
                self.files.discard((service, path))
        elif code == faults.PY_READ:
            if self.procs.get(service, 0):
                return faults.CmdResult(0, json.dumps({"state": self.f02_state, "held_mib": 8}) + "\n", "")
            return faults.CmdResult(0, "\n", "")
        return faults.CmdResult(0, "", "")

    def _config(self, args):
        files = self._files(args)
        services = {name: {"environment": {e.split("=", 1)[0]: e.split("=", 1)[1] for e in self._fault_env(name, files)}}
                    for name in self.base_env}
        return faults.CmdResult(0, json.dumps({"name": "sentinelops", "services": services}), "")

    def _up(self, args):
        service = args[-1]
        self.env[service] = self._fault_env(service, self._files(args))
        self.removed.discard(service)
        self.stopped.discard(service)
        if faults.database_url_from_env(self.env[service]) is None:
            self.crash.add(service)
        else:
            self.crash.discard(service)
        return faults.CmdResult(0, "", "")

    def _inspect(self, args):
        fmt, cid = args[args.index("--format") + 1], args[-1]
        service = self._service_of(cid)
        if fmt == "{{json .Config.Env}}":
            return faults.CmdResult(0, json.dumps(self.env.get(service, [])) + "\n", "")
        if fmt == "{{.RestartCount}} {{.State.Status}}":
            return faults.CmdResult(0, "3 restarting\n" if service in self.crash else "0 running\n", "")
        return faults.CmdResult(0, f"{self.memory_limit}\n", "")

    # -- the fake docker ------------------------------------------------------------------------
    def runner(self, args, timeout):
        self.commands.append(list(args))
        if args[:2] == ["docker", "inspect"]:
            return self._inspect(args)
        if "exec" in args:
            return self._exec(args)
        if "config" in args:
            return self._config(args)
        if "up" in args:
            return self._up(args)
        if "ps" in args and "-q" in args:
            service = args[-1]
            return faults.CmdResult(0, "" if service in self.removed else f"cid-{service}\n", "")
        if "ps" in args:
            rows = []
            for name in faults.M1_SERVICES:
                if name in self.removed:
                    continue
                if name in self.stopped:
                    rows.append(row(name, "exited", ""))
                elif name in self.crash:
                    rows.append(row(name, "restarting", ""))
                else:
                    rows.append(row(name))
            return faults.CmdResult(0, "\n".join(json.dumps(r) for r in rows), "")
        if "stop" in args:
            self.stopped.add(args[-1])
            self.procs[args[-1]] = 0                     # processes die with the container
        elif "start" in args:
            self.stopped.discard(args[-1])
            if self.on_start:
                self.on_start(args[-1])
        return faults.CmdResult(0, "", "")

    def docker(self, repo_root=None, echo=None):
        self.repo_root = Path(repo_root) if repo_root else CHAOS_DIR.parent
        clock = {"t": 0.0}
        return faults.Docker(runner=self.runner, echo=echo or (lambda _m: None),
                             sleep=lambda s: clock.update(t=clock["t"] + s),
                             monotonic=lambda: clock["t"], http=lambda _url: 200, repo_root=self.repo_root)


class FaultCommandTests(unittest.TestCase):
    def mutating(self, stack):
        return [c[-2:] for c in stack.commands if c[-2] in ("stop", "start")]

    def test_f10_inject_and_clear_command_sequence(self):
        stack = FakeStack()
        dk = stack.docker()
        faults.inject_f10(dk, {"target": "postgres"})
        self.assertEqual(stack.stopped, {"postgres"})
        faults.clear_f10(dk, {"target": "postgres"})
        self.assertEqual(stack.stopped, set())
        self.assertEqual(self.mutating(stack), [["stop", "postgres"], ["start", "postgres"]])

    def test_f13_inject_and_clear_command_sequence(self):
        stack = FakeStack()
        dk = stack.docker()
        faults.inject_f13(dk, {"target": "redis"})
        faults.clear_f13(dk, {"target": "redis"})
        self.assertEqual(self.mutating(stack), [["stop", "redis"], ["start", "redis"]])

    def test_commands_use_explicit_files_no_project_flag_no_down(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            for name in faults.BASE_COMPOSE_FILES:           # stand-ins for the real compose files
                (Path(tmp) / name).write_text("name: sentinelops\n", encoding="utf-8")
            dk = stack.docker()
            dk.repo_root = Path(tmp)
            faults.inject_f10(dk, {"target": "postgres"})
        for cmd in stack.commands:
            self.assertEqual(cmd[:6], ["docker", "compose", "-f", "docker-compose.yml",
                                       "-f", "docker-compose.override.yml"])
            self.assertNotIn("-p", cmd)
            self.assertNotIn("down", cmd)
            self.assertNotIn("-v", cmd)

    def test_wait_fails_with_clear_text_at_the_deadline(self):
        stack = FakeStack()
        stack.stopped.add("postgres")

        def start_that_does_nothing(args, timeout):
            stack.commands.append(list(args))
            if "ps" in args:
                return stack.runner(args, timeout)
            return faults.CmdResult(0, "", "")

        dk = stack.docker()
        dk.runner = start_that_does_nothing
        with self.assertRaisesRegex(faults.FaultError, r"timeout after 120 s.*postgres \(exited\)"):
            faults.clear_f10(dk, {"target": "postgres"})

    def test_heal_stopped_services_starts_only_stopped_ones(self):
        stack = FakeStack()
        stack.stopped.update({"redis", "orders"})
        started = faults.heal_stopped_services(stack.docker())
        self.assertEqual(started, ["redis", "orders"])
        self.assertEqual(stack.stopped, set())

    def test_wait_recovered_needs_http_200(self):
        stack = FakeStack()
        dk = stack.docker()
        dk.http = lambda _url: 503
        with self.assertRaisesRegex(faults.FaultError, "returned 503"):
            faults.wait_recovered(dk, timeout_s=6)
        dk.http = lambda _url: 200
        faults.wait_recovered(dk, timeout_s=6)


class EndToEndWithFakeDockerTests(unittest.TestCase):
    """The whole CLI flow (inject, status, second inject, clear) with a fake docker and temp files."""

    def make_ctx(self, tmp, stack):
        lines = []
        ctx = chaos.Context(catalogue=chaos.load_catalogue(), dk=stack.docker(), say=lines.append,
                            now=chaos.now_iso, sleep=lambda _s: None,
                            state_path=Path(tmp) / "active.json", truth_path=Path(tmp) / "truth.jsonl")
        return ctx, lines

    def events(self, tmp):
        return [json.loads(line) for line in (Path(tmp) / "truth.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_inject_status_refuse_second_inject_clear(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _lines = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["inject", "F10"], ctx), 0)
            self.assertEqual(chaos.read_state(ctx.state_path)["F10"]["inject_recorded"], True)
            self.assertEqual(chaos.main(["status"], ctx), 0)
            self.assertEqual(chaos.main(["inject", "F10"], ctx), 2)          # second inject refused
            self.assertEqual(chaos.main(["clear", "F10"], ctx), 0)
            self.assertEqual(chaos.read_state(ctx.state_path), {})
            self.assertEqual(chaos.main(["clear", "F10"], ctx), 0)           # idempotent
            events = self.events(tmp)
            self.assertEqual([e["event"] for e in events], ["inject", "clear", "recovered"])
            self.assertEqual(len({e["run_id"] for e in events}), 1)
            self.assertTrue(events[0]["ts"] <= events[1]["ts"] <= events[2]["ts"])
            self.assertIn("recovery_s", events[2])
            self.assertTrue(all("late" not in e for e in events))        # a normal clear is not late

    def test_stale_record_is_reported_and_cleared(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, lines = self.make_ctx(tmp, stack)
            chaos.main(["inject", "F13"], ctx)
            stack.stopped.clear()                                        # someone restarted redis by hand
            chaos.main(["status"], ctx)
            self.assertTrue(any("stale" in line for line in lines))
            self.assertEqual(chaos.main(["inject", "F13"], ctx), 2)      # stale record blocks a new inject
            self.assertEqual(chaos.main(["clear", "F13"], ctx), 0)
            events = self.events(tmp)
            self.assertEqual([e["event"] for e in events], ["inject", "clear", "recovered"])
            self.assertNotIn("late", events[0])
            self.assertIs(events[1]["late"], True)                       # the effect was already gone
            self.assertIs(events[2]["late"], True)

    def test_status_with_nothing_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, lines = self.make_ctx(tmp, FakeStack())
            self.assertEqual(chaos.main(["status"], ctx), 0)
            self.assertEqual(lines, ["no active faults"])

    def test_refusals_exit_with_2_and_write_nothing(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            for argv in (["inject", "F99"], ["inject", "F10", "--target", "redis"],
                         ["inject", "F10", "--duration", "9999"], ["inject", "F01", "--duration", "9999"],
                         ["inject", "F11"], ["inject", "F01", "--param", "processes=9"],
                         ["inject", "F02", "--param", "step_mib=0"], ["clear"], ["clear", "F10", "--all"]):
                with self.subTest(argv=argv):
                    self.assertEqual(chaos.main(argv, ctx), 2)
            self.assertFalse((Path(tmp) / "truth.jsonl").exists())
            self.assertEqual(stack.commands, [])                          # docker was never called

    def test_inject_is_refused_when_the_stack_is_not_healthy(self):
        stack = FakeStack()
        stack.stopped.add("orders")
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["inject", "F10"], ctx), 2)
            self.assertFalse((Path(tmp) / "truth.jsonl").exists())

    def test_clear_all_repairs_a_manually_stopped_service(self):
        stack = FakeStack()
        stack.stopped.add("payments")
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["clear", "--all"], ctx), 0)
            self.assertEqual(stack.stopped, set())

    def test_run_writes_inject_clear_recovered(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["run", "F10", "--warmup", "1", "--hold", "1", "--cooldown", "1"], ctx), 0)
            self.assertEqual([e["event"] for e in self.events(tmp)], ["inject", "clear", "recovered"])
            self.assertEqual(stack.stopped, set())

    def test_run_ctrl_c_during_hold_clears_and_marks_aborted(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            calls = {"n": 0}

            def sleep(_seconds):
                calls["n"] += 1
                if calls["n"] == 2:                    # 1st sleep = warm-up, 2nd = hold
                    raise KeyboardInterrupt

            ctx.sleep = sleep
            code = chaos.main(["run", "F13", "--warmup", "1", "--hold", "5", "--cooldown", "1"], ctx)
            self.assertEqual(code, 130)
            self.assertEqual(stack.stopped, set())
            self.assertEqual(chaos.read_state(ctx.state_path), {})
            self.assertEqual([e["event"] for e in self.events(tmp)], ["inject", "aborted", "clear", "recovered"])

    def test_no_password_in_any_written_line(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            ctx.mask = lambda text: chaos.mask_secrets(text, ["change_me_dev_only"])
            chaos.main(["inject", "F10"], ctx)
            chaos.main(["clear", "F10"], ctx)
            self.assertNotIn("change_me_dev_only", (Path(tmp) / "truth.jsonl").read_text(encoding="utf-8"))
            self.assertNotIn("change_me_dev_only", ctx.state_path.parent.joinpath("active.json").read_text(encoding="utf-8"))


class ListTests(unittest.TestCase):
    def test_list_shows_8_faults(self):
        lines = []
        ctx = chaos.Context(catalogue=chaos.load_catalogue(), dk=faults.Docker(), say=lines.append)
        self.assertEqual(chaos.cmd_list(ctx, None), 0)
        table = lines[0].splitlines()
        self.assertEqual(len(table), 2 + 8)
        for fid in EXPECTED_IDS:
            self.assertTrue(any(line.startswith(fid) for line in table[2:]), fid)
        self.assertTrue(re.search(r"F10 .*yes", table[2 + EXPECTED_IDS.index("F10")]))


class MaskingForGroundTruthTests(unittest.TestCase):
    """The ground-truth file masks password PATTERNS only, never plain words (CP2 request 1)."""

    def test_pattern_mask_does_not_touch_plain_words(self):
        self.assertEqual(chaos.mask_patterns("target postgres"), "target postgres")
        self.assertEqual(chaos.mask_patterns("postgresql://shop:abc123@postgres/shop"),
                         "postgresql://shop:***@postgres/shop")
        self.assertEqual(chaos.mask_patterns("POSTGRES_PASSWORD=abc"), "POSTGRES_PASSWORD=***")

    def test_value_mask_for_printed_output_still_replaces_the_word(self):
        self.assertEqual(chaos.mask_secrets("docker compose stop postgres", ["postgres"]),
                         "docker compose stop ***")

    def test_default_ground_truth_mask_keeps_a_secret_word_and_valid_json(self):
        cat = catalogue_dict()
        record = {"run_id": "F10-abcd1234", "fault_id": "F10", "target": "postgres",
                  "params": {"note": "POSTGRES_PASSWORD=abc and postgresql://u:pw123@postgres/db"}}
        event = chaos.make_event("inject", record, cat["F10"], "2026-10-11T10:00:00.000Z")
        parsed = json.loads(chaos.format_ground_truth_line(event))
        self.assertEqual(parsed["target"], "postgres")
        self.assertEqual(parsed["expected_root_cause"]["service"], "postgres")
        self.assertNotIn("abc and", parsed["params"]["note"].replace("PASSWORD=***", ""))
        self.assertNotIn("pw123", parsed["params"]["note"])

    def test_mask_structure_handles_nested_values(self):
        masked = chaos.mask_structure({"a": ["x1", {"b": "x1"}], "n": 5, "k": None}, lambda t: t.replace("x1", "#"))
        self.assertEqual(masked, {"a": ["#", {"b": "#"}], "n": 5, "k": None})

    def test_ground_truth_keeps_target_even_when_the_secret_is_the_word_postgres(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = chaos.Context(catalogue=chaos.load_catalogue(), dk=FakeStack().docker(), say=lambda _m: None,
                                sleep=lambda _s: None, state_path=Path(tmp) / "active.json",
                                truth_path=Path(tmp) / "truth.jsonl")
            ctx.mask = lambda text: chaos.mask_secrets(text, ["postgres"])       # printed output masks the word
            self.assertEqual(chaos.main(["inject", "F10"], ctx), 0)
            self.assertEqual(chaos.main(["clear", "F10"], ctx), 0)
            events = [json.loads(line) for line in (Path(tmp) / "truth.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 3)
            for event in events:
                self.assertEqual(event["target"], "postgres")
                self.assertEqual(event["expected_root_cause"]["service"], "postgres")


class ProcessHelperTests(unittest.TestCase):
    def test_python_command_has_no_quotes_or_newlines_and_round_trips(self):
        cmd = faults.python_command(faults.F01_CODE, "/tmp/x.stop", 180, "chaos-F01-proc")
        self.assertEqual(cmd[:3], ["python", "-c", faults.PY_WRAPPER])
        self.assertEqual(base64.b64decode(cmd[3]).decode(), faults.F01_CODE)
        self.assertEqual(cmd[4:], ["/tmp/x.stop", "180", "chaos-F01-proc"])
        self.assertTrue(all("\n" not in part and '"' not in part for part in cmd))

    def test_display_command_shortens_long_arguments_only(self):
        shown = faults.display_command(["docker", "compose", "exec", "x" * 500, "orders"])
        self.assertLess(len(shown), 100)
        self.assertIn("(500 chars)", shown)
        self.assertEqual(faults.display_command(["docker", "compose", "stop", "postgres"]),
                         "docker compose stop postgres")

    def test_the_inner_programs_are_valid_python(self):
        for name in ("PY_SCAN", "PY_TOUCH", "PY_REMOVE", "PY_READ", "F01_CODE", "F02_CODE"):
            compile(getattr(faults, name), name, "exec")

    def test_process_fault_markers_are_distinct_and_not_part_of_the_stop_file_path(self):
        for spec in faults.PROCESS_FAULTS.values():
            self.assertNotIn(spec.marker, spec.stop_file)
            self.assertNotIn(spec.marker, spec.status_file)
        self.assertNotEqual(faults.PROCESS_FAULTS["F01"].marker, faults.PROCESS_FAULTS["F02"].marker)


class ProcessFaultCommandTests(unittest.TestCase):
    def test_f01_inject_starts_n_detached_processes_with_marker_stop_file_and_duration(self):
        stack = FakeStack()
        faults.inject_f01(stack.docker(), {"target": "orders", "params": {"processes": 3}, "duration_s": 180})
        self.assertEqual(stack.procs["orders"], 3)
        self.assertEqual(len(stack.started), 3)
        for code, extra in stack.started:
            self.assertEqual(code, faults.F01_CODE)
            self.assertEqual(extra, ["/tmp/chaos-F01.stop", "180", "chaos-F01-proc"])
        detached = [c for c in stack.commands if "-d" in c]
        self.assertEqual(len(detached), 3)

    def test_f01_clear_creates_the_stop_file_waits_and_removes_files(self):
        stack = FakeStack()
        dk = stack.docker()
        faults.inject_f01(dk, {"target": "orders", "params": {"processes": 2}, "duration_s": 60})
        faults.clear_f01(dk, {"target": "orders"})
        self.assertEqual(stack.procs["orders"], 0)
        self.assertEqual(stack.files, set())                       # stop file and status file removed again

    def test_f01_inject_refuses_when_processes_are_left_over(self):
        stack = FakeStack()
        stack.procs["orders"] = 1
        with self.assertRaisesRegex(faults.FaultError, "already running"):
            faults.inject_f01(stack.docker(), {"target": "orders", "params": {"processes": 2}, "duration_s": 60})
        self.assertEqual(stack.started, [])

    def test_f01_inject_reports_when_exec_is_not_possible(self):
        stack = FakeStack()
        stack.exec_fails = True
        with self.assertRaisesRegex(faults.FaultError, "(?s)docker compose exec.*Stop here"):
            faults.inject_f01(stack.docker(), {"target": "orders", "params": {"processes": 2}, "duration_s": 60})

    def test_f01_clear_when_the_container_is_not_running_is_a_no_op(self):
        stack = FakeStack()
        stack.stopped.add("orders")
        messages = []
        dk = stack.docker()
        dk.echo = messages.append
        faults.clear_f01(dk, {"target": "orders"})
        self.assertTrue(any("not running" in m for m in messages))
        self.assertFalse(any("exec" in c for c in stack.commands))

    def test_f01_clear_times_out_with_clear_text_when_processes_do_not_end(self):
        stack = FakeStack()
        dk = stack.docker()
        faults.inject_f01(dk, {"target": "orders", "params": {"processes": 1}, "duration_s": 60})
        original = stack._exec

        def stubborn(args):
            result = original(args)
            stack.procs["orders"] = 1                              # the process ignores the stop file
            return result
        stack._exec = stubborn
        with self.assertRaisesRegex(faults.FaultError, "timeout after 30 s.*still running"):
            faults.clear_f01(dk, {"target": "orders"})

    def test_f02_ceiling_is_90_percent_of_the_limit_read_with_docker_inspect(self):
        stack = FakeStack()
        faults.inject_f02(stack.docker(), {"target": "payments", "duration_s": 240,
                                           "params": {"step_mib": 8, "interval_s": 2}})
        code, extra = stack.started[0]
        self.assertEqual(code, faults.F02_CODE)
        self.assertEqual(extra, ["/tmp/chaos-F02.stop", "240", "8", "2", str(int(268435456 * 0.9)),
                                 "/tmp/chaos-F02.status.json", "chaos-F02-proc"])
        self.assertTrue(any(c[:2] == ["docker", "inspect"] and "{{.HostConfig.Memory}}" in c for c in stack.commands))
        self.assertTrue(any(c[-2:] == ["-q", "payments"] for c in stack.commands))

    def test_f02_refuses_a_container_without_memory_limit(self):
        stack = FakeStack()
        stack.memory_limit = 0
        with self.assertRaisesRegex(faults.FaultError, "no memory limit"):
            faults.inject_f02(stack.docker(), {"target": "payments", "duration_s": 60,
                                               "params": {"step_mib": 8, "interval_s": 2}})
        self.assertEqual(stack.started, [])

    def test_f02_reports_an_allocator_error(self):
        stack = FakeStack()
        stack.f02_state = "error_no_cgroup"
        with self.assertRaisesRegex(faults.FaultError, "error_no_cgroup"):
            faults.inject_f02(stack.docker(), {"target": "payments", "duration_s": 60,
                                               "params": {"step_mib": 8, "interval_s": 2}})

    def test_f02_clear(self):
        stack = FakeStack()
        dk = stack.docker()
        faults.inject_f02(dk, {"target": "payments", "duration_s": 60, "params": {"step_mib": 8, "interval_s": 2}})
        faults.clear_f02(dk, {"target": "payments"})
        self.assertEqual(stack.procs["payments"], 0)
        self.assertEqual(stack.files, set())

    def test_process_counts_skips_unreadable_and_stopped_containers(self):
        stack = FakeStack()
        stack.procs["orders"] = 2
        dk = stack.docker()
        records = [{"fault_id": "F01", "target": "orders"}, {"fault_id": "F10", "target": "postgres"}]
        self.assertEqual(faults.process_counts(dk, records, {"orders", "postgres"}), {"F01": 2})
        self.assertEqual(faults.process_counts(dk, records, set()), {})
        stack.exec_fails = True
        self.assertEqual(faults.process_counts(dk, records, {"orders"}), {})

    def test_sweep_ends_leftover_processes_without_a_record(self):
        stack = FakeStack()
        stack.procs["orders"] = 2
        ended = faults.sweep_process_faults(stack.docker(), {"F01": ["orders"], "F02": ["payments"]})
        self.assertEqual(ended, [("F01", "orders")])
        self.assertEqual(stack.procs["orders"], 0)


class ProcessFaultFlowTests(unittest.TestCase):
    def make_ctx(self, tmp, stack):
        lines = []
        ctx = chaos.Context(catalogue=chaos.load_catalogue(), dk=stack.docker(), say=lines.append,
                            now=chaos.now_iso, sleep=lambda _s: None,
                            state_path=Path(tmp) / "active.json", truth_path=Path(tmp) / "truth.jsonl")
        return ctx, lines

    def events(self, tmp):
        return [json.loads(line) for line in (Path(tmp) / "truth.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_f01_inject_status_clear(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, lines = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["inject", "F01", "--duration", "90"], ctx), 0)
            self.assertEqual(stack.started[0][1][1], "90")
            self.assertEqual(chaos.main(["status"], ctx), 0)
            self.assertTrue(any(" active" in line for line in lines))
            self.assertEqual(chaos.main(["inject", "F01"], ctx), 2)           # same fault twice is refused
            self.assertEqual(chaos.main(["clear", "F01"], ctx), 0)
            events = self.events(tmp)
            self.assertEqual([e["event"] for e in events], ["inject", "clear", "recovered"])
            self.assertEqual(events[0]["expected_root_cause"], {"service": "orders", "type": "cpu_exhaustion"})
            self.assertEqual(events[0]["acceptable_actions"], ["restart_service", "scale_service"])
            self.assertTrue(all("late" not in e for e in events))

    def test_f01_whose_duration_is_over_is_stale_and_its_clear_is_late(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, lines = self.make_ctx(tmp, stack)
            chaos.main(["inject", "F01"], ctx)
            stack.procs["orders"] = 0                                         # the timer ended the processes
            chaos.main(["status"], ctx)
            self.assertTrue(any("stale" in line for line in lines))
            self.assertEqual(chaos.main(["inject", "F01"], ctx), 2)           # stale record blocks a new inject
            self.assertEqual(chaos.main(["clear", "F01"], ctx), 0)
            events = self.events(tmp)
            self.assertIs(events[1]["late"], True)
            self.assertIs(events[2]["late"], True)

    def test_f02_params_and_ground_truth(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["inject", "F02", "--param", "step_mib=4", "--param", "interval_s=1"], ctx), 0)
            self.assertEqual(stack.started[0][1][2:4], ["4", "1"])
            self.assertEqual(chaos.main(["clear", "F02"], ctx), 0)
            events = self.events(tmp)
            self.assertEqual(events[0]["params"], {"step_mib": 4, "interval_s": 1})
            self.assertEqual(events[0]["expected_root_cause"], {"service": "payments", "type": "memory_exhaustion"})

    def test_run_of_a_self_ending_fault_outlasts_the_hold_so_the_clear_is_not_late(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["run", "F01", "--warmup", "1", "--hold", "20", "--cooldown", "1"], ctx), 0)
            self.assertEqual(stack.started[0][1][1], str(20 + chaos.RUN_END_MARGIN_S))
            events = self.events(tmp)
            self.assertEqual([e["event"] for e in events], ["inject", "clear", "recovered"])
            self.assertTrue(all("late" not in e for e in events))

    def test_run_duration_is_capped_at_max_duration(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            chaos.main(["run", "F01", "--warmup", "0", "--hold", "590", "--cooldown", "0"], ctx)
            self.assertEqual(stack.started[0][1][1], "600")

    def test_clear_all_ends_leftover_processes_without_a_record(self):
        stack = FakeStack()
        stack.procs["payments"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            ctx, lines = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["clear", "--all"], ctx), 0)
            self.assertEqual(stack.procs["payments"], 0)
            self.assertTrue(any("leftover F02" in line for line in lines))

    def test_inject_failure_is_undone_and_nothing_is_recorded(self):
        stack = FakeStack()
        stack.memory_limit = 0                                                # F02 inject fails
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            self.assertEqual(chaos.main(["inject", "F02"], ctx), 1)
            self.assertEqual(chaos.read_state(ctx.state_path), {})
            self.assertFalse((Path(tmp) / "truth.jsonl").exists())


class SecondCtrlCTests(unittest.TestCase):
    """CP2 request 3: Ctrl+C while the fault is being cleared must not stop the tool before the events are written."""

    def make_ctx(self, tmp, stack):
        lines = []
        ctx = chaos.Context(catalogue=chaos.load_catalogue(), dk=stack.docker(), say=lines.append,
                            now=chaos.now_iso, sleep=lambda _s: None,
                            state_path=Path(tmp) / "active.json", truth_path=Path(tmp) / "truth.jsonl")
        return ctx, lines

    def events(self, tmp):
        return [json.loads(line) for line in (Path(tmp) / "truth.jsonl").read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def interrupt_once():
        fired = {"n": 0}

        def callback(_service):
            if fired["n"] == 0:
                fired["n"] += 1
                signal.raise_signal(signal.SIGINT)          # a Ctrl+C that arrives while the clear is running
        return callback

    def test_second_ctrl_c_during_the_clear_is_held_back(self):
        stack = FakeStack()
        before = signal.getsignal(signal.SIGINT)
        with tempfile.TemporaryDirectory() as tmp:
            ctx, lines = self.make_ctx(tmp, stack)
            calls = {"n": 0}

            def sleep(_seconds):
                calls["n"] += 1
                if calls["n"] == 2:                          # warm-up is the 1st sleep, hold the 2nd
                    raise KeyboardInterrupt                  # first Ctrl+C
            ctx.sleep = sleep
            stack.on_start = self.interrupt_once()           # second Ctrl+C, during `docker compose start`
            code = chaos.main(["run", "F13", "--warmup", "1", "--hold", "5", "--cooldown", "1"], ctx)
            self.assertEqual(code, 130)
            self.assertEqual([e["event"] for e in self.events(tmp)], ["inject", "aborted", "clear", "recovered"])
            self.assertEqual(chaos.read_state(ctx.state_path), {})
            self.assertEqual(stack.stopped, set())
            self.assertTrue(any("Ctrl+C received while clearing" in line for line in lines))
        self.assertIs(signal.getsignal(signal.SIGINT), before)      # the normal handler is back

    def test_ctrl_c_only_during_the_clear_still_records_everything_and_exits_130(self):
        stack = FakeStack()
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = self.make_ctx(tmp, stack)
            stack.on_start = self.interrupt_once()
            code = chaos.main(["run", "F13", "--warmup", "1", "--hold", "1", "--cooldown", "1"], ctx)
            self.assertEqual(code, 130)
            self.assertEqual([e["event"] for e in self.events(tmp)], ["inject", "clear", "recovered"])   # no 'aborted'
            self.assertEqual(stack.stopped, set())

    def test_the_guard_counts_and_restores_the_handler(self):
        before = signal.getsignal(signal.SIGINT)
        guard = chaos.InterruptGuard(lambda _m: None)
        with guard:
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
        self.assertEqual(guard.count, 2)
        self.assertIs(signal.getsignal(signal.SIGINT), before)


class ConfigFaultPureTests(unittest.TestCase):
    """F06/F08: generated override files, the checks on the rendered config, the stale decision."""

    def test_override_relpath_uses_forward_slashes(self):
        self.assertEqual(faults.override_relpath("F06", "inventory"), "chaos/overrides/F06-inventory.yml")

    def test_f06_override_text_removes_only_database_url_of_the_target(self):
        text = faults.override_text("F06", "inventory")
        self.assertIn("services:\n  inventory:\n    environment:\n      DATABASE_URL: !reset null\n", text)
        self.assertEqual(text.count("DATABASE_URL"), 1)
        self.assertNotIn("restart", text)                     # the base file already has restart: unless-stopped
        self.assertNotIn("\r", text)

    def test_f08_override_text_has_the_wrong_password_and_no_real_secret(self):
        text = faults.override_text("F08", "orders")
        self.assertIn("  orders:\n    environment:\n      DATABASE_URL: ", text)
        self.assertIn(":" + faults.WRONG_PASSWORD + "@postgres:5432/", text)
        self.assertIn("${POSTGRES_USER:-shop}", text)
        self.assertIn("${POSTGRES_DB:-shop}", text)
        self.assertNotIn("POSTGRES_PASSWORD", text)

    def test_override_text_for_other_faults_is_an_error(self):
        with self.assertRaises(ValueError):
            faults.override_text("F10", "postgres")

    def test_database_url_from_env(self):
        self.assertIsNone(faults.database_url_from_env(["A=1", "DATABASE_URL_OLD=x"]))
        self.assertEqual(faults.database_url_from_env(["DATABASE_URL="]), "")
        self.assertEqual(faults.database_url_from_env(["A=1", "DATABASE_URL=u"]), "u")

    def test_f06_check_needs_the_variable_to_be_absent_not_empty(self):
        faults.check_f06_rendered({"LOG_LEVEL": "INFO"})
        for environment in ({"DATABASE_URL": REAL_URL}, {"DATABASE_URL": ""}, {"DATABASE_URL": None}):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(faults.FaultError, "did not remove DATABASE_URL") as ctx:
                    faults.check_f06_rendered(environment)
                self.assertNotIn("realpw-secret", str(ctx.exception))

    def test_f08_check_accepts_a_password_only_change(self):
        faults.check_f08_rendered(REAL_URL, REAL_URL.replace("realpw-secret", faults.WRONG_PASSWORD))

    def test_f08_check_rejects_everything_else_without_printing_values(self):
        wrong = REAL_URL.replace("realpw-secret", faults.WRONG_PASSWORD)
        cases = [
            (REAL_URL, REAL_URL, "wrong password"),                                     # nothing changed
            (REAL_URL, wrong.replace("@postgres:", "@elsewhere:"), "more than the password"),
            (REAL_URL, wrong.replace("shop:", "admin:", 1), "more than the password"),
            (REAL_URL, wrong.replace("/shop", "/other"), "more than the password"),
            (wrong, wrong, "not in its base state"),
            (None, wrong, "missing"),
            (REAL_URL, None, "missing"),
        ]
        for base, fault, word in cases:
            with self.subTest(word=word):
                with self.assertRaisesRegex(faults.FaultError, word) as ctx:
                    faults.check_f08_rendered(base, fault)
                self.assertNotIn("realpw-secret", str(ctx.exception))

    def test_effect_gone_for_config_faults(self):
        running = {"inventory"}
        self.assertFalse(faults.effect_gone("F06", "inventory", running, None, {"F06": "faulted"}))
        self.assertTrue(faults.effect_gone("F06", "inventory", running, None, {"F06": "clean"}))
        self.assertTrue(faults.effect_gone("F08", "orders", running, None, {"F08": "missing"}))
        self.assertFalse(faults.effect_gone("F08", "orders", running, None, {}))        # could not read: unknown
        self.assertFalse(faults.effect_gone("F08", "orders", running, None, None))
        self.assertFalse(faults.effect_gone("F06", "inventory", set(), None, {"F06": "faulted"}))   # a crash loop is not "stopped"

    def test_config_state_from_the_container_environment(self):
        stack = FakeStack()
        dk = stack.docker()
        self.assertEqual(faults.config_state(dk, "F06", "inventory"), "clean")
        self.assertEqual(faults.config_state(dk, "F08", "inventory"), "clean")
        stack.env["inventory"] = [e for e in stack.env["inventory"] if not e.startswith("DATABASE_URL=")]
        self.assertEqual(faults.config_state(dk, "F06", "inventory"), "faulted")
        stack.env["orders"] = ["DATABASE_URL=postgresql://shop:" + faults.WRONG_PASSWORD + "@postgres:5432/shop"]
        self.assertEqual(faults.config_state(dk, "F08", "orders"), "faulted")
        stack.removed.add("payments")
        self.assertEqual(faults.config_state(dk, "F06", "payments"), "missing")

    def test_config_states_skips_unreadable_containers(self):
        stack = FakeStack()
        dk = stack.docker()
        original = dk.runner

        def broken_inspect(args, timeout):
            if args[:2] == ["docker", "inspect"]:
                return faults.CmdResult(1, "", "boom")
            return original(args, timeout)
        dk.runner = broken_inspect
        records = [{"fault_id": "F06", "target": "inventory"}, {"fault_id": "F10", "target": "postgres"}]
        self.assertEqual(faults.config_states(dk, records), {})


class ConfigFaultBase(unittest.TestCase):
    """A fake repo folder (with the two compose files), the fake stack, and ONE ordered output log."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for name in faults.BASE_COMPOSE_FILES:
            (self.repo / name).write_text("name: sentinelops\n", encoding="utf-8")
        self.stack = FakeStack()
        self.out = []                              # commands and messages, in the order the user would see them
        self.ctx = chaos.Context(catalogue=chaos.load_catalogue(),
                                 dk=self.stack.docker(repo_root=self.repo, echo=self.out.append),
                                 say=self.out.append, sleep=lambda _s: None,
                                 state_path=self.root / "active.json", truth_path=self.root / "truth.jsonl")

    def cli(self, *argv):
        return chaos.main(list(argv), self.ctx)

    def events(self):
        return [json.loads(line) for line in (self.root / "truth.jsonl").read_text(encoding="utf-8").splitlines()]

    def shown(self):
        """The printed output with run ids replaced, for exact comparisons."""
        return [re.sub(r"F0[68]-[0-9a-f]{8}", "F0X-<id>", re.sub(r"recovered \d+\.\d s", "recovered <t> s", line))
                for line in self.out]

    def override_file(self, fault_id, target):
        return self.repo / "chaos" / "overrides" / f"{fault_id}-{target}.yml"

    def base_files(self):
        return "-f docker-compose.yml -f docker-compose.override.yml"


class ConfigFaultCommandTests(ConfigFaultBase):
    def mutating(self):
        return [c for c in self.stack.commands if "up" in c]

    def test_f06_inject_prints_exactly_these_lines(self):
        self.assertEqual(self.cli("inject", "F06"), 0)
        fault_file = f"{self.base_files()} -f chaos/overrides/F06-inventory.yml"
        self.assertEqual(self.shown(), [
            f"+ docker compose {fault_file} config --format json",
            "verified: DATABASE_URL is absent from the rendered config of inventory",
            f"+ docker compose {fault_file} up -d --no-deps --force-recreate inventory",
            "verified: DATABASE_URL is absent in the new inventory container",
            "verified: inventory is in a crash loop (restart count >= 2)",
            "injected F06 on inventory (run id F0X-<id>)",
            "it stays until: python chaos/chaos.py clear F06",
        ])
        self.assertEqual(self.override_file("F06", "inventory").read_text(encoding="utf-8"),
                         faults.override_text("F06", "inventory"))
        self.assertIn("inventory", self.stack.crash)
        self.assertEqual(self.stack.env["orders"], self.stack.base_env["orders"])      # other services untouched

    def test_f06_clear_prints_exactly_these_lines_and_recreates_from_the_base_files(self):
        self.cli("inject", "F06")
        del self.out[:]
        self.assertEqual(self.cli("clear", "F06"), 0)
        self.assertEqual(self.shown(), [
            f"+ docker compose {self.base_files()} up -d --no-deps --force-recreate inventory",
            "cleared F06 (run id F0X-<id>)",
            "waiting for all 6 containers to be healthy and the gateway to answer 200 ...",
            "F06: recovered <t> s after the clear",
        ])
        self.assertEqual(self.stack.env["inventory"], self.stack.base_env["inventory"])
        self.assertNotIn("inventory", self.stack.crash)
        self.assertFalse(self.override_file("F06", "inventory").exists())
        last_up = self.mutating()[-1]
        self.assertNotIn("chaos/overrides/F06-inventory.yml", last_up)               # base files only
        self.assertIn("--force-recreate", last_up)                                    # recreate, not restart
        self.assertFalse(any("restart" in c for c in self.stack.commands))

    def test_f06_on_another_target_uses_its_own_file_name(self):
        self.assertEqual(self.cli("inject", "F06", "--target", "payments"), 0)
        self.assertTrue(self.override_file("F06", "payments").exists())
        self.assertFalse(self.override_file("F06", "inventory").exists())
        self.assertIn("payments", self.stack.crash)

    def test_f06_is_refused_when_the_reset_tag_does_not_remove_the_variable(self):
        self.stack.reset_works = False
        self.assertEqual(self.cli("inject", "F06"), 1)
        self.assertFalse(any("up" in c and "chaos/overrides/F06-inventory.yml" in c
                             for c in self.stack.commands))                           # the fault was never applied
        self.assertTrue(any("Stop and report" in line for line in self.out))
        self.assertEqual(chaos.read_state(self.ctx.state_path), {})
        self.assertFalse((self.root / "truth.jsonl").exists())
        self.assertFalse(self.override_file("F06", "inventory").exists())              # rolled back

    def test_f06_clear_works_when_the_generated_file_is_already_gone(self):
        self.cli("inject", "F06")
        self.override_file("F06", "inventory").unlink()
        self.assertEqual(self.cli("status"), 0)
        self.assertTrue(any(line.rstrip().endswith("active") for line in self.out))    # the container is still faulted
        self.assertEqual(self.cli("clear", "F06"), 0)
        events = self.events()
        self.assertTrue(all("late" not in e for e in events))
        self.assertEqual(self.stack.env["inventory"], self.stack.base_env["inventory"])

    def test_f08_inject_prints_exactly_these_lines(self):
        self.assertEqual(self.cli("inject", "F08"), 0)
        fault_file = f"{self.base_files()} -f chaos/overrides/F08-orders.yml"
        self.assertEqual(self.shown(), [
            f"+ docker compose {self.base_files()} config --format json",
            f"+ docker compose {fault_file} config --format json",
            "verified: the rendered DATABASE_URL of orders differs from the base one only in the password",
            f"+ docker compose {fault_file} up -d --no-deps --force-recreate orders",
            "verified: orders is running with the wrong database password",
            "injected F08 on orders (run id F0X-<id>)",
            "it stays until: python chaos/chaos.py clear F08",
        ])
        url = faults.database_url_from_env(self.stack.env["orders"])
        self.assertIn(faults.WRONG_PASSWORD, url)
        self.assertNotIn("orders", self.stack.crash)                                     # F08 does not crash the service
        self.assertEqual(self.stack.env["payments"], self.stack.base_env["payments"])

    def test_f08_clear_prints_exactly_these_lines(self):
        self.cli("inject", "F08")
        del self.out[:]
        self.assertEqual(self.cli("clear", "F08"), 0)
        self.assertEqual(self.shown(), [
            f"+ docker compose {self.base_files()} up -d --no-deps --force-recreate orders",
            "cleared F08 (run id F0X-<id>)",
            "waiting for all 6 containers to be healthy and the gateway to answer 200 ...",
            "F08: recovered <t> s after the clear",
        ])
        self.assertEqual(self.stack.env["orders"], self.stack.base_env["orders"])
        self.assertFalse(self.override_file("F08", "orders").exists())

    def test_the_real_password_never_appears_anywhere(self):
        self.cli("inject", "F08")
        self.cli("status")
        self.cli("clear", "F08")
        self.cli("run", "F06", "--warmup", "0", "--hold", "1", "--cooldown", "0")
        everything = "\n".join(self.out) + (self.root / "truth.jsonl").read_text(encoding="utf-8") \
            + (self.root / "active.json").read_text(encoding="utf-8")
        for path in (self.repo / "chaos" / "overrides").glob("*"):
            everything += path.read_text(encoding="utf-8")
        self.assertNotIn("realpw-secret", everything)
        self.assertNotIn(REAL_URL, everything)

    def test_commands_never_use_a_project_flag_and_always_list_the_base_files_first(self):
        self.cli("inject", "F08")
        self.cli("clear", "F08")
        for cmd in self.stack.commands:
            if cmd[:2] == ["docker", "compose"]:
                self.assertNotIn("-p", cmd)
                self.assertEqual(cmd[2:6], ["-f", "docker-compose.yml", "-f", "docker-compose.override.yml"])

    def test_sweep_recreates_only_the_faulted_services_and_deletes_generated_files(self):
        self.cli("inject", "F06")                  # inventory faulted (F06)
        self.stack.env["orders"] = self.stack._fault_env("orders", [])
        self.stack.env["orders"] = [e for e in self.stack.env["orders"] if not e.startswith("DATABASE_URL=")] \
            + ["DATABASE_URL=postgresql://shop:" + faults.WRONG_PASSWORD + "@postgres:5432/shop"]   # orders faulted (F08)
        del self.stack.commands[:]
        done = faults.sweep_config_faults(self.ctx.dk, ["orders", "payments", "inventory"])
        self.assertEqual(done, [("F08", "orders"), ("F06", "inventory")])
        recreated = [c[-1] for c in self.stack.commands if "up" in c]
        self.assertEqual(recreated, ["orders", "inventory"])                              # payments was clean
        self.assertEqual(self.stack.env, self.stack.base_env)
        self.assertEqual(list((self.repo / "chaos" / "overrides").glob("*.yml")), [])

    def test_sweep_ignores_a_service_without_container(self):
        self.stack.removed.add("payments")
        self.assertEqual(faults.sweep_config_faults(self.ctx.dk, ["payments"]), [])
        self.assertFalse(any("up" in c for c in self.stack.commands))


class ConfigFaultFlowTests(ConfigFaultBase):
    """Status, stale detection and the late flag for F06/F08 (CP3 request 1), all with the fake docker."""

    def recreate_by_hand(self, service):
        self.stack.env[service] = list(self.stack.base_env[service])
        self.stack.crash.discard(service)

    def test_f06_normal_cycle_is_not_late(self):
        self.assertEqual(self.cli("inject", "F06"), 0)
        self.assertEqual(self.cli("inject", "F06"), 2)                                    # second inject refused
        self.assertEqual(self.cli("clear", "F06"), 0)
        events = self.events()
        self.assertEqual([e["event"] for e in events], ["inject", "clear", "recovered"])
        self.assertTrue(all("late" not in e for e in events))
        self.assertEqual(events[0]["expected_root_cause"], {"service": "inventory", "type": "misconfiguration"})
        self.assertEqual(events[0]["expected_action"], "revert_config")

    def test_f06_recreated_by_hand_is_stale_and_the_clear_is_late(self):
        self.cli("inject", "F06")
        self.recreate_by_hand("inventory")
        del self.out[:]
        self.assertEqual(self.cli("status"), 0)
        self.assertTrue(any(line.rstrip().endswith("stale") for line in self.out))
        self.assertTrue(any(line.startswith("stale = the effect is gone") for line in self.out))
        self.assertEqual(self.cli("inject", "F06"), 2)                                    # a stale record blocks a new inject
        self.assertTrue(any("stale" in line for line in self.out))
        self.assertEqual(self.cli("clear", "F06"), 0)
        events = self.events()
        self.assertIs(events[1]["late"], True)
        self.assertIs(events[2]["late"], True)
        self.assertTrue(any("(late clear: the effect was already gone)" in line for line in self.out))

    def test_f06_service_removed_is_stale_too(self):
        self.cli("inject", "F06")
        self.stack.removed.add("inventory")            # the container is gone; `clear` will create a clean one
        self.stack.crash.discard("inventory")
        self.cli("status")
        self.assertTrue(any(line.rstrip().endswith("stale") for line in self.out))
        self.assertEqual(self.cli("clear", "F06"), 0)
        self.assertIs(self.events()[1]["late"], True)

    def test_f06_plain_restart_does_not_make_it_stale(self):
        self.cli("inject", "F06")
        self.stack.stopped.add("inventory")            # someone stops and starts it: the environment is the same
        self.stack.stopped.discard("inventory")
        self.cli("status")
        self.assertTrue(any(line.rstrip().endswith("active") for line in self.out))

    def test_f08_normal_cycle_is_not_late(self):
        self.assertEqual(self.cli("inject", "F08", "--target", "payments"), 0)
        self.assertEqual(self.cli("status"), 0)
        self.assertTrue(any(line.rstrip().endswith("active") for line in self.out))
        self.assertEqual(self.cli("clear", "F08"), 0)
        events = self.events()
        self.assertTrue(all("late" not in e for e in events))
        self.assertEqual(events[0]["target"], "payments")
        self.assertEqual(events[0]["expected_root_cause"], {"service": "payments", "type": "db_auth_failure"})

    def test_f08_recreated_by_hand_is_stale_and_the_clear_is_late(self):
        self.cli("inject", "F08")
        self.recreate_by_hand("orders")
        del self.out[:]
        self.cli("status")
        self.assertTrue(any(line.rstrip().endswith("stale") for line in self.out))
        self.assertEqual(self.cli("inject", "F08"), 2)
        self.assertEqual(self.cli("clear", "F08"), 0)
        events = self.events()
        self.assertEqual([e["event"] for e in events], ["inject", "clear", "recovered"])
        self.assertIs(events[1]["late"], True)
        self.assertIs(events[2]["late"], True)

    def test_f08_after_the_override_file_was_removed_and_the_service_recreated(self):
        self.cli("inject", "F08")
        self.override_file("F08", "orders").unlink()
        self.recreate_by_hand("orders")                # what `docker compose up -d orders` would do without the file
        self.assertEqual(self.cli("clear", "F08"), 0)
        self.assertIs(self.events()[1]["late"], True)

    def test_run_of_f06_and_f08_writes_inject_clear_recovered_without_late(self):
        self.assertEqual(self.cli("run", "F06", "--warmup", "1", "--hold", "5", "--cooldown", "1"), 0)
        self.assertEqual(self.cli("run", "F08", "--warmup", "1", "--hold", "5", "--cooldown", "1"), 0)
        events = self.events()
        self.assertEqual([e["event"] for e in events], ["inject", "clear", "recovered"] * 2)
        self.assertTrue(all("late" not in e for e in events))
        self.assertEqual(self.stack.env, self.stack.base_env)

    def test_duration_is_refused_for_f06_and_f08(self):
        self.assertEqual(self.cli("inject", "F06", "--duration", "30"), 2)
        self.assertEqual(self.cli("inject", "F08", "--target", "gateway"), 2)
        self.assertEqual(self.stack.commands, [])

    def test_clear_all_repairs_a_faulted_service_without_any_record(self):
        self.cli("inject", "F06")
        self.ctx.state_path.unlink()                   # the record is lost
        del self.out[:]
        self.assertEqual(self.cli("clear", "--all"), 0)
        self.assertIn("recreated inventory from the base files (it still had the F06 fault)", self.out)
        self.assertEqual(self.stack.env, self.stack.base_env)
        self.assertNotIn("inventory", self.stack.crash)
        self.assertEqual(list((self.repo / "chaos" / "overrides").glob("*.yml")), [])
        self.assertIn("no active faults; the stack is healthy", self.out)

    def test_clear_all_with_a_record_recreates_the_service_only_once(self):
        self.cli("inject", "F08")
        del self.stack.commands[:]
        self.assertEqual(self.cli("clear", "--all"), 0)
        recreations = [c for c in self.stack.commands if "--force-recreate" in c]
        self.assertEqual(len(recreations), 1)
        self.assertEqual(self.events()[-2]["event"], "clear")
        self.assertNotIn("late", self.events()[-2])

    def test_clear_all_after_ctrl_c_during_run_of_f06(self):
        calls = {"n": 0}

        def sleep(_seconds):
            calls["n"] += 1
            if calls["n"] == 2:                        # warm-up is the 1st sleep, hold the 2nd
                raise KeyboardInterrupt
        self.ctx.sleep = sleep
        self.assertEqual(self.cli("run", "F06", "--warmup", "1", "--hold", "30", "--cooldown", "1"), 130)
        self.assertEqual([e["event"] for e in self.events()], ["inject", "aborted", "clear", "recovered"])
        self.assertEqual(self.stack.env, self.stack.base_env)
        self.assertEqual(self.cli("clear", "--all"), 0)

    def test_inject_failure_in_the_middle_is_rolled_back(self):
        original = self.stack._up

        def failing_up(args):
            result = original(args)
            if any(a.startswith("chaos/overrides/") for a in args):
                return faults.CmdResult(1, "", "boom while recreating")
            return result
        self.stack._up = failing_up
        self.assertEqual(self.cli("inject", "F08"), 1)
        self.assertEqual(chaos.read_state(self.ctx.state_path), {})
        self.assertFalse((self.root / "truth.jsonl").exists())
        self.assertEqual(self.stack.env, self.stack.base_env)
        self.assertFalse(self.override_file("F08", "orders").exists())


if __name__ == "__main__":
    unittest.main()
