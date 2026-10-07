# SentinelOps: Project Context (read this first)

**Owner:** Shahzaib, BS-IT, Minhaj University Lahore (FYP)
**Last updated:** 2026-10-06
**Current module:** M2 (Observability stack), in progress: CP1 to CP4 done and verified. M1 (Target application) is done and verified
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
  scripts/           smoke_test.py
  docs/              PROPOSAL.md, topology.yaml, BASELINE.md, ui-mockup/
  prompts/           one prompt file per module
  docker-compose.yml, docker-compose.override.yml, .env.example, .gitignore, .gitattributes
  README.md, PROJECT_CONTEXT.md
```

Later modules add: `observability/`, `chaos/`, `detector/`, `agent/`, `kb/`, `remediation/`, `api/`, `dashboard/`, `notify/`, `eval/`, `infra/`.

## 7. Module status

| ID | Module | Tier | Status | Notes |
|----|--------|------|--------|-------|
| M1 | Target application | 1 | Done (verified) | CP1 to CP4 done; Definition of Done 1 to 9 verified on the owner's Docker Desktop; normal-load baseline in `docs/BASELINE.md`. Prompt: `prompts/M1_target_app.md` |
| M2 | Observability stack | 1 | In progress | CP1 to CP4 done and verified (CP3: commit 6c30ce7): Prometheus, cAdvisor (containerd socket mounted), Loki + Alloy, and Grafana (13.2.3) with the provisioned SentinelOps Overview dashboard. CP5 (Service Detail dashboard, check script, docs) next. Prompt: `prompts/M2_observability.md` |
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

## 9. Current focus

- **Module:** M2 Observability stack (in progress)
- **Last completed:** M2 CP4 (Grafana provisioning + SentinelOps Overview dashboard), verified on the owner's Docker Desktop
- **Next task:** M2 CP5: Service Detail dashboard, scripts/observability_check.py, docs/OBSERVABILITY.md, README section, failure-visibility and overhead checks, final PROJECT_CONTEXT.md update with sections 2b, 3 and 6 (DoD 1, 7, 8, 9). Prompt: `prompts/M2_observability.md`. M2 must not require changes to the M1 service code.

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
- The Overview table shows postgres and redis with CPU and memory only (no scrape target, so Up is n/a).
- Dashboards edited in the Grafana UI are stored in the Grafana database; a changed JSON file in observability/grafana/dashboards overrides them within 10 s.
- Verified on the owner's machine with 1-minute averages (20 users, full stack): CPU % of limit is highest for orders (about 44 %) and the gateway (about 40 %), then inventory (about 30 %) and payments (about 20 %). The earlier note that inventory is the busiest service came from a single `docker stats` sample and is wrong; docs/BASELINE.md carries the correction. Gateway p95 with the full observability stack is about 160 ms (Locust, whole run) to 203 ms (dashboard, 1-minute window) against 120 ms without the stack, so M2 Definition of Done 6 should use a p95 limit of 250 ms.
- Overview cosmetics to fix in CP5: the Up cell for postgres and redis shows a red "n/a" (no scrape target; make it neutral); with no traffic the p95 column of the table shows "NaN" (histogram_quantile of a zero rate; show "-" or "no traffic") and the gateway p95 stat keeps showing the last value (88.7 ms seen while requests/s was 0.0).

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
