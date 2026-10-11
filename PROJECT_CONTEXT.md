# SentinelOps: Project Context (read this first)

**Owner:** Shahzaib, BS-IT, Minhaj University Lahore (FYP)
**Last updated:** 2026-10-11
**Current module:** M3 (Chaos / fault injection), not started. M1 (Target application) and M2 (Observability stack) are done and verified
**Full proposal:** `docs/PROPOSAL.md` · **UI target:** `docs/ui-mockup/dashboard.png`

## 1. What this project is

SentinelOps is an AI-powered platform that monitors a containerised application, detects incidents, finds the root cause with an LLM using grounded evidence, proposes and (with approval) executes a safe fix, verifies recovery, and writes a postmortem. It is evaluated against baselines with labelled fault scenarios.

## 2. Rules for any AI assistant working on this repo

1. Work on **one module at a time**. Do not build, stub or scaffold other modules.
2. **Free tools only**, local-first (Docker Compose). Cloud deployment is the last module.
3. All LLM access goes through one function, `call_llm()` (module M6). Never call a provider directly elsewhere.
4. No secrets in the repo. Use `.env` (ignored) and `.env.example`.
5. Every module ends with: README section, tests or a verification script, and an update to this file.
6. Keep code simple and commented where a design decision matters. The owner must be able to explain every part in the viva.
7. Do not rewrite or refactor files unrelated to the current task.
8. Use current, non-deprecated APIs (FastAPI lifespan, Pydantic v2, Docker Compose v2 syntax without `version:`).
9. Work in checkpoints: after each one, list files created, exact verification commands and expected output, then wait.

## 2a. Host environment

Windows with Docker Desktop (WSL2 backend). Commands must work in PowerShell. Edit this line if it changes.

Host facts: Docker 29.8.1 with Docker Compose v5.5.1 (use the Compose file format without a "version:" key). The host Python is 3.14.7, but every service runs on Python 3.12 inside containers. Therefore run pytest inside a python:3.12 container (docker compose run or docker run), never on the host. Write scripts/smoke_test.py using only the Python standard library (urllib, subprocess, json), so it runs on the host without installing packages. Docker Desktop has about 7.6 GiB RAM and 12 CPUs available.

## 2b. Standard commands (PowerShell, repo root)

