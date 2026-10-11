"""Unit tests for the pure logic of the chaos tool (module M3).

Run from the repo root (host Python, standard library only):
    python -m unittest discover -s chaos/tests

No real docker call happens here. A few tests use a fake command runner to check the
command sequences of F10 and F13.
"""

import copy
import json
import re
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
        with self.assertRaisesRegex(chaos.ValidationError, "not built yet.*CP2"):
            self.validate("F01")

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
        self.assertFalse(faults.effect_gone("F01", "orders", {"orders"}))


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
class FakeStack:
    """Pretends to be docker: remembers which services are stopped and records every command."""

    def __init__(self):
        self.stopped = set()
        self.commands = []

    def runner(self, args, timeout):
        self.commands.append(list(args))
        if "ps" in args:
            rows = [row(n, "exited", "") if n in self.stopped else row(n) for n in faults.M1_SERVICES]
            return faults.CmdResult(0, "\n".join(json.dumps(r) for r in rows), "")
        if "stop" in args:
            self.stopped.add(args[-1])
        elif "start" in args:
            self.stopped.discard(args[-1])
        return faults.CmdResult(0, "", "")

    def docker(self):
        clock = {"t": 0.0}
        return faults.Docker(runner=self.runner, echo=lambda _m: None, sleep=lambda s: clock.update(t=clock["t"] + s),
                             monotonic=lambda: clock["t"], http=lambda _url: 200, repo_root=CHAOS_DIR.parent)


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
            self.assertEqual([e["event"] for e in self.events(tmp)], ["inject", "clear", "recovered"])

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
                         ["inject", "F01"], ["clear"], ["clear", "F10", "--all"]):
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


if __name__ == "__main__":
    unittest.main()
