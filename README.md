# SentinelOps: target application (module M1)

Four small FastAPI services plus postgres and redis, run with Docker Compose. Later SentinelOps modules monitor this
application, break it on purpose, find the root cause and fix it, **without changing this code**.

Host: Windows + Docker Desktop (WSL2). All commands below are PowerShell, run from the repository root.

## Quick start (3 commands)

```powershell
Copy-Item .env.example .env
docker compose up -d --build --wait
python scripts/smoke_test.py
```

- `.env` is not committed. The placeholder `POSTGRES_PASSWORD` is fine for local use (letters, digits, `- _ .` only).
- `--wait` returns when all 6 containers are healthy (the first build takes a few minutes; after that it is well under 90 s).
- The smoke test prints `SMOKE TEST PASSED` and exits with code 0 when everything works.
- Stop: `docker compose down`. Stop and delete the database (re-runs `db/init.sql`): `docker compose down -v`.

## Architecture

```
client (your browser / PowerShell / Locust)
   |
   |  http://localhost:8000          the only published port (the override file adds 8001-8003 for debugging)
   v
gateway --> orders --> inventory --> postgres, redis
   |           |-----> payments  --> postgres, redis
   |           `-----> postgres
   `-------> payments
```

All containers share one Docker network (`sentinelops-net`); services reach each other by name
(`http://orders:8001`, `postgres:5432`, `redis:6379`).

Call graph (also in `docs/topology.yaml`): gateway -> orders, payments; orders -> inventory, payments, postgres;
inventory -> postgres, redis; payments -> postgres, redis.

| Service | Port | Role | Datastores checked by `/ready` |
|---|---|---|---|
| gateway | 8000 (published on 127.0.0.1) | Single entry point, routing, request id, timeouts | none |
| orders | 8001 | Order workflow: reserve stock, create order, charge payment | postgres |
| payments | 8002 | Simulated payments, idempotency keys in Redis | postgres, redis |
| inventory | 8003 | Stock levels, Redis read cache (TTL 30 s) | postgres, redis |
| postgres | 5432 (internal) | Shared database `shop` (tables `items`, `orders`, `payments`) | |
| redis | 6379 (internal) | Cache and idempotency keys | |

`docker-compose.override.yml` (loaded automatically) also publishes 8001, 8002 and 8003 on 127.0.0.1 for debugging.
Use `docker compose -f docker-compose.yml up -d` to run without it.

## Endpoints

Every service also has `GET /health` (liveness), `GET /ready` (own datastores, 503 with details if one fails)
and `GET /metrics` (Prometheus format).

**gateway** (public, `http://localhost:8000`)

| Method and path | Forwards to | Notes |
|---|---|---|
| GET /api/products | orders `/products` | |
| GET /api/products/{sku} | orders `/products/{sku}` | 404 `item_not_found` |
| POST /api/orders | orders `/orders` | body `{sku, quantity, customer_id}`; 201 with the order |
| GET /api/orders/{order_id} | orders `/orders/{id}` | |
| GET /api/payments/{payment_id} | payments `/payments/{id}` | |

The gateway returns the downstream status and body unchanged. If orders or payments time out it returns **504**
`{"error":"downstream_timeout","downstream":"orders","request_id":"..."}`; if it cannot connect, **502**
`{"error":"downstream_unreachable",...}`.

**orders** (8001): `GET /products`, `GET /products/{sku}` (via inventory), `POST /orders`, `GET /orders/{id}`.
Order flow: 1) reserve stock, 2) insert order PENDING, 3) charge payment, 4) mark PAID. If the payment fails, the stock
is released (best effort), the order becomes FAILED and the caller gets 502 or 504 with the `order_id`.

**payments** (8002)

| Method and path | Notes |
|---|---|
| POST /payments/charge | `{order_id, amount, idempotency_key}`; 201 new payment, 200 + header `Idempotent-Replay: true` for a repeated key; delay random between `PAYMENT_DELAY_MS_MIN` and `_MAX` |
| GET /payments/{payment_id} | 404 `payment_not_found` |

**inventory** (8003)

| Method and path | Notes |
|---|---|
| GET /items | list of the 50 seeded products (SKU-001 to SKU-050, stock 1000) |
| GET /items/{sku} | Redis cache then postgres; header `X-Cache` is HIT, MISS or BYPASS (Redis down) |
| POST /items/{sku}/reserve | `{quantity}`; atomic `UPDATE ... WHERE stock >= quantity`; 409 `insufficient_stock` |
| POST /items/{sku}/release | `{quantity}` |

## Configuration

Set in `.env` (see `.env.example`); compose passes them to the containers.

