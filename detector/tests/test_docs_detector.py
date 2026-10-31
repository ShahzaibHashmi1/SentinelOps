"""docs/DETECTOR.md must agree with the code and with rules.json (CP5 step 2).

Standard library only, no network, no Docker, no temporary files: the documentation, rules.json,
api.py and metrics.py are read with Path.read_text (the file is closed when the call returns), so the
test also passes on Windows with Python 3.14.

What is checked:
  * every rule id of rules.json, every route of api.py and every metric name of metrics.py appears in the doc
  * the rules table and the provenance table show the values of rules.json (name, condition, hold,
    severity, role, opens_incident, baseline_max, fault_value_min, margin_ratio)
  * the container counts (6, 11, 12), the profile flags and the start and reload commands are written down
  * the two real incidents (INC-0003 from F10, INC-0010 from F18) are named, and their JSON examples have
    exactly the fields of the incident format (taken from model.py) and the documented shape
  * every incident and symptom field is described, and every tier 1 fault has a row in the measured table

The helper functions at the top are tested on small strings, so a helper that never finds anything
cannot make the whole file pass by accident.
"""
import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401
from _bootstrap import DETECTOR_DIR, REPO_DIR, RULES_JSON
from app import api as api_module
from app.model import INCIDENT_STATUSES, SCHEMA_VERSION, Incident, Symptom

DOC_PATH = REPO_DIR / "docs" / "DETECTOR.md"
METRICS_PY = DETECTOR_DIR / "app" / "metrics.py"

TIER1_FAULTS = ("F01", "F02", "F06", "F08", "F10", "F11", "F13", "F18")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

START_COMMANDS = (
    "docker compose up -d --wait",
    "docker compose --profile observability up -d --build --wait",
    "docker compose --profile observability --profile detector up -d --build --wait",
    "docker compose --profile detector up -d --build --wait detector",
    "docker compose --profile observability kill -s SIGHUP prometheus",
)
PROFILE_FLAGS = ("--profile observability", "--profile detector")
COUNT_PHRASES = ("6 containers", "11 containers", "12 containers")
REAL_INCIDENTS = ("INC-0003", "INC-0010", "F10_INC_0003", "F18_INC_0010")


# ---------------------------------------------------------------------- helpers

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def has_word(doc: str, word: str) -> bool:
    """The word as a whole token (R01 does not match R010, detector_rule_state does not match a longer name)."""
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", doc) is not None


def has_route(doc: str, route: str) -> bool:
    """The route as a path of its own: not the tail of a longer path ('/ready' inside '/-/ready') and not the
    head of a longer path ('/internal/v1/incidents' inside '/internal/v1/incidents/<id>')."""
    pattern = r"(?<![A-Za-z0-9_/.\-])" + re.escape(route) + r"(?![A-Za-z0-9_/<])"
    return re.search(pattern, doc) is not None


def missing(doc: str, words, matcher=has_word) -> list:
    return [w for w in words if not matcher(doc, w)]


def table_rows(doc: str, first_cell: str) -> list:
    """Cells (stripped) of every markdown table row whose first cell is `first_cell`."""
    rows = []
    for line in doc.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == first_cell:
            rows.append(cells)
    return rows


def json_blocks(doc: str) -> list:
    return re.findall(r"```json\n(.*?)\n```", doc, flags=re.DOTALL)


def metric_names(source: str) -> list:
    return re.findall(r'block\(\s*"(detector_[a-z_]+)"', source)


def num(value) -> str:
    """How the doc writes a number of rules.json: 44, 0.2107, 1.932; None is 'null'."""
    return "null" if value is None else f"{value:g}"


