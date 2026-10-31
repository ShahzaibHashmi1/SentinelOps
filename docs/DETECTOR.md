# SentinelOps incident detector (module M4, tier 1)

**Status:** built (M4 CP5 step 2). The values marked "measured on the owner's machine" were printed by `scripts/detector_check.py` or by the checks of earlier checkpoints; nothing in this file is an estimate unless it says so.
**Audience:** the contract of M4 for M5 (evidence builder), M6 (agent), M10 (dashboard) and M12 (evaluation).
**Related:** `docs/CHAOS.md` (faults and ground truth), `docs/OBSERVABILITY.md` (labels and queries), `detector/rules/rules.json` (the rules), `PROJECT_CONTEXT.md` (decisions D55 to D63).

## 1. What the detector is

| Item | Value |
|---|---|
| Purpose | Turns Prometheus metrics and Loki logs into incidents: threshold rules, then deterministic correlation |
| Claim | Tier 1 records symptoms and an optional `primary_service`. It does not claim a root cause (D61) |
| Container | `detector`, compose profile `detector`, image `sentinelops/detector:${APP_VERSION:-1.0.0}` (python:3.12-slim, standard library only), uid 10001 |
| Labels | `sentinelops.tier=observability`, `sentinelops.service=detector` (Alloy and the cAdvisor allow-list therefore skip it) |
| Port | `127.0.0.1:9100` (localhost only) |
| Storage | SQLite in the named volume `detector-data` mounted at `/data`, journal mode WAL, one writer (D57) |
| Limits | cpus 0.25, memory 128M |
| Inputs | Prometheus (`http://prometheus:9090`) and Loki (`http://loki:3100`) over HTTP, nothing else (D58) |
| Output | A read-only JSON API under `/internal/v1` (D60) and Prometheus metrics at `/metrics` |
| Scope of tier 1 | Threshold rules and incident correlation. No EWMA, z-score, Isolation Forest, Drain3, Alertmanager, notifications or LLM code (D55) |

## 2. Architecture and data flow

```
 Prometheus  <--- POST /api/v1/query  --+
 (GET /-/ready first)                    |        one evaluation cycle every EVAL_INTERVAL_S (5 s)
                                         +--> ThresholdDetector --> Observation list
 Loki        <--- POST /loki/api/v1/query +        (rules.json)            |
 (GET /ready first)                                                        v
                                                                      Correlator  (pure logic, no I/O)
                                                                           |  incidents and symptoms that changed
                                                                           v
                                              SQLite /data/detector.db (one transaction per cycle, WAL)
                                                                           ^
 HTTP API (threads, read-only connections, mode=ro) -----------------------+
   /health /ready /metrics /internal/v1/status /internal/v1/rules /internal/v1/incidents[/<id>]
```

| Step | What happens |
|---|---|
| 1. Source check | Each source asks its readiness path (`/-/ready` for Prometheus, `/ready` for Loki). Anything but HTTP 200 gives `no_data` for that source (section 8) |
| 2. Evaluation | For every rule and every subject (a service name or none) the `ThresholdDetector` yields exactly one `Observation`: `firing`, `ok` or `no_data` |
| 3. Correlation | The `Correlator` receives only observations (and the name, severity, role, `opens_incident`, `for_s` of each rule id). It knows nothing about PromQL, thresholds or SQL |
| 4. Storage | Every incident that changed is written in one SQLite transaction. A failed write is kept in memory and retried in the next cycle |
| 5. Publication | The loop publishes the cycle result for `/internal/v1/status` and `/metrics` |

Properties of the loop: one thread; HTTP timeout 3 s per query; queries are POST forms; a cycle never overlaps the next one (a late cycle logs `cycle_late` and skips ticks); holds use `time.monotonic`, stored times are UTC.

## 3. Start and stop

| Containers | Meaning | Command | Depends on real Docker |
|---|---|---|---|
| 6 | M1 only | `docker compose up -d --wait` | container ids, Compose output |
| 11 | M1 + observability (prometheus, cadvisor, loki, alloy, grafana), **without** the detector profile | `docker compose --profile observability up -d --build --wait` | same |
| 12 | M1 + observability + detector | `docker compose --profile observability --profile detector up -d --build --wait` | same |
| detector only | Both sources then report `no_data` | `docker compose --profile detector up -d --build --wait detector` | same |