| Variable | Default | Meaning |
|---|---|---|
| POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB | shop / (required) / shop | Database credentials; the password has no default |
| APP_VERSION | 1.0.0 | Docker build arg, becomes an env var; shown in `/health`, `app_info` and every log line; image tag `sentinelops/<service>:<version>` |
| LOG_LEVEL | INFO | Log level |
| HTTP_TIMEOUT_SECONDS | 2.0 | Timeout of calls between services |
| GATEWAY_HTTP_TIMEOUT_SECONDS | 3.0 | Same, for the gateway only (kept above the backend timeout) |
| HTTP_MAX_CONNECTIONS | 50 | Connection limit of the HTTP client |
| DB_POOL_MIN_SIZE / DB_POOL_MAX_SIZE | 2 / 10 | Postgres pool size per service |
| DB_POOL_TIMEOUT_SECONDS | 2.0 | Max wait for a free pool connection |
| PAYMENT_DELAY_MS_MIN / PAYMENT_DELAY_MS_MAX | 20 / 80 | Simulated payment processing time |
| USERS / SPAWN_RATE / DURATION | 20 / 2 / 5m | Load test |

Set per service inside `docker-compose.yml`: `SERVICE_NAME`, `DATABASE_URL`, `REDIS_URL`, `ORDERS_URL`, `PAYMENTS_URL`,
`INVENTORY_URL`.

## Observability conventions

| Item | Convention |
|---|---|
| Logs | JSON, one line per event, stdout. Fields: `ts, level, service, version, request_id, event, method, path, status, duration_ms, error`. One `http_request` line per request (successful `/health`, `/ready`, `/metrics` calls are DEBUG) |
| Request id | `X-Request-ID` is accepted or generated, logged, forwarded on every outbound call, returned in the response |
| Metrics | `http_requests_total`, `http_request_duration_seconds`, `http_requests_in_flight`, `dependency_requests_total{dependency,outcome}`, `dependency_request_duration_seconds`, `db_pool_connections_in_use`, `db_pool_connections_max`, `app_info` |
| Docker labels | `sentinelops.service=<name>`, `sentinelops.tier=app` or `infra` |

## How to verify

**Smoke test** (standard library only, runs on the host Python):

```powershell
python scripts/smoke_test.py
```

It waits for readiness, creates an order through the gateway (PAID, stock decreased, payment exists), checks that one
`X-Request-ID` appears in the logs of gateway, orders, inventory and payments, that all log lines are valid JSON, and that
every `/metrics` has the required names.

**Unit tests** (run inside a Python 3.12 container; the host has Python 3.14):

```powershell
foreach ($s in 'gateway','orders','payments','inventory') { docker compose --profile test run --rm -e SERVICE=$s tests; if ($LASTEXITCODE -ne 0) { break } }
```

**Load test** (Locust, results in `loadtest/results/`, numbers go into `docs/BASELINE.md`):

```powershell
docker compose --profile load run --rm loadtest
```

The run fails (exit code 1) if there is any failed request or p95 is above 500 ms.

**Failure drills** (what each should do; all recover without restarting a service):

| Drill | Command | Expected |
|---|---|---|
| Postgres down | `docker compose stop postgres` | orders, payments, inventory `/ready` 503; `POST /api/orders` 5xx within about 2 s |
| Postgres back | `docker compose start postgres` | everything works again in a few seconds |
| Payments down | `docker compose stop payments` | `POST /api/orders` 502 within milliseconds, stock released, order FAILED |
| Payments hung | `docker compose pause payments` | `POST /api/orders` 504 after about 2 s |
| Redis down | `docker compose stop redis` | inventory still serves reads (`X-Cache: BYPASS`); payments logs a WARNING and still charges |

## Repository layout

