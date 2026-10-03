#!/usr/bin/env python3
"""SentinelOps M1 smoke test. Standard library only, so it runs on the Windows host without installing anything.

    python scripts/smoke_test.py            (run from anywhere; it finds the repo root itself)

Checks, in order:
  1. all four services answer /ready (waits up to --timeout seconds)
  2. an order created through the gateway is PAID, stock went down, the payment exists
  3. the SAME X-Request-ID appears in the logs of gateway, orders, inventory and payments
  4. the logs of every service are valid one-line JSON with the mandatory fields
  5. every /metrics endpoint contains the required metric names
Exit code 0 = everything passed, 1 = at least one check failed.

Backend services (orders, payments, inventory) have no published port in the base compose file, so their
/ready and /metrics are read with "docker compose exec" from inside each container.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE_PORTS = {"gateway": 8000, "orders": 8001, "payments": 8002, "inventory": 8003}
REQUIRED_METRICS = (
    "http_requests_total", "http_request_duration_seconds", "http_requests_in_flight",
    "dependency_requests_total", "dependency_request_duration_seconds",
    "db_pool_connections_in_use", "db_pool_connections_max", "app_info",
)
MANDATORY_LOG_FIELDS = ("ts", "level", "service", "version", "request_id", "event")

# Runs INSIDE a container (python is always there): prints the HTTP status, then the body.
IN_CONTAINER_GET = """\
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=3)
    status, body = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    status, body = e.code, e.read().decode()
print(status)
print(body)
"""

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'OK' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def compose(*args: str, stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, input=stdin, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def container_get(service: str, path: str) -> tuple[int, str]:
    """GET a path of `service` from inside its own container. Returns (status, body); (0, error) on failure."""
    url = f"http://127.0.0.1:{SERVICE_PORTS[service]}{path}"
    p = compose("exec", "-T", service, "python", "-", url, stdin=IN_CONTAINER_GET)
    if p.returncode != 0:
        return 0, (p.stderr or p.stdout).strip()[:300]
    status, _, body = p.stdout.partition("\n")
    return int(status.strip() or 0), body


def gateway_call(base_url: str, method: str, path: str, body: dict | None = None,
                 headers: dict | None = None) -> tuple[int, dict | list | str, dict]:
    """Call the gateway from the host. Returns (status, parsed JSON or text, response headers)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        status, raw, hdrs = resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read().decode(), dict(e.headers)
    try:
        return status, json.loads(raw), hdrs
    except ValueError:
        return status, raw, hdrs


def wait_until_ready(timeout: int) -> bool:
    print(f"Waiting up to {timeout}s for /ready on {', '.join(SERVICE_PORTS)} ...")
    deadline = time.time() + timeout
    status: dict[str, int] = {}
    while time.time() < deadline:
        status = {s: container_get(s, "/ready")[0] for s in SERVICE_PORTS}
        if all(code == 200 for code in status.values()):
            return record("all services ready", True, ", ".join(f"{s}={c}" for s, c in status.items()))
        time.sleep(2)
    return record("all services ready", False, ", ".join(f"{s}={c}" for s, c in status.items()))


def check_order_flow(base_url: str, request_id: str) -> None:
    sku, quantity = "SKU-001", 1
    status, product, _ = gateway_call(base_url, "GET", f"/api/products/{sku}")
    if not record("GET /api/products/{sku} through the gateway", status == 200, f"HTTP {status}"):
        return
    stock_before = product["stock"]

    status, order, headers = gateway_call(
        base_url, "POST", "/api/orders", {"sku": sku, "quantity": quantity, "customer_id": "smoke-test"},
        {"X-Request-ID": request_id})
    ok = status == 201 and isinstance(order, dict) and order.get("status") == "PAID"
    if not record("POST /api/orders returns 201 and the order is PAID", ok, f"HTTP {status}"):
        print("    response:", str(order)[:300])
        return
    echoed = {k.lower(): v for k, v in headers.items()}.get("x-request-id")
    record("gateway returns the X-Request-ID it was given", echoed == request_id, f"got {echoed}")

    _, product_after, _ = gateway_call(base_url, "GET", f"/api/products/{sku}")
    after = product_after["stock"] if isinstance(product_after, dict) else None
    record("stock decreased by the ordered quantity", after == stock_before - quantity,
           f"{stock_before} -> {after}")

    status, payment, _ = gateway_call(base_url, "GET", f"/api/payments/{order['payment_id']}")
    record("payment row exists and SUCCEEDED",
           status == 200 and isinstance(payment, dict) and payment.get("status") == "SUCCEEDED"
           and payment.get("order_id") == order["id"], f"HTTP {status}")


def check_logs(request_id: str) -> None:
    for service in SERVICE_PORTS:
        p = compose("logs", "--no-log-prefix", "--tail", "1000", service)
        lines = [ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()]
        parsed, bad = [], 0
        for ln in lines:
            try:
                parsed.append(json.loads(ln))
            except ValueError:
                bad += 1
        missing = sum(1 for d in parsed if not all(f in d for f in MANDATORY_LOG_FIELDS))
        traced = [d for d in parsed if d.get("request_id") == request_id]
        record(f"{service}: request id found in logs", len(traced) > 0, f"{len(traced)} lines")
        record(f"{service}: logs are one-line JSON with mandatory fields", bad == 0 and missing == 0,
               f"{len(parsed)} lines, {bad} not JSON, {missing} missing fields")


def check_metrics() -> None:
    for service in SERVICE_PORTS:
        status, text = container_get(service, "/metrics")
        missing = [m for m in REQUIRED_METRICS if not re.search(rf"^# HELP {m} ", text, re.M)]
        record(f"{service}: /metrics has all {len(REQUIRED_METRICS)} required names",
               status == 200 and not missing, f"missing: {', '.join(missing)}" if missing else "")
        info_ok = re.search(rf'^app_info\{{[^}}]*service="{service}"', text, re.M) is not None
        record(f"{service}: app_info shows its own service name", info_ok)


def main() -> int:
    parser = argparse.ArgumentParser(description="SentinelOps M1 smoke test")
    parser.add_argument("--base-url", default="http://localhost:8000", help="gateway URL as seen from the host")
    parser.add_argument("--timeout", type=int, default=90, help="seconds to wait for readiness")
    args = parser.parse_args()

    if not wait_until_ready(args.timeout):
        print("\nServices are not ready. Try: docker compose ps   and   docker compose logs --tail 30 <service>")
        return 1
    request_id = "smoke-" + uuid.uuid4().hex[:12]
    print(f"\nRequest id for this run: {request_id}")
    check_order_flow(args.base_url, request_id)
    check_logs(request_id)
    check_metrics()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
