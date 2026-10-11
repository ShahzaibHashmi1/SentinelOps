#!/usr/bin/env python3
"""SentinelOps M2 observability check. Standard library only, so it runs on the Windows host without installing anything.

    python scripts/observability_check.py                (stack must be running: docker compose --profile observability up -d --build --wait)
    python scripts/observability_check.py --skip-smoke   (everything except the M1 smoke test, which takes about 20 s)

Checks (one [OK]/[FAIL] line each), in this order:
  1. Prometheus ready; targets gateway, orders, payments, inventory, cadvisor are up
  2. http_requests_total has the labels service, method, route, status and no "exported_service" label exists
  3. container CPU, CPU quota, memory working set and memory limit exist for all 6 containers (label "service")
  4. a request sent through the gateway with its own X-Request-ID is found by LogQL in gateway, orders, payments, inventory
  5. Loki ready, label set is service/container/tier/level (no request_id/path/event), recent logs for all 6 containers
  6. Grafana healthy, datasources "prometheus" and "loki" healthy, dashboards "sentinelops-overview" and
     "sentinelops-service-detail" present (HTTP API, admin password from .env)
  7. M1's smoke test (scripts/smoke_test.py) still passes
Exit code 0 = everything passed, 1 = at least one check failed.

Note: step 4 creates one real order (1 x SKU-001) through the gateway, exactly like the M1 smoke test does.
"""
import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMETHEUS = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
GRAFANA = "http://127.0.0.1:3000"
GATEWAY = "http://127.0.0.1:8000"

APP_SERVICES = ("gateway", "orders", "payments", "inventory")
ALL_SERVICES = APP_SERVICES + ("postgres", "redis")
APP_REGEX = "|".join(APP_SERVICES)    # for PromQL / LogQL matchers
ALL_REGEX = "|".join(ALL_SERVICES)
APP_TARGETS = APP_SERVICES + ("cadvisor",)
CONTAINER_METRICS = (
    "container_cpu_usage_seconds_total",     # CPU use (rate)
    "container_spec_cpu_quota",              # with the period: the CPU limit, needed for "CPU % of limit"
    "container_memory_working_set_bytes",    # memory in use
    "container_spec_memory_limit_bytes",     # memory limit
)
HTTP_LABELS = ("service", "method", "route", "status")
FORBIDDEN_LOKI_LABELS = ("request_id", "path", "event")   # high cardinality: must stay inside the log line
DASHBOARDS = ("sentinelops-overview", "sentinelops-service-detail")
DATASOURCES = ("prometheus", "loki")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'OK' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


# ---------------------------------------------------------------- HTTP helpers
def http(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None,
         timeout: int = 10) -> tuple[int, str]:
    """Returns (status, body text); (0, error text) when the host cannot be reached."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def http_json(url: str, **kw) -> tuple[int, dict | list | None]:
    status, text = http(url, **kw)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, None


def prom_query(expr: str) -> list[dict] | None:
    """Instant PromQL query. Returns the result list, or None on any error."""
    status, data = http_json(f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
    if status != 200 or not isinstance(data, dict) or data.get("status") != "success":
        return None
    return data["data"]["result"]


def read_env_value(key: str, default: str) -> str:
    """Reads KEY=value from .env (simple parser, no quotes handling needed for our placeholders)."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    return default


# ---------------------------------------------------------------- 1 and 2: Prometheus
def check_prometheus() -> None:
    status, text = http(f"{PROMETHEUS}/-/ready")
    if not record("Prometheus is ready", status == 200, f"HTTP {status}"):
        print("    Is the stack running?  docker compose --profile observability up -d --build --wait")
        return

    status, data = http_json(f"{PROMETHEUS}/api/v1/targets?state=active")
    health = {}
    if status == 200 and isinstance(data, dict):
        for t in data["data"]["activeTargets"]:
            health[t["labels"].get("job", "?")] = t["health"]
    for job in APP_TARGETS:
        record(f"target {job} is up", health.get(job) == "up", f"state: {health.get(job, 'not found')}")

    status, data = http_json(f"{PROMETHEUS}/api/v1/labels?" + urllib.parse.urlencode({"match[]": "http_requests_total"}))
    names = set(data["data"]) if status == 200 and isinstance(data, dict) else set()
    missing = [lbl for lbl in HTTP_LABELS if lbl not in names]
    record("http_requests_total has the labels service, method, route, status", not missing,
           f"missing: {', '.join(missing)}" if missing else "")
    wrong = prom_query('count by (__name__) ({exported_service!=""})')
    record('no "exported_service" label anywhere', wrong is not None and len(wrong) == 0,
           "query failed" if wrong is None else ("found on: " + ", ".join(r["metric"]["__name__"] for r in wrong) if wrong else ""))

    for metric in CONTAINER_METRICS:
        res = prom_query(f'count by (service) ({metric}{{service=~"{ALL_REGEX}"}})')
        present = {r["metric"].get("service") for r in res} if res is not None else set()
        missing = [s for s in ALL_SERVICES if s not in present]
        record(f"{metric} exists for all 6 containers", res is not None and not missing,
               f"missing: {', '.join(missing)}" if missing else "")


# ---------------------------------------------------------------- 4 and 5: Loki
def loki_newest(selector: str, window_s: int) -> tuple[int, float | None]:
    """Newest log line matching `selector` in the last window_s seconds. Returns (http status, age in seconds or None)."""
    now = time.time_ns()
    q = urllib.parse.urlencode({"query": selector, "limit": 1, "direction": "backward",
                                "start": now - window_s * 1_000_000_000, "end": now})
    status, data = http_json(f"{LOKI}/loki/api/v1/query_range?{q}")
    if status != 200 or not isinstance(data, dict):
        return status, None
    stamps = [int(v[0]) for s in data["data"]["result"] for v in s["values"]]
    return status, (now - max(stamps)) / 1e9 if stamps else None


