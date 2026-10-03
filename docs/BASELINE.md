# SentinelOps M1 baseline

Measured normal behaviour of the target application (module M1) under a steady load, on a healthy stack.
Later modules compare faults and fixes against these numbers.
The numbers in the mockup (`docs/ui-mockup/dashboard.png`) are design placeholders, not results.

## 1. Run information

| Item | Value |
|---|---|
| Date / time | 2026-10-03, 00:46:54 to 00:51:53 UTC (05:46 to 05:51 in Lahore), 300 seconds |
| Git commit / APP_VERSION | APP_VERSION 1.0.0 (all four services); last commit before the CP4 files: 991a254 |
| Host (CPU model, RAM) | Intel Core i7-10750H @ 2.60 GHz (6 cores / 12 threads), 15.8 GB RAM as reported by Windows |
| Docker Desktop / Compose version | Docker 29.8.1 / Docker Compose v5.5.1, Locust 2.46.6 |
| Docker Desktop resources (CPUs, RAM) | 12 CPUs, 7.634 GiB (WSL2 backend) |
| Fresh database before the run (`docker compose down -v` then `up`) | yes (the smoke test created 1 order before the load test) |

## 2. Test parameters

| Item | Value |
|---|---|
| Command | `docker compose --profile load run --rm loadtest` |
| Users / spawn rate / duration | 20 / 2 per second / 5m (all 20 users running after about 9 s) |
| Think time per user | 0.5 to 1.5 s (random) |
| Task mix | list products 40, get product 30, create order 25, get order 5 |
| Target | gateway, `http://gateway:8000` |

## 3. Overall result (Definition of Done item 8: 0 failures and p95 below 500 ms)

| Metric | Value |
|---|---|
| Total requests | 5683 |
| Requests per second | 18.97 |
| p50 (ms) | 12 |
| p95 (ms) | 120 |
| p99 (ms) | 180 |
| Max (ms) | 495 |
| Failed requests | 0 |
| Error % (failures / requests x 100) | 0.00 |
| Pass (0 failures and p95 < 500 ms)? | yes (Locust: `PASS: 0 failures, p95 120 ms (limit 500.0 ms)`, exit code 0) |

Source: `loadtest/results/run_stats.csv` (5683 requests). The console summary printed 3 more requests (5686) that finished while Locust was shutting down; the percentiles are the same.

## 4. Per endpoint

| Endpoint | Requests | Failures | Req/s | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|
| GET /api/products | 2294 | 0 | 7.66 | 10 | 63 | 110 |
| GET /api/products/[sku] | 1614 | 0 | 5.39 | 10 | 62 | 110 |
| POST /api/orders | 1485 | 0 | 4.96 | 90 | 180 | 230 |
| GET /api/orders/[id] | 290 | 0 | 0.97 | 6 | 45 | 64 |

Request mix as measured: 40.4% / 28.4% / 26.1% / 5.1% (target 40 / 30 / 25 / 5).

## 5. Container resources (`docker stats`)

CPU % is relative to one core; the limits are 0.5 CPU / 256 MiB for app services, 1.0 CPU / 512 MiB for postgres,
0.25 CPU / 128 MiB for redis.

| Container | Idle CPU % | Idle memory | Under load CPU % | Under load memory | Memory limit |
|---|---|---|---|---|---|
| sentinelops-gateway-1 | 0.13 | 42.54 MiB | 6.31 | 42.2 MiB | 256 MiB |
| sentinelops-orders-1 | 0.11 | 50.56 MiB | 7.30 | 51.95 MiB | 256 MiB |
| sentinelops-payments-1 | 0.13 | 51.68 MiB | 4.87 | 52.62 MiB | 256 MiB |
| sentinelops-inventory-1 | 0.12 | 53.21 MiB | 30.46 | 66.34 MiB | 256 MiB |
| sentinelops-postgres-1 | 0.00 | 47.5 MiB | 5.54 | 50.28 MiB | 512 MiB |
| sentinelops-redis-1 | 0.35 | 4.621 MiB | 0.62 | 4.457 MiB | 128 MiB |

Idle: sampled after the smoke test and unit tests, before the load test. Under load: one `docker stats` sample taken during the test
(the load-test container itself used 3.32% CPU and 39.88 MiB in that sample). A single sample is a snapshot, not a maximum.

## 6. How to collect the numbers (PowerShell, repo root)

Idle (stack up, no load, wait about 30 s after start):

```powershell
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
```

Under load: start the load test in one window, and in a second window take a sample after about 2 minutes
(repeat 2 or 3 times and write down the highest CPU %):

```powershell
docker compose --profile load run --rm loadtest
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
```

Load-test numbers (the CSV files are written to `loadtest/results/` and overwritten by the next run, so copy them first):

```powershell
Import-Csv .\loadtest\results\run_stats.csv | Where-Object { $_.Name -eq 'Aggregated' } |
  Select-Object 'Request Count','Failure Count','Requests/s','50%','95%','99%','100%'
Import-Csv .\loadtest\results\run_stats.csv | Where-Object { $_.Name -ne 'Aggregated' } |
  Select-Object Name,'Request Count','Failure Count','Requests/s','50%','95%','99%' | Format-Table -AutoSize
```

Error % = `Failure Count` / `Request Count` x 100. The HTML report is `loadtest/results/run_report.html`.

## 7. Observations

- Healthy reference for later modules: error rate 0%, about 19 requests/s, overall p95 120 ms and p99 180 ms; the slowest endpoint is `POST /api/orders` (p50 90 ms, p95 180 ms), the fastest is `GET /api/orders/[id]` (p50 6 ms).
- `POST /api/orders` is slower because one order calls inventory, postgres and payments (payments adds a simulated 20 to 80 ms).
- `inventory` was the busiest container: 30.46% CPU in the sample, which is about 61% of its 0.5 CPU limit (50%). All other containers stayed below 8% CPU. Most likely reason: every product list, product read and stock reservation is served by inventory. It is the service with the least CPU headroom, so a CPU or load fault is expected to show up there first.
- Memory is far from every limit: the highest was inventory at 66.34 MiB of 256 MiB (26%).
- Latency spikes were rare: p99.9 was 260 ms and the single slowest request took 495 ms, still below the 500 ms limit.
- Throughput (18.97 requests/s) matches 20 users with 0.5 to 1.5 s think time. If a later fault drops the rate or raises errors, compare against this baseline.