| Task | Command |
|------|---------|
| Start everything | `Copy-Item .env.example .env` then `docker compose up -d --build --wait` |
| Smoke test | `python scripts/smoke_test.py` (standard library only, exit code 0 = pass) |
| Unit tests (one service) | `docker compose --profile test run --rm -e SERVICE=orders tests` (gateway, orders, payments, inventory) |
| Load test | `docker compose --profile load run --rm loadtest` (USERS, SPAWN_RATE, DURATION from `.env`; CSV in `loadtest/results/`) |
| Reset database | `docker compose down -v` (init.sql only runs on an empty volume) |
| Per-service logs | `docker compose logs --no-log-prefix <service>` |
| Start with observability (11 containers) | `docker compose --profile observability up -d --build --wait` (Grafana http://127.0.0.1:3000, login `admin` / `GRAFANA_ADMIN_PASSWORD` from `.env`) |
| Observability check | `python scripts/observability_check.py` (standard library only, exit code 0 = pass; `--skip-smoke` leaves out the M1 smoke test) |
| Failure test | `docker compose stop payments`, then `docker compose start payments` while a load test runs (procedure in `docs/OBSERVABILITY.md` section 9.2) |
| Stop everything including observability | `docker compose --profile observability down` (add `-v` to delete all data volumes) |

Later modules must keep these working and must not require changes to the M1 service code.

## 3. Architecture (target application, module M1)

```
gateway ──▶ orders ──▶ inventory ──▶ postgres, redis
   │           ├─────▶ payments  ──▶ postgres, redis
   │           └─────▶ postgres
   └──────▶ payments
```

Source of truth for dependencies: `docs/topology.yaml`.

| Service | Port | Role |
|---------|------|------|
| gateway | 8000 (public) | Single entry point, routing, request-id, timeouts |
| orders | 8001 | Order workflow (reserve stock, create order, charge payment) |
| payments | 8002 | Simulated payment processing, idempotency via Redis |
| inventory | 8003 | Stock levels, Redis read cache |
| postgres | 5432 (internal) | Shared database `shop` |
| redis | 6379 (internal) | Cache and idempotency keys |

**Observability containers (module M2, compose profile `observability`; details in `docs/OBSERVABILITY.md`):**

| Service | Port | Role |
|---------|------|------|
| prometheus | 127.0.0.1:9090 | Metrics database; scrapes gateway, orders, payments, inventory, cadvisor every 5 s; 7-day retention |
| cadvisor | 8080 (internal); 127.0.0.1:8081 only in the override file | Container CPU, memory and network, labelled with `service` |
| loki | 127.0.0.1:3100 | Log database; 7-day retention; labels service, container, tier, level |
| alloy | 12345 (internal); 127.0.0.1:12345 only in the override file | Log collector: reads the Docker socket, pushes to Loki |
| grafana | 127.0.0.1:3000 | Dashboards SentinelOps Overview and SentinelOps Service Detail, provisioned from files |

## 4. Fixed technology decisions

| Area | Decision |
|------|----------|
| Language | Python 3.12 |
| Web framework | FastAPI + uvicorn (1 worker per service) |
| HTTP client | httpx (async, explicit timeouts and limits) |
| Database access | psycopg 3 with `AsyncConnectionPool` |
| Cache | Redis 7 (`redis.asyncio`) |
| Config | pydantic-settings, environment variables |
| Metrics | prometheus-client, `/metrics` on every service |
| Logging | JSON, one line per event, to stdout |
| Load testing | Locust |
| Orchestration | Docker Compose v2 |

## 5. Conventions

**Log fields:** `ts`, `level`, `service`, `version`, `request_id`, `event`, `method`, `path`, `status`, `duration_ms`, `error`

**Metric names:** `http_requests_total{service,method,route,status}`, `http_request_duration_seconds`, `http_requests_in_flight`, `dependency_requests_total{dependency,outcome}`, `dependency_request_duration_seconds{dependency}`, `db_pool_connections_in_use`, `db_pool_connections_max`, `app_info{service,version}`

**Endpoints on every service:** `/health` (liveness), `/ready` (own datastores), `/metrics`

**Request tracing:** header `X-Request-ID` is accepted or generated, logged, forwarded on every outbound call and returned in the response.

**Docker labels:** `sentinelops.service=<name>`, `sentinelops.tier=app|infra`

**Env vars:** `SERVICE_NAME`, `APP_VERSION`, `LOG_LEVEL`, `DATABASE_URL`, `REDIS_URL`, `*_URL` for downstream services, `HTTP_TIMEOUT_SECONDS`, `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_TIMEOUT_SECONDS`

## 6. Target repo structure

```
sentinelops/
  common/            shared code (settings, logging, middleware, metrics, db, redis, http, health, errors, app factory, testing helpers)
  services/          gateway, orders, payments, inventory (app/, Dockerfile, requirements.txt, tests/)
  db/init.sql        schema and seed data
  loadtest/          locustfile.py, results/
  scripts/           smoke_test.py, observability_check.py
  observability/     prometheus/ (prometheus.yml), loki/ (loki-config.yml), alloy/ (config.alloy),
                     grafana/provisioning/ (datasources, dashboard provider), grafana/dashboards/ (sentinelops-overview.json, sentinelops-service-detail.json)
  docs/              PROPOSAL.md, topology.yaml, BASELINE.md, OBSERVABILITY.md, ui-mockup/
  prompts/           one prompt file per module
  docker-compose.yml, docker-compose.override.yml, .env.example, .gitignore, .gitattributes
  README.md, PROJECT_CONTEXT.md
```

Later modules add: `chaos/`, `detector/`, `agent/`, `kb/`, `remediation/`, `api/`, `dashboard/`, `notify/`, `eval/`, `infra/`.

## 7. Module status

| ID | Module | Tier | Status | Notes |
|----|--------|------|--------|-------|
| M1 | Target application | 1 | Done (verified) | CP1 to CP4 done; Definition of Done 1 to 9 verified on the owner's Docker Desktop; normal-load baseline in `docs/BASELINE.md`. Prompt: `prompts/M1_target_app.md` |
| M2 | Observability stack | 1 | Done (verified) | CP1 to CP5 done; Definition of Done 1 to 9 verified on the owner's Docker Desktop (check script 29/29; overhead and failure test recorded in `docs/OBSERVABILITY.md` section 9). Contract for later modules: `docs/OBSERVABILITY.md`. Prompt: `prompts/M2_observability.md` |
| M3 | Chaos / fault injection | 1→2 | Not started | |
| M4 | Detection and correlation | 1→2 | Not started | |
| M5 | Evidence builder and dependency graph | 2 | Not started | |
| M6 | LLM RCA agent | 1→3 | Not started | |
| M7 | Knowledge base (RAG) | 2 | Not started | |
| M8 | Remediation engine and guardrails | 1→2 | Not started | |
| M9 | Security layer | 2 | Not started | |
| M10 | Dashboard | 1→3 | Not started | Design target: `docs/ui-mockup/dashboard.png` |
| M11 | ChatOps and postmortem | 2 | Not started | |
| M12 | Evaluation framework | 1→2 | Not started | |
| M13 | Cloud deployment and CI/CD | 3 | Not started | |
| M14 | Replay and offline benchmark | 2 | Not started | |

Status values: Not started · In progress · Done (verified) · Blocked

## 7a. Notes about the UI mockup

The numbers shown in the mockup (MTTR, accuracy, etc.) are placeholders for design only. Real values come from module M12 experiments.

## 8. Design decision log

| ID | Date | Decision | Reason |
|----|------|----------|--------|
| D1 | 2026-10-01 | Python/FastAPI for all services | One language, fast to build, owner knows Python |
| D2 | 2026-10-01 | Docker Compose, local-first | Zero cost; Azure only for final deployment |
| D3 | 2026-10-01 | psycopg pool with explicit size and timeout | Needed for realistic pool-exhaustion faults later |
| D4 | 2026-10-01 | JSON logs and `X-Request-ID` propagation | Lets later modules correlate evidence across services |
| D5 | 2026-10-01 | 1 uvicorn worker per service and compose resource limits | Makes CPU/memory faults map clearly to one process |
| D6 | 2026-10-01 | Message queue deferred to a later module | Not needed for M1; added with fault F14 |
| D7 | 2026-10-01 | Dashboard UI follows `docs/ui-mockup/dashboard.png` | Agreed visual target |
| D8 | 2026-10-01 | Compose healthcheck uses /health (liveness); /ready is for dependency checks | A dependency outage must not mark app containers unhealthy |
| D9 | 2026-10-01 | Successful /health, /ready, /metrics requests logged at DEBUG; 5xx at ERROR | Keeps INFO logs for real traffic and RCA evidence |
| D10 | 2026-10-01 | Pool opens with wait=False, check_connection, reconnect_timeout=10 s | Service starts without postgres and recovers on its own quickly |
| D11 | 2026-10-01 | Inventory cache is invalidated on reserve/release; X-Cache debug header | Reads show current stock; cache behaviour is visible during fault tests |
| D12 | 2026-10-01 | APP_VERSION comes only from the image (build arg to ENV); image tag sentinelops/<svc>:<version> | Rollback by M8 reports the correct version |
| D13 | 2026-10-01 | POSTGRES_PASSWORD required from .env, no default in repo; money stored as NUMERIC(10,2), JSON float | No secrets in the repo; simple API for a simulated shop |
| D14 | 2026-10-01 | Orders workflow compensates on failure: release stock, mark FAILED, answer 502/504 with order_id | Keeps stock and orders consistent when payments or postgres fail mid-flow |
| D15 | 2026-10-01 | Payment idempotency key = order-<order_id>, Redis fast path plus UNIQUE constraint in postgres | Retries never double-charge, even with Redis down |
| D16 | 2026-10-01 | Inventory reserve/release return unit price; orders table has payment_id | Order amount without an extra call; GET /orders returns the payment |
| D17 | 2026-10-01 | Downstream errors: 504 downstream_timeout, 502 downstream_unreachable or downstream_error, body {error, downstream, request_id} | One error contract for orders and the gateway |
| D18 | 2026-10-02 | Gateway timeout 3 s (GATEWAY_HTTP_TIMEOUT_SECONDS) above the 2 s used between backend services | Backend error answers (with downstream detail) reach the client instead of a generic gateway 504 |
| D19 | 2026-10-02 | Gateway forwards downstream status and body unchanged; it issues 504/502 only when orders or payments are unreachable | Gateway stays pure routing; root-cause detail is preserved |
| D20 | 2026-10-02 | Gateway published on 127.0.0.1:8000 only; its /ready has no checks (no datastore) | Safe default on a laptop; readiness reflects own datastores only |
| D21 | 2026-10-02 | Unit tests use fakes (FakeDb, FakeRedis, httpx.MockTransport, ASGITransport) and run in a python:3.12 container via compose profile "test" | Host Python is 3.14; no real postgres/redis/network needed |
| D22 | 2026-10-02 | Smoke test reads backend /ready and /metrics with `docker compose exec` and uses only the standard library | Backends have no published port in the base compose file; works on the Windows host |
| D23 | 2026-10-02 | Load test runs from its own image (Locust 2.46.6 pinned) in compose profile "load"; exit code 1 on any failure or p95 above P95_LIMIT_MS (500) | DoD item 8 is checked automatically; think time 0.5 to 1.5 s per user |
| D24 | 2026-10-02 | docs/topology.yaml is the source of truth for service dependencies | Later modules (dependency graph, evidence builder) read it instead of hard-coding the graph |
| D25 | 2026-10-03 | Prometheus image pinned to prom/prometheus:v3.15.0, retention 7d via command flag, port 127.0.0.1:9090, healthcheck on /-/ready | Reproducible build; infra tools wait for readiness, not just liveness (deliberate difference from D8) |
| D26 | 2026-10-03 | Prometheus config directory is bind-mounted (not a single file); reload with SIGHUP; no --web.enable-lifecycle; no depends_on on app services | File mounts can go stale on Windows; no mutating HTTP endpoint; Prometheus keeps running and shows up=0 while an app is stopped |
| D27 | 2026-10-03 | Scrape job name = service name; no target label called "service"; metrics without a "service" label are selected by job | Avoids exported_service; dependency_*, db_pool_* and http_requests_in_flight carry no service label |
| D28 | 2026-10-04 | cAdvisor pinned to ghcr.io/google/cadvisor:v0.60.6, privileged, no published port in the base file; flags: docker_only, housekeeping 5s, no dynamic housekeeping, whitelisted labels, enable_metrics=cpu,memory,network. On Docker Desktop (WSL2) with the containerd image store the VM's /run/containerd/containerd.sock is mounted read-only into the container | Reproducible; fresh data every 5s scrape; only the metric groups we use. Without the containerd socket cAdvisor registers no docker factory (verified on the owner's machine). The mount gives cAdvisor privileged containerd access, like docker.sock; the path may not exist on other hosts |
| D29 | 2026-10-04 | cadvisor scrape job: honor_timestamps false; metric_relabel keeps only series whose container_label_sentinelops_service is one of the 6 M1 services and copies it to the metric label "service" | Series of stopped containers disappear at the next scrape; consistent "service" label across app and container metrics; limits cardinality |
| D30 | 2026-10-04 | cAdvisor debug port 127.0.0.1:8081 is published only in docker-compose.override.yml | Production-like runs (-f docker-compose.yml) expose nothing extra |
| D31 | 2026-10-05 | Loki pinned to grafana/loki:3.7.8: single binary, auth off, filesystem, TSDB schema v13, 7-day retention via compactor, usage reporting off, discover_service_name [] and discover_log_levels false, localhost-only port 3100 | Same retention as Prometheus; the label set stays exactly service, container, tier, level |
| D32 | 2026-10-05 | Alloy pinned to grafana/alloy:v1.20.1 with --disable-reporting; reads the Docker socket read-only; keeps containers with tier app or infra, drops service loadtest and tests; labels service, container, tier; level taken from the JSON field "level" | Collects only M1 containers; app logs have a level label, postgres and redis (not JSON) have none |
| D33 | 2026-10-05 | request_id, event, path, status stay inside the log line and are read with explicit "| json field=\"field\""; a plain "| json" would rename the fields service and level to service_extracted and level_extracted | Never use high-cardinality values as labels; avoids the clash with the stream labels |
| D34 | 2026-10-05 | No healthcheck for loki (distroless image) and alloy (no wget or curl); readiness is checked over HTTP (Loki /ready, Alloy /-/ready); Alloy debug port 12345 only in docker-compose.override.yml; alloy depends_on loki with service_started | Follows the prompt rule for images without tools; --wait waits only for "running" for these two |
| D35 | 2026-10-06 | Grafana pinned to grafana/grafana:13.2.3 (Alpine variant), localhost-only port 3000, admin login from GRAFANA_ADMIN_PASSWORD (default change_me_dev_only), anonymous access off; usage reporting, update checks, news feed, feedback links, gravatar and plugin preinstall disabled; healthcheck is curl on /api/health | Reproducible; no data sent to the internet; the missing-variable default never breaks the M1-only workflow |
| D36 | 2026-10-06 | Datasources (uids prometheus, loki) and dashboards (folder SentinelOps, files in observability/grafana/dashboards) are provisioned from files; UI edits allowed (allowUiUpdates), changed files re-read every 10 s | Works with no clicking after "up"; dashboards stay in git |
| D37 | 2026-10-06 | Overview queries use 1-minute windows and exclude the routes /health, /ready and /metrics from traffic panels; 5xx % uses "or ... * 0" so services without errors show 0; metrics without a service label are grouped by job | Numbers compare with docs/BASELINE.md; no gaps in error panels |
| D38 | 2026-10-07 | Definition of Done 6 uses a gateway p95 limit of 250 ms and no longer requires inventory to have the highest CPU % | Measured with the full stack: Locust p95 160 ms, dashboard p95 about 203 ms (120 ms without the stack); 1-minute CPU averages: orders about 44 %, gateway about 40 %, inventory about 30 %, payments about 20 % |
| D39 | 2026-10-07 | The two p95 views (Overview table column D and the gateway p95 stat) return no value when a service had no requests in the last minute (`and on(service) (sum by (service) (rate(..._count[1m])) > 0)`); the stat uses an instant query; missing values are shown as `-`, the Up cell of postgres and redis as neutral `n/a` | histogram_quantile of a zero rate is NaN, and a range query with lastNotNull keeps the last old value; a red or green cell would suggest a status that does not exist |
| D40 | 2026-10-07 | Service Detail dashboard (uid sentinelops-service-detail): custom variable `service`, text variable `request_id` (empty = all lines, filter `request_id=~".*$request_id.*"` after `| json request_id="request_id"`); one extra panel "Requests/s by status code" so 502/504 are visible | The prompt lists per-route, dependency, container and logs panels; the status panel is needed for the failure-visibility check (DoD 7) |
| D41 | 2026-10-07 | `scripts/observability_check.py` is standard library only; it sends one real order (1 x SKU-001, own X-Request-ID) through the gateway and finds that id with LogQL in all four app services; postgres and redis log recency is checked over 24 h, app services over 10 min; Grafana is checked through its HTTP API with the admin password from `.env` | Same constraints as the smoke test (host Python 3.14, no packages); postgres and redis log rarely |
| D42 | 2026-10-07 | Overhead of the observability containers is measured with `docker stats` sampling during the 5-minute load test, not with Prometheus | cAdvisor series for containers other than the six M1 services are dropped by D29 |