def check_request_id_trace() -> None:
    """Send one order through the gateway and find its request id in the logs of all four app services."""
    request_id = "obscheck-" + uuid.uuid4().hex[:12]
    status, text = http(f"{GATEWAY}/api/orders", method="POST",
                        body={"sku": "SKU-001", "quantity": 1, "customer_id": "observability-check"},
                        headers={"X-Request-ID": request_id})
    if not record("order through the gateway with a known X-Request-ID", status == 201, f"HTTP {status}"):
        print("    response:", text[:200])
        return
    # Alloy ships lines within a few seconds; poll up to 40 s.
    selector = (f'{{service=~"{APP_REGEX}"}} |= "{request_id}" '
                f'| json request_id="request_id" | request_id="{request_id}"')
    found: set[str] = set()
    deadline = time.time() + 40
    while time.time() < deadline:
        now = time.time_ns()
        q = urllib.parse.urlencode({"query": selector, "limit": 100, "start": now - 300 * 1_000_000_000, "end": now})
        status, data = http_json(f"{LOKI}/loki/api/v1/query_range?{q}")
        if status == 200 and isinstance(data, dict):
            found = {s["stream"].get("service", "?") for s in data["data"]["result"]}
        if all(s in found for s in APP_SERVICES):
            break
        time.sleep(3)
    missing = [s for s in APP_SERVICES if s not in found]
    record(f"request id {request_id} found by LogQL in all four app services", not missing,
           f"missing: {', '.join(missing)}" if missing else "services: " + ", ".join(sorted(found)))


def check_loki() -> None:
    status, text = http(f"{LOKI}/ready")
    if not record("Loki is ready", status == 200, f"HTTP {status}"):
        return
    now = time.time_ns()
    q = urllib.parse.urlencode({"start": now - 3600 * 1_000_000_000, "end": now})
    status, data = http_json(f"{LOKI}/loki/api/v1/labels?{q}")
    labels = set(data["data"]) if status == 200 and isinstance(data, dict) else set()
    needed = [lbl for lbl in ("service", "container", "tier", "level") if lbl not in labels]
    bad = [lbl for lbl in FORBIDDEN_LOKI_LABELS if lbl in labels]
    record("Loki labels: service, container, tier, level present; request_id, path, event are not labels",
           not needed and not bad,
           "; ".join(x for x in (f"missing: {', '.join(needed)}" if needed else "",
                                 f"must not be labels: {', '.join(bad)}" if bad else "") if x))
    # App services log at INFO only for real traffic (health checks are DEBUG), so the trace step above
    # (and the smoke test) provide the fresh lines. postgres and redis log rarely: allow 24 h.
    for service in ALL_SERVICES:
        window = 600 if service in APP_SERVICES else 86400
        status, age = loki_newest(f'{{service="{service}"}}', window)
        record(f"Loki has recent logs for {service}", age is not None,
               f"newest line {age:.0f} s old" if age is not None else f"no line in the last {window // 60} min (HTTP {status})")


# ---------------------------------------------------------------- 6: Grafana
def check_grafana() -> None:
    status, data = http_json(f"{GRAFANA}/api/health")
    if not record("Grafana is healthy", status == 200 and isinstance(data, dict) and data.get("database") == "ok",
                  f"HTTP {status}"):
        return
    password = read_env_value("GRAFANA_ADMIN_PASSWORD", "change_me_dev_only")
    auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}

    status, _ = http(f"{GRAFANA}/api/org", headers=auth)
    if not record("Grafana admin login with the password from .env", status == 200, f"HTTP {status}"):
        print("    The password only applies when the grafana-data volume is created. Reset it with:")
        print("    docker compose --profile observability exec grafana grafana cli admin reset-admin-password <password>")
        return

    for uid in DATASOURCES:
        status, data = http_json(f"{GRAFANA}/api/datasources/uid/{uid}/health", headers=auth)
        ok = status == 200 and isinstance(data, dict) and str(data.get("status", "")).upper() == "OK"
        record(f"Grafana datasource {uid} is healthy", ok,
               (data or {}).get("message", f"HTTP {status}") if isinstance(data, dict) else f"HTTP {status}")

    for uid in DASHBOARDS:
        status, data = http_json(f"{GRAFANA}/api/dashboards/uid/{uid}", headers=auth)
        ok = status == 200 and isinstance(data, dict)
        detail = f"HTTP {status}"
        if ok:
            detail = f"{len(data['dashboard'].get('panels', []))} panels, provisioned: {data['meta'].get('provisioned')}"
        record(f"Grafana dashboard {uid} is present", ok, detail)


# ---------------------------------------------------------------- 7: M1 smoke test
def check_smoke_test() -> None:
    print("Running the M1 smoke test (about 20 s) ...")
    try:
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_test.py")], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        record("M1 smoke test still passes", False, "timed out after 300 s")
        return
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if "checks passed" in ln), lines[-1] if lines else "no output")
    record("M1 smoke test still passes", p.returncode == 0, summary)
    if p.returncode != 0:
        for ln in lines:
            if ln.startswith("[FAIL]"):
                print("   ", ln)


def main() -> int:
    parser = argparse.ArgumentParser(description="SentinelOps M2 observability check")
    parser.add_argument("--skip-smoke", action="store_true", help="do not run the M1 smoke test")
    args = parser.parse_args()

    check_prometheus()
    check_request_id_trace()      # before the Loki checks: it produces fresh log lines for the four app services
    check_loki()
    check_grafana()
    if not args.skip_smoke:
        check_smoke_test()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("OBSERVABILITY CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