```
common/          shared code: settings, logging, middleware, metrics, db, redis, http, health, errors, app factory, test helpers
services/        gateway, orders, payments, inventory  (app/, tests/, Dockerfile, requirements.txt)
db/init.sql      schema and seed data (runs only on an empty database volume)
loadtest/        locustfile.py, Dockerfile, results/
scripts/         smoke_test.py
docs/            topology.yaml, BASELINE.md, PROPOSAL.md, ui-mockup/
prompts/         one prompt file per module
docker-compose.yml, docker-compose.override.yml, .env.example, PROJECT_CONTEXT.md
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `required variable POSTGRES_PASSWORD is missing` | Run from the repo root and copy `.env.example` to `.env` |
| Schema or seed data looks old | `docker compose down -v`, then `docker compose up -d --build --wait` |
| A container is not healthy | `docker compose logs --tail 30 <service>` |
| Port 8000 already in use | Stop the other program, or change the left side of `127.0.0.1:8000:8000` in `docker-compose.yml` |

## Observability (module M2)

Metrics, logs and dashboards for the six M1 containers. The observability containers are optional (compose profile `observability`); without the profile only the six M1 containers start.

### Quick start (PowerShell, repo root)

```powershell
Copy-Item .env.example .env          # first time only; set POSTGRES_PASSWORD (and optionally GRAFANA_ADMIN_PASSWORD)
docker compose --profile observability up -d --build --wait
python scripts/observability_check.py
```

`docker compose ps` should list 11 containers (6 M1 + prometheus, cadvisor, loki, alloy, grafana). "11" means without the detector profile: the incident detector (module M4) is a 12th container in its own compose profile, and with `docker compose --profile observability --profile detector up -d --build --wait` the command `docker compose ps -q` lists 12 containers. `loki` and `alloy` have no healthcheck and show `Up` without `(healthy)`. Wait about 90 seconds before judging rates on the dashboards.

### Where to look

| What | URL |
|------|-----|
| Grafana (login `admin` / `GRAFANA_ADMIN_PASSWORD` from `.env`, default `change_me_dev_only`) | http://127.0.0.1:3000 |
| Dashboard: SentinelOps Overview | http://127.0.0.1:3000/d/sentinelops-overview |
| Dashboard: SentinelOps Service Detail (choose the service at the top; paste a request id to filter the logs) | http://127.0.0.1:3000/d/sentinelops-service-detail |
| Prometheus (targets: `/targets`) | http://127.0.0.1:9090 |
| Loki readiness | http://127.0.0.1:3100/ready |
| Detector status (only with `--profile detector`) | http://127.0.0.1:9100/internal/v1/status |

### Generate load and see it

```powershell
docker compose --profile load run --rm loadtest      # duration from DURATION in .env, about 19 requests/s with 20 users
```

### Stop and reset

```powershell
docker compose --profile observability down          # keeps data
docker compose --profile observability down -v       # deletes the database, metrics, logs and Grafana data
```

Labels, queries, ports, limits, operations and known limitations are documented in [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md). On a host other than Docker Desktop (WSL2) check the cAdvisor containerd socket mount first (see that file, section 7).

## Fault injection (module M3)

Eight faults can be injected into the running stack, and every experiment is written to a ground-truth log: F01 CPU saturation (orders), F02 memory growth (payments), F06 missing environment variable, F08 invalid database credential, F10 PostgreSQL down, F11 lock contention, F13 Redis down, F18 traffic spike. No M1 code changes and no extra container.

### Quick start (PowerShell, repo root)

```powershell
docker compose --profile observability up -d --build --wait
python chaos/chaos.py list
python chaos/chaos.py inject F10          # stops postgres
python chaos/chaos.py status
python chaos/chaos.py clear F10           # starts it again and waits until the stack is healthy
python chaos/chaos.py clear --all         # returns the stack to healthy from any state
python scripts/chaos_check.py             # verifies all 8 faults (18 min 40 s measured; --quick, --only F10)
```

`python chaos/chaos.py run F11 --warmup 60 --hold 120 --cooldown 60` does warm-up, inject, hold, clear and cool-down as one labelled experiment. The ground truth is in `chaos/data/ground_truth.jsonl` (git-ignored; delete `chaos/data` before real evaluation runs). Do not run `scripts/smoke_test.py` while a load test or F18 is running.

Catalogue, expected symptoms with their queries, ground-truth format, safety rules, the F18 measurement and known limitations are in [`docs/CHAOS.md`](docs/CHAOS.md).

## Detection (module M4)

The incident detector turns Prometheus metrics and Loki logs into incidents: nine threshold rules (`detector/rules/rules.json`, each with its provenance) and a deterministic correlator that keeps one open incident at a time. Tier 1 records symptoms and an optional `primary_service`; it does not claim a root cause. The detector is one container (compose profile `detector`, port `127.0.0.1:9100`, incidents in SQLite in the named volume `detector-data`) that reads only Prometheus and Loki.

### Quick start (PowerShell, repo root)

```powershell
docker compose --profile observability --profile detector up -d --build --wait
Invoke-RestMethod http://127.0.0.1:9100/internal/v1/status
(Invoke-RestMethod "http://127.0.0.1:9100/internal/v1/incidents?status=open").incidents
python -m unittest discover -s detector/tests
python scripts/detector_check.py
```

- With the detector running, `docker compose ps -q` lists 12 containers (6 M1 + 5 observability + detector). "11" means without the detector profile. Starting only the detector (`docker compose --profile detector up -d --build --wait detector`) is valid; both sources then report `no_data`.
- API (GET only): `http://127.0.0.1:9100/health`, `/ready`, `/metrics`, `/internal/v1/status`, `/internal/v1/rules`, `/internal/v1/incidents`, `/internal/v1/incidents/<incident_id>`.
- `python scripts/detector_check.py` needs the observability and detector profiles (12 containers) and takes about 75 minutes, at most about 87 minutes (estimated, not yet timed in one go). Options: `--only F10 F13` selects faults, `--quick` shortens the waits, `--skip-nodata` leaves out the Prometheus and Loki no-data step, `--skip-persistence` leaves out the persistence step. One fault alone: `python scripts/detector_check.py --quick --only F01 --skip-nodata --skip-persistence`. Exit code 0 means pass.
- After a change of detector code or `rules.json`, rebuild with the first command above.
- Stop: `docker compose --profile observability --profile detector down` (add `-v` to delete all data volumes, including the incident history).

Architecture, rules with provenance, the incident format with two real examples, correlation, no-data semantics, measured detection times, limitations and troubleshooting are in [`docs/DETECTOR.md`](docs/DETECTOR.md).