## 9. Current focus

- **Module:** none in progress; M3 Chaos / fault injection is next
- **Last completed:** M2 CP5 (Service Detail dashboard, check script, docs, README section), verified on the owner's Docker Desktop on 2026-10-11; M2 is Done (verified)
- **Next task:** write `prompts/M3_chaos.md` in the same format as `prompts/M1_target_app.md` and `prompts/M2_observability.md` (scope decision with the owner first; labelled fault catalogue in `docs/PROPOSAL.md` section 8, tier 1 starts with 8 faults), then start the M3 builder chat with `PROJECT_CONTEXT.md` and that prompt.

## 10. Known issues

- With postgres down, POST /api/orders returns 504 or 502 with downstream "inventory", depending on whether orders' 2 s timeout or inventory's 2 s pool timeout fires first. Both are 5xx within the gateway timeout. Make it deterministic only if the evaluation (M12) needs it.
- Observed on Docker Desktop: with payments or postgres stopped, orders answers 504 downstream_timeout after about 2 s (the Docker network gives no fast connection refusal), not a fast 502. This is within Definition of Done item 6 and is the typical fault signature that M3 to M6 will see.
- Inventory and payments /ready return 503 when only Redis is down, although they still serve (degraded).
- If the charge succeeds but marking the order PAID then fails (postgres down at that moment), the order stays PENDING while the payment exists. Not handled in M1; reconciliation could be a later module.
- Locust CSV files in `loadtest/results/` are overwritten by each run and are git-ignored. Copy them before the next run; the M1 baseline numbers are recorded in `docs/BASELINE.md`.
- http_requests_in_flight, dependency_* and db_pool_* have no "service" label; select them by job (job = service name).
- Error-rate queries need "or vector(0)": with no 5xx series, sum(rate(...status=~"5..")) returns no data instead of 0.
- cAdvisor container series are selected by the "service" label (set in Prometheus metric_relabel_configs). Use max by (service) for spec/gauge metrics and sum by (service) for rates.
- cAdvisor exports no restart count. container_scrape_error, machine_* and cadvisor_version_info are dropped by the keep rule; debug via http://127.0.0.1:8081/metrics.
- rate() is underestimated during the first minute of data; wait at least 90 s after a start before judging rates.
- Verified on the owner's machine: container_start_time_seconds{service=...} changes when a container is restarted (payments: 1791142090 before, 1791142121 after `docker compose restart`), so it is the restart signal for M4/M5; cAdvisor has no restart-count metric.
- Idle CPU of the app services is not zero: cAdvisor shows about 11 to 13 % of the CPU limit (1-minute average) per app service on an otherwise idle stack, while single `docker stats` samples jump between about 0.1 % and 36 % of a core. Likely cause: the 5-second healthchecks (a Python process each time) and the 5-second Prometheus scrape. Use averaging windows of at least 30 s for CPU thresholds in M4.
- cAdvisor on Docker Desktop (WSL2) with the containerd image store registers no docker factory unless the VM's /run/containerd/containerd.sock is mounted into the cadvisor container (log symptom: "unable to create containerd client ... no such file or directory"). On hosts without that path Docker creates an empty directory there; adjust or remove the mount.
- The docker.sock and containerd.sock mounts give cAdvisor privileged access to the container runtime; ":ro" does not restrict API access.
- Loki labels are service, container, tier, level only. In LogQL use explicit extraction (`| json request_id="request_id"`) or the stream labels: a plain `| json` renames the JSON fields service and level to service_extracted and level_extracted.
- Loki /ready returns 503 for about 15 s after start. loki and alloy have no healthcheck (images without shell tools), so `docker compose ps` shows them Up without (healthy); `--wait` only waits for "running" for these two.
- Alloy shows its components as healthy even when the Docker socket cannot be read; the failure appears only as "Unable to refresh target groups" errors in its log.
- postgres and redis log rarely; right after a start they may have no lines in Loki. Alloy positions are not persisted, so logs written while Alloy is down may be missed.
- Never use request_id, path or event as a Loki label.
- Grafana's admin password from GRAFANA_ADMIN_PASSWORD only applies when the grafana-data volume is first created. Change it later with: `docker compose --profile observability exec grafana grafana cli admin reset-admin-password <new>`. Do not use "$" in the value.
- Overview panels use 1-minute rate windows: values lag a change by up to a minute and are unreliable during the first 1 to 2 minutes after a start. The "up" panels react within one scrape (5 s).
- The Overview table shows postgres and redis with CPU and memory only (no scrape target, so Up is a neutral n/a and the traffic columns show -).
- `scripts/observability_check.py` creates one real order (1 x SKU-001) per run through the gateway, like the smoke test, and needs the observability profile running.
- Dashboards edited in the Grafana UI are stored in the Grafana database; a changed JSON file in observability/grafana/dashboards overrides them within 10 s.
- Verified on the owner's machine with 1-minute averages (20 users, full stack): CPU % of limit is highest for orders (about 44 %) and the gateway (about 40 %), then inventory (about 30 %) and payments (about 20 %). The earlier note that inventory is the busiest service came from a single `docker stats` sample and is wrong; docs/BASELINE.md carries the correction. Gateway p95 with the full observability stack is about 160 ms (Locust, whole run) to 203 ms (dashboard, 1-minute window) against 120 ms without the stack, so M2 Definition of Done 6 uses a p95 limit of 250 ms (D38).
- Overhead of the observability containers (5-minute load test, one dashboard open at 5 s refresh; `docs/OBSERVABILITY.md` section 9.3): peak CPU in % of one core (limit): prometheus 13.7 (50), loki 2.2 (50), alloy 5.8 (25), cadvisor 3.6 (25), grafana 38.7 (50); peak memory in MiB (limit): prometheus 57.0 (512), loki 86.7 (512), alloy 158.3 (256), cadvisor 29.4 (256), grafana 454.5 (512). Run result: 5654 requests, 0 failures, p95 160 ms, 18.9 requests/s. No OOM kill, no restart.
- Grafana is closest to its limit (454.5 of 512 MiB, about 89 %). The limit is unchanged; raise it only through a new decision if a later module adds dashboards or heavier queries.
- Failure test verified: after `docker compose stop payments` the Overview showed payments DOWN (3 of 4 up), about 25 % 5xx on gateway and orders, gateway and orders p95 about 2.2 s, and requests/s fell from about 19 to about 12; orders logged 504 downstream_timeout. After `docker compose start payments` everything recovered. While a container is stopped its CPU and memory cells show `-` (no cAdvisor series).
- Do not wrap `docker compose up` in PowerShell `Measure-Command`: Compose v5.5.1 then exits at once with "failed to get console: The handle is invalid." and starts nothing. Time it with `Get-Date` before and after instead. Clean start with the profile took about 30 s.
- Observed once: the first run of `scripts/observability_check.py`, about 4 minutes after the start, got HTTP 503 from Loki /ready; the next two runs passed. Cause not found; the script has no retry. If it recurs, collect `docker compose --profile observability logs --no-log-prefix loki --tail 50` right away.
- Overview cosmetics (neutral Up cell for postgres and redis, `-` instead of NaN for p95, p95 stat without stale value) were fixed in CP5 (D39). The p95 stat has no sparkline any more because it uses an instant query.

## 11. Session handoff prompt (paste at the start of a new chat)

```
I am continuing my final year project "SentinelOps". Below is PROJECT_CONTEXT.md.
Follow its rules strictly. Current module: <Mx>. Current checkpoint: <CPy>.
Task: <one clear task>.
Relevant files are pasted after this message. Do not touch anything else.
Stop after the checkpoint and give me verification commands.

<paste PROJECT_CONTEXT.md here>
<paste only the relevant files here>
```