def reference_keys():
    """Field names (in order) of the incident format, taken from model.py."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    incident = Incident(seq=1, status="open", opened_at=now, updated_at=now)
    symptom = Symptom(seq=1, rule_id="R01", detector="threshold", source="prometheus", service=None,
                      role="root_signal", severity="high", opens_incident=True, first_seen=now, last_seen=now,
                      active=True, flaps=0, comparator="==", threshold=0.0, peak_value=0.0, last_value=0.0,
                      detail="")
    return list(incident.to_api()), list(symptom.to_api())


class DocsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = read_text(DOC_PATH)
        cls.rules = json.loads(read_text(RULES_JSON))["rules"]


# ------------------------------------------------------------------ helper tests

class HelperTests(unittest.TestCase):
    def test_has_word_needs_a_whole_token(self):
        self.assertTrue(has_word("see R01, R02.", "R01"))
        self.assertFalse(has_word("see R010", "R01"))
        self.assertFalse(has_word("see XR01", "R01"))
        self.assertFalse(has_word("detector_incidents_total", "detector_incidents"))

    def test_missing_lists_every_absent_word(self):
        self.assertEqual(missing("R01 and R03", ["R01", "R02", "R03", "R04"]), ["R02", "R04"])
        self.assertEqual(missing("nothing", []), [])

    def test_has_route_ignores_longer_paths(self):
        self.assertTrue(has_route("`GET /ready` answers", "/ready"))
        self.assertFalse(has_route("readiness path `/-/ready` only", "/ready"))
        self.assertFalse(has_route("http://127.0.0.1:9100/health", "/health"))
        self.assertTrue(has_route("`GET /internal/v1/incidents?status=open`", "/internal/v1/incidents"))
        self.assertFalse(has_route("`GET /internal/v1/incidents/<incident_id>`", "/internal/v1/incidents"))
        self.assertFalse(has_route("/internal/v1/incidents/INC-0003", "/internal/v1/incidents"))

    def test_table_rows_split_cells(self):
        text = "| Id | Name |\n|---|---|\n| R01 | target_down |\nplain R01 line\n| R01 | second |\n"
        self.assertEqual(table_rows(text, "R01"), [["R01", "target_down"], ["R01", "second"]])
        self.assertEqual(table_rows(text, "R02"), [])

    def test_json_blocks_and_number_format(self):
        text = "a\n```json\n{\"x\": 1}\n```\nb\n```powershell\nls\n```\n"
        self.assertEqual(json_blocks(text), ['{"x": 1}'])
        self.assertEqual([num(44), num(0.2107), num(1.932), num(None), num(0)], ["44", "0.2107", "1.932", "null", "0"])

    def test_the_sources_of_the_checks_are_found(self):
        names = metric_names(read_text(METRICS_PY))
        self.assertGreaterEqual(len(names), 9)
        self.assertIn("detector_incidents_total", names)
        self.assertGreaterEqual(len(api_module.ROUTES), 6)
        self.assertTrue(api_module.INCIDENT_PREFIX.endswith("/"))
        self.assertGreaterEqual(len(json.loads(read_text(RULES_JSON))["rules"]), 9)

    def test_reference_keys_are_the_fixed_format(self):
        incident_keys, symptom_keys = reference_keys()
        self.assertEqual(incident_keys[:3], ["incident_id", "schema_version", "status"])
        self.assertEqual(incident_keys[-2:], ["symptoms", "data_gaps"])
        self.assertEqual(symptom_keys[:2], ["symptom_id", "rule_id"])
        self.assertEqual(len(incident_keys), 14)
        self.assertEqual(len(symptom_keys), 17)


# --------------------------------------------------------------- the doc itself

class DetectorDocTests(DocsTestCase):
    def test_doc_exists_and_is_not_empty(self):
        self.assertTrue(DOC_PATH.is_file(), f"{DOC_PATH} is missing")
        self.assertGreater(len(self.doc), 5000)

    def test_every_rule_id_is_in_the_doc(self):
        ids = [rule["id"] for rule in self.rules]
        self.assertEqual(missing(self.doc, ids), [], "rule ids of rules.json missing from docs/DETECTOR.md")

    def test_every_api_route_is_in_the_doc(self):
        routes = sorted(api_module.ROUTES) + [api_module.INCIDENT_PREFIX + "<incident_id>"]
        self.assertEqual(missing(self.doc, routes, has_route), [],
                         "API routes of api.py missing from docs/DETECTOR.md")

    def test_every_metric_name_is_in_the_doc(self):
        names = metric_names(read_text(METRICS_PY))
        self.assertEqual(missing(self.doc, names), [], "metric names of metrics.py missing from docs/DETECTOR.md")

    def test_container_counts_are_documented(self):
        self.assertEqual([p for p in COUNT_PHRASES if p not in self.doc], [])

    def test_profile_flags_and_start_commands_are_documented(self):
        self.assertEqual([f for f in PROFILE_FLAGS if f not in self.doc], [])
        self.assertEqual([c for c in START_COMMANDS if c not in self.doc], [])

    def test_real_incident_ids_are_named(self):
        self.assertEqual([i for i in REAL_INCIDENTS if i not in self.doc], [])

    def test_every_tier1_fault_has_a_measured_row(self):
        for fault in TIER1_FAULTS:
            rows = table_rows(self.doc, fault)
            self.assertTrue(rows, f"no row for {fault} in the measured table")
            self.assertTrue(any(len(r) == 7 for r in rows), f"the row of {fault} has no 7 columns")

    def test_rules_table_matches_rules_json(self):
        for rule in self.rules:
            rows = [r for r in table_rows(self.doc, rule["id"]) if len(r) == 10]
            self.assertEqual(len(rows), 1, f"{rule['id']}: expected one row in the rules table")
            _, name, source, _signal, condition, hold, severity, role, opens, _guard = rows[0]
            where = rule["id"]
            self.assertEqual(name, rule["name"], where)
            self.assertEqual(source, rule["source"], where)
            self.assertIn(f"{rule['comparator']} {num(rule['threshold'])}", condition, where)
            self.assertEqual(hold, f"{num(rule['for_s'])} s", where)
            self.assertEqual(severity, rule["severity"], where)
            self.assertEqual(role, rule["role"], where)
            self.assertIn("yes" if rule["opens_incident"] else "no", opens, where)

    def test_provenance_table_matches_rules_json(self):
        for rule in self.rules:
            prov = rule["provenance"]
            rows = [r for r in table_rows(self.doc, rule["id"]) if len(r) == 6]
            self.assertEqual(len(rows), 1, f"{rule['id']}: expected one row in the provenance table")
            _, threshold, baseline, fault, margin, _source = rows[0]
            where = rule["id"]
            if rule["comparator"] == "==":
                self.assertIn(f"== {num(rule['threshold'])}", threshold, where)
            else:
                self.assertEqual(threshold, num(rule["threshold"]), where)
            self.assertEqual(baseline, num(prov["baseline_max"]), where)
            self.assertEqual(fault, num(prov["fault_value_min"]), where)
            self.assertTrue(margin.startswith(num(prov["margin_ratio"])), f"{where}: margin_ratio {margin!r}")


class IncidentExampleTests(DocsTestCase):
    def examples(self) -> dict:
        found = {}
        for text in json_blocks(self.doc):
            data = json.loads(text)
            if isinstance(data, dict) and "incident_id" in data:
                found[data["incident_id"]] = data
        return found

    def test_two_examples_are_present_and_parse(self):
        self.assertEqual(sorted(self.examples()), ["INC-0003", "INC-0010"])

    def test_examples_have_exactly_the_fields_of_the_incident_format(self):
        incident_keys, symptom_keys = reference_keys()
        for incident_id, data in self.examples().items():
            self.assertEqual(list(data), incident_keys, incident_id)
            self.assertEqual(data["schema_version"], SCHEMA_VERSION, incident_id)
            self.assertIn(data["status"], INCIDENT_STATUSES, incident_id)
            for key in ("opened_at", "updated_at", "resolved_at"):
                self.assertRegex(data[key], TIMESTAMP, f"{incident_id} {key}")
            self.assertTrue(data["symptoms"], incident_id)
            for symptom in data["symptoms"]:
                self.assertEqual(list(symptom), symptom_keys, f"{incident_id} {symptom.get('symptom_id')}")
                self.assertRegex(symptom["first_seen"], TIMESTAMP)
                self.assertRegex(symptom["last_seen"], TIMESTAMP)
            for gap in data["data_gaps"]:
                self.assertEqual(sorted(gap), ["end", "source", "start"])

    def test_example_1_is_a_single_root_with_primary_postgres(self):
        data = self.examples()["INC-0003"]
        self.assertEqual(data["primary_service"], "postgres")
        roots = {s["service"] for s in data["symptoms"] if s["role"] == "root_signal"}
        self.assertEqual(roots, {"postgres"})
        self.assertFalse([s for s in data["symptoms"] if s["role"] == "load"])
        self.assertEqual(data["severity"], "high")

    def test_example_2_has_several_symptoms_and_primary_null(self):
        data = self.examples()["INC-0010"]
        self.assertIsNone(data["primary_service"])
        self.assertTrue([s for s in data["symptoms"] if s["role"] == "load"])
        self.assertGreaterEqual(len({s["rule_id"] for s in data["symptoms"]}), 2)
        self.assertIn("gateway", data["candidate_services"])

    def test_example_symptoms_use_known_rule_ids(self):
        ids = {rule["id"] for rule in self.rules}
        for incident_id, data in self.examples().items():
            for symptom in data["symptoms"]:
                self.assertIn(symptom["rule_id"], ids, incident_id)

    def test_every_incident_and_symptom_field_is_described(self):
        incident_keys, symptom_keys = reference_keys()
        absent = [k for k in incident_keys + symptom_keys if f"`{k}`" not in self.doc]
        self.assertEqual(absent, [], "fields of the incident format without a description in docs/DETECTOR.md")


if __name__ == "__main__":
    unittest.main()