- Counts: 6 containers (M1 only), 11 containers (with the `observability` profile, without the detector profile), 12 containers (with the `observability` and `detector` profiles).
- "11" always means "without the detector profile". With the detector running, `docker compose ps -q` lists 12 containers, with or without the profile flags (measured on the owner's machine).
- Prometheus and cAdvisor still see exactly the 6 M1 containers; Alloy collects no detector logs.
- After a change of detector code or of `rules.json`, rebuild: `docker compose --profile observability --profile detector up -d --build --wait` (the rules file is copied into the image).
- After a change of `observability/prometheus/prometheus.yml`, reload Prometheus once: `docker compose --profile observability kill -s SIGHUP prometheus`.
- Stop all 12 containers and keep the data: `docker compose --profile observability --profile detector down`.
- Stop all 12 containers and delete every data volume, including `detector-data` (the incident history): `docker compose --profile observability --profile detector down -v`. A new empty volume starts again at `INC-0001`.
- Stop or restart only the detector: `docker compose --profile detector stop detector`, `docker compose --profile detector restart detector`. An open incident survives (section 7.7).
- Read the detector log: `docker compose --profile detector logs --no-log-prefix detector --tail 50`.

### 3.1 Settings

All settings are environment variables of the container with these defaults. In `docker-compose.yml` the four timing settings come from the optional variables `DETECTOR_*`, which are not in `.env.example`.

| Variable | Compose variable | Default | Meaning |
|---|---|---|---|
| `PROMETHEUS_URL` | (fixed) | `http://prometheus:9090` | Prometheus base URL |
| `LOKI_URL` | (fixed) | `http://loki:3100` | Loki base URL |
| `EVAL_INTERVAL_S` | `DETECTOR_EVAL_INTERVAL_S` | 5 | Seconds between evaluation cycles |
| `WARMUP_S` | `DETECTOR_WARMUP_S` | 30 | Warm-up of a source after it returns and after a detector start |
| `RESOLVE_AFTER_S` | `DETECTOR_RESOLVE_AFTER_S` | 60 | Quiet time before an incident resolves |
| `REOPEN_WITHIN_S` | `DETECTOR_REOPEN_WITHIN_S` | 120 | Window after a resolve in which an opening symptom reopens the same incident |
| `DB_PATH` | (fixed) | `/data/detector.db` | SQLite file |
| `LOG_LEVEL` | `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `APP_VERSION` | `APP_VERSION` | from the image build argument | Reported by `/health` and in every log line |

The HTTP timeout of 3 s per query is a constant, not a setting. An invalid setting stops the detector with exit code 2 (log event `settings_invalid`); an invalid `rules.json` stops it with exit code 3 (`rules_invalid`); an unusable database path stops it with exit code 4 (`store_unavailable`).

## 4. Rules

### 4.1 The nine rules

Exact queries are in `detector/rules/rules.json` and in `GET /internal/v1/rules`. The table describes the signal in words. "Hold" is `for_s`: the rule must fire in every evaluation for that many seconds.

| Id | Name | Source | Signal (subject) | Condition | Hold | Severity | Role | Opens incident | Guard |
|---|---|---|---|---|---|---|---|---|---|
| R01 | target_down | prometheus | `up` of the scrape targets gateway, orders, payments, inventory (job) | `== 0` | 10 s | high | root_signal | yes | none |
| R02 | container_missing | prometheus | Count of cAdvisor memory series per M1 container, 6 expected (service); a missing subject is evaluated as 0 | `== 0` | 10 s | high | root_signal | yes | cAdvisor target up and at least one of the 6 series present, otherwise `no_data` |
| R03 | cpu_high | prometheus | CPU % of the container's CPU limit, 1-minute average (service) | `> 85` | 10 s | medium | root_signal | yes | none |
| R04 | memory_high | prometheus | Memory working set % of the container's memory limit (service) | `> 80` | 10 s | medium | root_signal | yes | none |
| R05 | gateway_5xx_pct | prometheus | Share of gateway requests answered with 5xx, health, ready and metrics routes excluded, 1-minute window (gateway) | `> 5` | 15 s | high | effect | yes | Rate guard: gateway at least 0.5 requests/s, otherwise `ok` |
| R06 | dependency_errors | prometheus | Failed dependency calls per second, 1-minute window (calling service = job); the dependency is written into the detail as `dependency=<name>` | `> 0.1` | 10 s | medium | dependency_effect | yes | none |
| R07 | latency_p95 | prometheus | p95 request latency, health, ready and metrics routes excluded, 1-minute window (service) | `> 0.5` s | 15 s | medium | effect | yes | Rate guard: that service at least 1 request/s, otherwise `ok` |
| R08 | throughput_surge | prometheus | Gateway requests per second (gateway) | `> 30` | 15 s | medium | load | yes | none |
| R09 | log_errors | loki | WARNING and ERROR lines in the last minute (service) | `> 0` | 10 s | low | evidence | **no** | none |

Notes:
- CPU % of the limit can exceed 100; no maximum is assumed.
- `>` is strict: a value equal to the threshold does not fire.
- R09 never opens an incident (Loki is the weaker and later source). An R09 symptom attaches to an open incident, and an active R09 symptom keeps that incident from resolving (section 11).
- The rate guards exist because a single failed request at a very low rate would otherwise read 100 %. Below the minimum the rule is `ok` with the detail "traffic ... is below the minimum", not `no_data`.
- Severities and roles are design assumptions fixed in CP1 and unchanged since.

### 4.2 Provenance and the margin rule

Every rule carries `provenance` in `rules.json`: `baseline_max` (highest normal value measured or documented), `fault_value_min` (first M3 fault reading), `margin_ratio` and `derived_from` (the text of the evidence). `python -m app.rules detector/rules/rules.json` prints the table and the line `pending provenance: none`.

**Margin rule.** A threshold with comparator `>` must be at least 1.5 times the highest baseline value measured or documented (idle, 10 users, 20 users) and must not be above the first M3 fault reading. `margin_ratio = threshold / baseline_max`, rounded to 3 decimals. The loader enforces the rule: an invalid file stops the detector (exit code 3).

| Id | Threshold | baseline_max | fault_value_min | margin_ratio | Source of the numbers (short) |
|---|---|---|---|---|---|
| R01 | `== 0` | 1 | 0 | null (state rule) | F06: `up` is 0 at every reading from the first on; baseline 1 |
| R02 | `== 0` | 1 | 0 | null (state rule) | F10 and F13: series count 0 within 10 s (5 s measured); baseline 1 series per container |
| R03 | 85 | 44 | 90.92 | 1.932 | F01 orders 90.92 % of the CPU limit 45 s after the inject; baseline_max 44 is orders at 20 users (1-minute averages); orders 24.7 and 23.79 at 10 users |
| R04 | 80 | 33.55 | 83.54 | 2.385 | F02 payments 83.54 % 35 s after the inject, steady about 86 to 87; baseline_max 33.55 is payments memory in the owner's full `detector_check.py` run (earlier values 23.9, 29.06, 33.4) |
| R05 | 5 | 0 | 6.26 | null (baseline 0) | F08 6.26, F10 9.19, F11 8.864 (first readings); baseline 0 %; the 17.7 % read right after an F06 recreate is the previous minute, not a baseline |
| R06 | 0.1 | 0 | 0.18 | null (baseline 0) | F08 orders 0.18 per s, F13 0.50 per s (first readings); baseline empty (counted as 0) |
| R07 | 0.5 | 0.2107 | 0.955 | 2.373 | F18 gateway 0.955 s, F11 inventory 1.507 s (first readings); baseline_max 0.2107 is gateway p95 in the owner's full `detector_check.py` run (10 users) |
| R08 | 30 | 18.97 | 31.77 | 1.581 | F18 first reading above 30: 31.77 requests/s after 50 s (10-user background load), plateau about 38; baseline 18.97 requests/s at 20 users |
| R09 | 0 | 0 | 1 | null (baseline 0) | Baseline measured in Loki over 10 idle minutes and 6 minutes with 10 users: no WARNING or ERROR line at all; fault_value_min 1 is the F13 evidence, taken from a different query (an upper bound, not a reading of this query) |

**Approved exceptions** (owner, 2026-10-19): R01 and R02 are state rules (`== 0`, baseline 1) and R05 and R06 have baseline 0, so `margin_ratio` is null for these four. The loader refuses every other violation.

## 5. Incident format (schema_version 1)

The API returns exactly this format. Field order is part of the contract. All timestamps are UTC ISO 8601 with milliseconds and a trailing `Z`, like the ground truth of M3.

### 5.1 Incident fields

| Field | Type | Meaning |
|---|---|---|
| `incident_id` | string | `INC-` plus a zero-padded sequence number from the database (`INC-0003`) |
| `schema_version` | integer | 1 |
| `status` | string | `open` or `resolved` |
| `opened_at` | string | Time the incident was created, i.e. after the hold of its first symptom. **M12 uses it for MTTD** (compared with the inject `ts` of the ground truth) |
| `updated_at` | string | Last cycle that changed the incident |
| `resolved_at` | string or null | Null while open |
| `reopen_count` | integer | How often the incident was reopened (section 7.5) |
| `severity` | string | `low`, `medium` or `high`: the highest severity among the symptoms |
| `title` | string | Deterministic short text (section 7.8) |
| `primary_service` | string or null | Section 7.6 |
| `candidate_services` | list of strings | Services of all symptoms plus the dependency named by R06, ordered by first appearance (`first_seen` of the earliest symptom naming it), then by name |
| `affected_services` | list of strings | Services of all symptoms, same order |
| `symptoms` | list | Section 5.2 |
| `data_gaps` | list | Periods in which a source had no data while the incident existed: `source`, `start`, `end` (null while open) |

### 5.2 Symptom fields

| Field | Type | Meaning |
|---|---|---|
| `symptom_id` | string | `SYM-` plus a zero-padded sequence number |
| `rule_id` | string | `R01` to `R09` (a later detector uses its own rule ids) |
| `detector` | string | `threshold` in tier 1; a later detector stores its own name here, so the schema does not change |
| `source` | string | `prometheus` or `loki` |
| `service` | string or null | The subject of the rule |
| `role` | string | `root_signal`, `effect`, `dependency_effect`, `load` or `evidence` |
| `severity` | string | Severity of the rule |
| `opens_incident` | boolean | Whether the rule can open an incident |
| `first_seen` | string | First firing evaluation of the hold. It shows when the signal started; `opened_at` minus `first_seen` of the opening symptom is the hold |
| `last_seen` | string | Last firing evaluation |
| `active` | boolean | True while the rule fires |
| `flaps` | integer | How often the symptom was reactivated after it stopped |
| `comparator` | string | `>` or `==` |
| `threshold` | number or null | Threshold of the rule |
| `peak_value` | number or null | Most extreme firing value (for `==` rules the latest value) |
| `last_value` | number or null | Value at `last_seen` |
| `detail` | string | Short text, for example `dependency=orders (0.1091)` for R06 or `missing from the result, evaluated as 0` for R02 |

### 5.3 Example 1: single root, primary service postgres (F10, INC-0003)

Real incident returned by the detector API on the owner's machine (key `F10_INC_0003` in the owner's capture): fault F10 (PostgreSQL down), run of `scripts/detector_check.py`. All 11 symptoms are shown, one symptom per line; numbers are as the API returned them (not rounded). Opening symptom: R02 postgres, `first_seen` 07:05:38.440, `opened_at` 07:05:48.440 (the 10 s hold of R02).

```json
{
  "incident_id": "INC-0003",
  "schema_version": 1,
  "status": "resolved",
  "opened_at": "2026-10-24T07:05:48.440Z",
  "updated_at": "2026-10-24T07:09:13.427Z",
  "resolved_at": "2026-10-24T07:09:13.427Z",
  "reopen_count": 0,
  "severity": "high",
  "title": "container_missing postgres, dependency_errors gateway, dependency_errors inventory and 5 more",
  "primary_service": "postgres",
  "candidate_services": ["inventory", "orders", "postgres", "gateway"],
  "affected_services": ["inventory", "orders", "postgres", "gateway"],
  "symptoms": [
    {"symptom_id": "SYM-00024", "rule_id": "R02", "detector": "threshold", "source": "prometheus", "service": "postgres", "role": "root_signal", "severity": "high", "opens_incident": true, "first_seen": "2026-10-24T07:05:38.440Z", "last_seen": "2026-10-24T07:07:08.435Z", "active": false, "flaps": 0, "comparator": "==", "threshold": 0.0, "peak_value": 0.0, "last_value": 0.0, "detail": "missing from the result, evaluated as 0"},
    {"symptom_id": "SYM-00025", "rule_id": "R09", "detector": "threshold", "source": "loki", "service": "inventory", "role": "evidence", "severity": "low", "opens_incident": false, "first_seen": "2026-10-24T07:05:38.440Z", "last_seen": "2026-10-24T07:08:08.431Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.0, "peak_value": 212.0, "last_value": 2.0, "detail": ""},
    {"symptom_id": "SYM-00026", "rule_id": "R09", "detector": "threshold", "source": "loki", "service": "orders", "role": "evidence", "severity": "low", "opens_incident": false, "first_seen": "2026-10-24T07:05:38.440Z", "last_seen": "2026-10-24T07:08:08.431Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.0, "peak_value": 217.0, "last_value": 5.0, "detail": ""},
    {"symptom_id": "SYM-00027", "rule_id": "R06", "detector": "threshold", "source": "prometheus", "service": "gateway", "role": "dependency_effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-24T07:05:43.440Z", "last_seen": "2026-10-24T07:08:03.433Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.1, "peak_value": 3.3821256477861623, "last_value": 0.12727735554020145, "detail": "dependency=orders (0.1273)"},
    {"symptom_id": "SYM-00028", "rule_id": "R06", "detector": "threshold", "source": "prometheus", "service": "inventory", "role": "dependency_effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-24T07:05:43.440Z", "last_seen": "2026-10-24T07:08:03.433Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.1, "peak_value": 3.1821074643149374, "last_value": 0.20000727299174512, "detail": "dependency=postgres (0.2)"},
    {"symptom_id": "SYM-00029", "rule_id": "R06", "detector": "threshold", "source": "prometheus", "service": "orders", "role": "dependency_effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-24T07:05:43.440Z", "last_seen": "2026-10-24T07:08:03.433Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.1, "peak_value": 3.2002909355395945, "last_value": 0.2545593396003418, "detail": "dependency=inventory (0.2546)"},
    {"symptom_id": "SYM-00030", "rule_id": "R09", "detector": "threshold", "source": "loki", "service": "gateway", "role": "evidence", "severity": "low", "opens_incident": false, "first_seen": "2026-10-24T07:05:43.440Z", "last_seen": "2026-10-24T07:08:08.431Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.0, "peak_value": 201.0, "last_value": 5.0, "detail": ""},
    {"symptom_id": "SYM-00031", "rule_id": "R05", "detector": "threshold", "source": "prometheus", "service": "gateway", "role": "effect", "severity": "high", "opens_incident": true, "first_seen": "2026-10-24T07:05:48.440Z", "last_seen": "2026-10-24T07:07:53.433Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 5.0, "peak_value": 100.0, "last_value": 8.558558558558559, "detail": ""},
    {"symptom_id": "SYM-00032", "rule_id": "R07", "detector": "threshold", "source": "prometheus", "service": "gateway", "role": "effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-24T07:05:48.440Z", "last_seen": "2026-10-24T07:07:58.433Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.5, "peak_value": 2.4393063583815024, "last_value": 1.2125000000000001, "detail": ""},
    {"symptom_id": "SYM-00033", "rule_id": "R07", "detector": "threshold", "source": "prometheus", "service": "inventory", "role": "effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-24T07:05:48.440Z", "last_seen": "2026-10-24T07:07:58.433Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.5, "peak_value": 2.425, "last_value": 1.1998903594174057, "detail": ""},
    {"symptom_id": "SYM-00034", "rule_id": "R07", "detector": "threshold", "source": "prometheus", "service": "orders", "role": "effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-24T07:05:48.440Z", "last_seen": "2026-10-24T07:07:58.433Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.5, "peak_value": 2.438135593220339, "last_value": 1.604545454545452, "detail": ""}
  ],
  "data_gaps": []
}
```

How to read it:
- `primary_service` is `postgres`: the only `root_signal` symptom is R02 postgres and the incident has no `load` symptom.
- `candidate_services` is ordered by first appearance: inventory and orders (R09 rows, first seen at the same time as R02, ordered by name), then postgres, then gateway.
- `title` names only symptoms that can open an incident (R09 is left out): 8 distinct labels, 3 shown, `5 more`.
- R09 rows (`source` loki, role `evidence`) are evidence; the incident was open because R02 fired.
- `resolved_at` is 65.0 s after the latest `last_seen` (07:08:08.431): one cycle in which the last symptom stops firing plus the 60 s quiet time.

### 5.4 Example 2: several symptoms, primary service null (F18, INC-0010)

Real incident (key `F18_INC_0010` in the owner's capture): fault F18 (traffic spike), a capacity limit with no single root service. All 7 symptoms are shown. Opening symptoms: R07 gateway and R07 orders, `first_seen` 01:35:37.303, `opened_at` 01:35:52.301 (the 15 s hold of R07).

```json
{
  "incident_id": "INC-0010",
  "schema_version": 1,
  "status": "resolved",
  "opened_at": "2026-10-26T01:35:52.301Z",
  "updated_at": "2026-10-26T01:39:47.283Z",
  "resolved_at": "2026-10-26T01:39:47.283Z",
  "reopen_count": 0,
  "severity": "medium",
  "title": "latency_p95 gateway, latency_p95 orders, throughput_surge gateway and 2 more",
  "primary_service": null,
  "candidate_services": ["gateway", "orders"],
  "affected_services": ["gateway", "orders"],
  "symptoms": [
    {"symptom_id": "SYM-00073", "rule_id": "R07", "detector": "threshold", "source": "prometheus", "service": "gateway", "role": "effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-26T01:35:37.303Z", "last_seen": "2026-10-26T01:38:42.288Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.5, "peak_value": 6.543388429752056, "last_value": 4.333333333333334, "detail": ""},
    {"symptom_id": "SYM-00074", "rule_id": "R07", "detector": "threshold", "source": "prometheus", "service": "orders", "role": "effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-26T01:35:37.303Z", "last_seen": "2026-10-26T01:38:37.288Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.5, "peak_value": 1.8815972222222221, "last_value": 0.8135416666666676, "detail": ""},
    {"symptom_id": "SYM-00075", "rule_id": "R09", "detector": "threshold", "source": "loki", "service": "gateway", "role": "evidence", "severity": "low", "opens_incident": false, "first_seen": "2026-10-26T01:35:42.303Z", "last_seen": "2026-10-26T01:38:22.288Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.0, "peak_value": 9.0, "last_value": 2.0, "detail": ""},
    {"symptom_id": "SYM-00076", "rule_id": "R09", "detector": "threshold", "source": "loki", "service": "orders", "role": "evidence", "severity": "low", "opens_incident": false, "first_seen": "2026-10-26T01:35:47.303Z", "last_seen": "2026-10-26T01:38:22.288Z", "active": false, "flaps": 1, "comparator": ">", "threshold": 0.0, "peak_value": 2.0, "last_value": 1.0, "detail": ""},
    {"symptom_id": "SYM-00077", "rule_id": "R08", "detector": "threshold", "source": "prometheus", "service": "gateway", "role": "load", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-26T01:36:12.301Z", "last_seen": "2026-10-26T01:37:57.290Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 30.0, "peak_value": 37.71251931993818, "last_value": 30.784616783343942, "detail": ""},
    {"symptom_id": "SYM-00078", "rule_id": "R03", "detector": "threshold", "source": "prometheus", "service": "gateway", "role": "root_signal", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-26T01:36:22.298Z", "last_seen": "2026-10-26T01:38:02.290Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 85.0, "peak_value": 112.20684062187469, "last_value": 88.81947449768171, "detail": ""},
    {"symptom_id": "SYM-00079", "rule_id": "R06", "detector": "threshold", "source": "prometheus", "service": "gateway", "role": "dependency_effect", "severity": "medium", "opens_incident": true, "first_seen": "2026-10-26T01:36:32.298Z", "last_seen": "2026-10-26T01:36:52.296Z", "active": false, "flaps": 0, "comparator": ">", "threshold": 0.1, "peak_value": 0.1272842985725975, "last_value": 0.10909487617731554, "detail": "dependency=orders (0.1091)"}
  ],
  "data_gaps": []
}
```

How to read it:
- `primary_service` is null because the incident has a `load` symptom (R08). The CPU symptom R03 on the gateway does not change that.
- One incident carries 5 distinct rule ids (R07, R09, R08, R03, R06); `candidate_services` contains `gateway` and `orders` (orders through the `dependency=orders` detail of R06).
- `title`: first three labels by `first_seen`, then `and 2 more`.
- `resolved_at` is 65.0 s after the latest `last_seen` (01:38:42.288), as in example 1.

## 6. How a consumer reads incidents

| Need | Use |
|---|---|
| Detection time (MTTD, M12) | `opened_at` of the incident minus the `inject` `ts` of the ground-truth run (`chaos/data/ground_truth.jsonl`, order events by file position, exclude `late` events, see `docs/CHAOS.md` section 5) |
| When the signal started | `first_seen` of the first symptom |
| Candidates for the root cause (M5, M6) | `primary_service` when not null, else `candidate_services` ordered by first appearance, plus `symptoms[].role` and `symptoms[].detail` |
| Evidence windows | `symptoms[].first_seen` and `last_seen`; `data_gaps` mark periods without data |
| Dashboard list (M10) | `GET /internal/v1/incidents?status=open` and `?limit=` |
| Repair time | `resolved_at`; note that the detector resolves 60 s after the last symptom stopped, plus the 1-minute query windows (section 10) |

Rules for consumers:
- `primary_service` null means "no single root service claimed", not "unknown error".
- `chaos/data` is deleted before real evaluation runs (`docs/CHAOS.md`).
- Faults injected less than 120 s after the previous incident resolved are merged into one incident (section 11).

## 7. Correlation rules

Implemented in `detector/app/correlator.py` (pure logic, deterministic, no LLM).

1. **Hold.** The key of a symptom is (rule id, subject). A rule must fire in every evaluation for `for_s` seconds before it becomes a symptom. A hold is met when the elapsed time plus 0.5 s reaches `for_s` (so a cycle that starts a few hundred milliseconds early does not cost a whole interval). An `ok`, a `no_data` or a missing observation resets the hold. `first_seen` is the first firing evaluation of the hold, so the delay is visible.
2. **Symptom lifecycle.** A symptom is active while the rule fires and inactive when it stops (`last_seen` = last firing evaluation). A symptom that fires again inside the same incident is reactivated (`flaps` + 1), never duplicated. `peak_value` and `last_value` are kept.
3. **One open incident at a time.** The first symptom whose rule has `opens_incident` true creates the incident (`opened_at` = the cycle in which the hold was met). Every later symptom attaches to it, whatever its `opens_incident` value. A symptom of a rule that cannot open an incident (R09) waits and attaches when an incident opens; it is dropped if it stops firing first. The database enforces one open incident with a partial unique index.
4. **Resolve.** An incident resolves when no symptom has been active for `RESOLVE_AFTER_S` (60 s). A data gap pauses the timer and never counts as quiet time; the interval that ends in the first cycle after a gap is not credited either.
5. **Reopen.** An opening symptom within `REOPEN_WITHIN_S` (120 s) after a resolve reopens the same incident (status `open`, `resolved_at` null, `reopen_count` + 1) instead of creating a new one. After that window a new incident is created. The comparison uses the stored `resolved_at`, so it also works after a detector restart. Only an opening symptom reopens; evidence alone does not.
6. **`primary_service`.** If no symptom of the incident (active or past) has role `load`, and all symptoms with role `root_signal` name one and the same service, that service; otherwise null. The roles `effect`, `dependency_effect` and `evidence` never set it. Expected results: F10 postgres, F13 redis, F06 inventory, F01 orders, F02 payments; F08, F11 and F18 give null.
7. **Persistence.** The latest incident is read back from SQLite after a detector restart. Active symptoms stay active. Hold counters and the resolve timer are not persisted and start from zero (section 11).
8. **Severity and title.** Severity is the highest severity among the symptoms. The title is built from the symptoms that can open an incident (evidence is used only if there is no other symptom): the distinct labels `<rule name> <service>` ordered by `first_seen`, then rule id, then service; the first three joined with `, `, followed by ` and N more` when there are more. Incidents stored before the CP3 title change keep their old title.

## 8. No data and warm-up (D59, D62)

Missing data is a status, not a verdict.

| Situation | Result |
|---|---|
| Source readiness is not HTTP 200 (Prometheus `/-/ready`, Loki `/ready`; a Loki 503 included), timeout, refused connection | Source `no_data`; all its rules give `no_data` |
| A query fails because the server cannot be reached | The whole source goes to `no_data` at once and its warm-up starts again when it returns |
| A query fails because the server answered with an error (HTTP 4xx or 5xx, `status` not `success`, unexpected result type) | Only that rule gives `no_data`; counted in `detector_query_errors_total{source}` |
| A guard query (R02) is not greater than 0 | That rule gives `no_data` |
| Value is NaN or infinite | `no_data` for that subject |
| Empty result | A normal answer, not an error: `on_missing` decides. `zero` (R02) evaluates a missing subject as 0, `ok` treats it as ok. A subject is never silently dropped |
| Source returned after an outage, and the first `WARMUP_S` (30 s) after every detector start | Source `warming_up`: its observations are `no_data` |

Effects of `no_data`:
- It freezes symptoms (`active`, `last_seen`, `last_value` do not change), resets the hold counter of that rule, pauses the resolve timer, and is recorded in `data_gaps` of an open incident.
- It never fires, never clears, never opens, extends or resolves an incident, and no false incident appears when data returns.
- `GET /ready` stays 200 while a source is `no_data` or `warming_up`.
- Every detector start logs one `data_gap_opened` and one `data_gap_closed` per source (the warm-up counts as a gap); no incident is involved.
- Reasons for the warm-up: right after a restart Prometheus has no series for a few seconds (a missing series must not read as a missing container), rate windows are not full, and Loki windows can still contain old lines.

## 9. API and metrics

All routes accept GET only. Any other method on a known path gives 405 with the header `Allow: GET`; an unknown path gives 404. Errors have the body `{"error": {"code": ..., "message": ...}}`. JSON is UTF-8. Incidents are read through a read-only SQLite connection opened per request.

### 9.1 Routes

| Route | Answer |
|---|---|
| `GET /health` | Always 200 `{"status": "ok", "service": "detector", "version": ...}` (liveness; used by the compose healthcheck) |
| `GET /ready` | 200 `{"status": "ready", "last_cycle_age_s": ...}` when the evaluation loop finished a cycle within the last 3 intervals (15 s with the default interval), otherwise 503 (`not_ready`) |
| `GET /metrics` | Prometheus text format (section 9.3) |
| `GET /internal/v1/status` | `sources` (`prometheus`, `loki`: `state` ok, no_data or warming_up, `since`, `detail`), `last_cycle_at`, `cycle_duration_ms`, `cycles`, `rules_loaded`, `open_incident_id` (or null), `rules` (state per rule and subject), `settings` |
| `GET /internal/v1/rules` | `{"rules": [...]}`: the loaded rules, including provenance |
| `GET /internal/v1/incidents?status=&limit=&since=` | `{"incidents": [...]}`, newest first, full incident objects. `status` is `open` or `resolved`; `limit` 1 to 200 (default 50); `since` is an ISO 8601 time (a time without a zone is read as UTC) and means "opened at or after". A bad parameter gives 400 `bad_request` |
| `GET /internal/v1/incidents/<incident_id>` | One incident (section 5) or 404 `not_found` |

PowerShell examples:

```powershell
Invoke-RestMethod http://127.0.0.1:9100/internal/v1/status
(Invoke-RestMethod "http://127.0.0.1:9100/internal/v1/incidents?status=open").incidents
Invoke-RestMethod http://127.0.0.1:9100/internal/v1/incidents/INC-0003 | ConvertTo-Json -Depth 6
```

### 9.2 Exit codes and log events

JSON log lines use the fields of the M1 services (`ts`, `level`, `service`, `version`, `request_id` null, `event`) plus the fields of the event. Events: `detector_started`, `source_state_changed`, `incident_opened`, `incident_reopened`, `incident_resolved`, `symptom_added`, `symptom_reactivated`, `symptom_cleared`, `data_gap_opened`, `data_gap_closed`, `cycle_late`, `store_write_failed`, `cycle_failed`, `detector_failed`, `source_check_failed`, `unknown_rule_ignored`, `http_request` (DEBUG for `/health`, `/ready`, `/metrics`), `detector_stopped`.

### 9.3 Metrics

The metrics have no label named `service` (docs/OBSERVABILITY.md gotcha); the scrape job is `detector`.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `detector_evaluation_cycles_total` | counter | none | Evaluation cycles finished since the start |
| `detector_last_evaluation_timestamp_seconds` | gauge | none | Unix time at which the last cycle finished (0 before the first) |
| `detector_evaluation_duration_seconds` | gauge | none | Duration of the last cycle |
| `detector_source_status` | gauge | `source` | 1 when the source is ok, 0 when `no_data` or `warming_up` |
| `detector_rule_state` | gauge | `rule_id`, `subject` | 1 firing, 0 ok, -1 no_data (36 series on an idle stack, measured) |
| `detector_rule_value` | gauge | `rule_id`, `subject` | Last evaluated value (22 series on an idle stack, measured; R05, R07 and R08 have no value without traffic) |
| `detector_incidents_open` | gauge | none | Open incidents (0 or 1) |
| `detector_incidents_total` | counter | none | Incidents created so far (continues across restarts) |
| `detector_query_errors_total` | counter | `source` | Failed queries per source since the start |

## 10. Measured detection and resolve times

Measured on the owner's machine with `scripts/detector_check.py` (faults run through `python chaos/chaos.py run <ID>`; each value comes from one or two runs, the number is given). Delays count from the END of the inject (the moment `chaos.py` printed `injected ...`); the value in brackets counts from the inject `ts` of the ground truth. The difference is the time `chaos.py` needed to return from the inject (F06 recreates a container, so it is the largest). "Resolved" counts from the `recovered` event of the ground truth.

| Fault | Runs | Opened after end of inject, s (from inject ts) | Opened by | `primary_service` | Resolved after recovered, s | Check limit for opening, s |
|---|---|---|---|---|---|---|
| F10 | 2 | 11.3 (12.7) in the full run; 15.5 in the quick run | R02 postgres | postgres | 119.7; 117.5 | 50 |
| F13 | 2 | 12.5 (13.8); 17.9 in the persistence run | R02 redis | redis | 116.4; 117.2 | 50 |
| F06 | 2 | 4.5 (14.0); 0.3 (13.0) | R01 inventory | inventory | 115.4; 118.0 | 50 |
| F01 | 1 | 59.3 (63.0) | R03 orders | orders | 78.3 | 110 |
| F02 | 1 | 56.0 (59.0) | R04 payments | payments | 72.3 | 140 |
| F08 | 1 | 18.6 (28.9) | R06 gateway, R06 orders | null | 146.5 | 80 |
| F11 | 1 | 17.3 (18.8) | R06 gateway | null | 119.2 | 110 |
| F18 | 1 | 22.3 (28.9) | R07 gateway, R07 orders (R08 joined at +42.3 s, R03 at +52.3 s, R06 at +62.3 s) | null | 119.8 | 110 |

- The limit for resolving is 180 s after the `recovered` event for every fault; the slowest measured value is F08 with 146.5 s.
- Resolve times of about 115 to 120 s follow from the 1-minute query windows (old values stay visible for up to a minute after the recovery) plus the 60 s quiet time.
- F08, F11 and F18 give `primary_service` null; in the F08 run no `root_signal` rule fired (R01 and R02 stayed quiet while orders was recreated).

Baselines measured with 10 load-test users (highest values; no rule fired, no `no_data`, no new incident, both sources ok):

| Signal | gateway | inventory | orders | payments | postgres | redis |
|---|---|---|---|---|---|---|
| CPU % of limit (R03) | 29.4 | 25.7 | 34.4 | 21.5 | 2.7 | 7.7 |
| Memory % of limit (R04) | 24.8 | 29.9 | 26.7 | 33.55 | 7.5 | 9.9 |
| p95 latency, s (R07) | 0.2107 | 0.0366 | 0.1952 | 0.157 | - | - |

Other baseline values: R08 gateway 9.82 requests/s; R05 and R06 0; R09 no log lines.

No-data tests: `no_data` reported after 8 s for Prometheus and for Loki; the warm-up ended with `ok` after 30 s (Prometheus) and 34 s (Loki, `/ready` attempt 5 of 36); no incident was created or resolved; no new incident 90 s after the return.

Persistence test (the detector restarted while an F13 incident was open): same incident, `opened_at` unchanged, 3 symptoms before and 6 after the warm-up, `reopen_count` 0, no second incident, resolved 117.2 s after the recovered event.

Not measured yet: one full run of every step of `scripts/detector_check.py` in one go. Its banner estimates about 75 minutes typical and at most about 87 minutes (an estimate).

## 11. Limitations

| Limitation | Consequence |
|---|---|
| One incident at a time (the stack is one application) | Two independent faults at the same time give one incident |
| Faults injected less than 120 s after the previous incident resolved are merged into one incident (the opening symptom reopens the old one) | M12 runs need at least 130 s between faults; `detector_check.py` waits at least 100 s after the recovered event and at least `reopen_within_s` + 10 s after the previous resolve |
| Starting or stopping the whole stack is a real outage | It opens an incident (R01, R02) |
| F08, F11 and F18 give `primary_service` null | Tier 1 does not tell the caller from the dependency (F08, F11) or name a root service for a capacity limit (F18); `candidate_services` lists the services involved |
| R02 is `no_data` when all six container series are missing while cAdvisor is up | A stop of all M1 containers is carried by R01 (the four app targets) |
| Loki is evidence only (R09 never opens an incident) | An active R09 symptom keeps an incident open, so R09 can delay the resolve |
| Hold counters and the resolve timer are not persisted | After a detector restart they start from zero; the open incident and its active symptoms are read back from SQLite |
| Incidents stored before the CP3 title change keep their old title | For example INC-0001 and INC-0002 on the owner's machine contain `log_errors` in the title |
| 1-minute query windows | Symptoms stay for up to about a minute after a clear; detection lags a change by the hold plus up to one window |
| `rules.json` is copied into the image | A threshold change needs a rebuild |
| A new empty `detector-data` volume restarts the numbering | `docker compose ... down -v` deletes the incident history |
| No notifications, no public API, no authentication | The public API belongs to a later module |

## 12. Adding a detector later (EWMA / z-score)

The interface is in `detector/app/model.py` (D55).

```python
class Detector(ABC):
    name: str = ""
    @abstractmethod
    def evaluate(self, now: datetime) -> list:   # one Observation per rule and subject
        ...
```

An `Observation` has: `rule_id`, `detector` (string, `threshold` now), `source` (`prometheus` or `loki`), `subject` (service name or null), `state` (`firing`, `ok` or `no_data`), `value`, `threshold`, `comparator` (`>` or `==`), `ts` (UTC), `detail`. It is the only thing a detector produces.

| Step | Change |
|---|---|
| New class with `name = "ewma"` and `evaluate(now)` | New module under `detector/app/` |
| Registration | One more element in the detector list passed to `EvaluationLoop` in `App.__init__` of `main.py`; its `RuleMeta` entries (rule id, name, severity, role, `opens_incident`, `for_s`) go into the list given to the `Correlator` (an observation with an unknown rule id is ignored and logged as `unknown_rule_ignored`) |
| Symptoms | They store `detector = "ewma"`; the incident format and the database schema do not change |

What stays unchanged: `model.py`, `correlator.py`, `store.py`, `api.py`, the incident format and the tier 1 code (`thresholds.py`, `rules.json`). The loop publishes the observations of every detector to `/metrics` and `/internal/v1/status` through the same path. `GET /internal/v1/rules` and the per-rule section of the status list the rules of `rules.json` until the rule list handed to the API context also includes the new rules. Isolation Forest and Drain3 are not part of M4.

## 13. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| The detector shows an old threshold, title or rule after a change | The image was built before the change | `docker compose --profile observability --profile detector up -d --build --wait` |
| Prometheus target `detector` is missing or down after a change of `prometheus.yml` | Prometheus did not reload its config | `docker compose --profile observability kill -s SIGHUP prometheus` |
| Container restarts or exits at start | Invalid setting (exit 2), invalid `rules.json` (exit 3), unusable database path (exit 4) | `docker compose --profile detector logs --no-log-prefix detector --tail 50` |
| `/ready` answers 503 | No cycle finished yet, or the last one is older than 3 intervals | Check the log; the loop logs `cycle_late` or `cycle_failed` |
| A source shows `no_data` or `warming_up` | The source is down, answers non-200 on its readiness path, or returned less than 30 s ago | `(Invoke-RestMethod http://127.0.0.1:9100/internal/v1/status).sources`; Loki `/ready` can answer 503 for minutes after a start; no incident is created meanwhile |
| No incident during an outage | Both sources report data and no rule fires, or the sources are in `no_data` | Read `/internal/v1/status`, section `rules`; only `firing` rules count |
| An incident does not resolve | A symptom is still active (R09 log lines in the last minute, or a rule that still fires), or a data gap pauses the timer | Read `symptoms[].active` and `data_gaps` of the incident |
| Two faults appear as one incident | The second opening symptom came within 120 s after the first incident resolved | Wait at least 130 s between faults |
| Stack state unknown after an interrupted fault run | A fault is still injected | `python chaos/chaos.py clear --all` |
| `docker compose ps -q` lists 12 lines, the documentation says 11 | 11 means without the detector profile | Count 12 with the detector running |
| A compose command does not know `prometheus`, `loki` or `detector` | The profile flag is missing | Add `--profile observability` or `--profile detector` to every compose call that names these services |

## 14. Design choice: rules in the detector, not in Prometheus

The proposal's tool list says "Prometheus rules". The thresholds are PromQL and LogQL expressions in `rules.json`, evaluated by the detector, and not Prometheus rule files or Alertmanager. Reasons:

| Reason | Detail |
|---|---|
| Explainable | Each rule carries its provenance (baseline, first fault reading, margin) and a description, and `/internal/v1/rules` shows them |
| Testable | The rules, the holds and the correlation run against fake sources and a fake clock in unit tests, without Docker |
| No new component | No Alertmanager and no `rule_files` or `alerting` section in `prometheus.yml`; the only Prometheus change is the scrape job `detector` |
| One place for both sources | The same rule format covers Prometheus metrics and Loki logs |
| The correlator is needed anyway | The incident format (symptoms, roles, one open incident, resolve, reopen, `primary_service`) is built by the correlator from observations; an alert from Alertmanager would still have to be turned into it |
