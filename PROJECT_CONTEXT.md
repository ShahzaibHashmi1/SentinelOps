# SentinelOps: Project Context (read this first)

**Owner:** Shahzaib, BS-IT, Minhaj University Lahore (FYP)
**Last updated:** 2026-10-15
**Current module:** M3 (Chaos / fault injection), in progress: CP1 to CP4 done and verified, CP5 open. M1 (Target application) and M2 (Observability stack) are done and verified
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
| M3 | Chaos / fault injection | 1→2 | In progress | CP1 done and verified (catalogue of 8 faults, CLI list/status/inject/clear/run, ground truth, safety checks, F10, F13); CP2 done and verified (F01 CPU saturation, F02 memory growth, late flag, Ctrl+C-safe clear, ground-truth masking fix); CP3 done and verified (F06 missing variable, F08 invalid credential, stale/late detection for config faults); CP4 done and verified (F11 lock contention, F18 traffic spike with the 20 versus 100 user measurement, stale/late detection for both, `run` lifecycle for all 8 faults); CP5 open. Prompt: `prompts/M3_chaos.md` |
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
| D43 | 2026-10-11 | Tier 1 contains exactly 8 faults of the proposal catalogue: F01, F02, F06, F08, F10, F11, F13, F18. The other 13 (F03, F04, F05, F07, F09, F12, F14 to F17, F19 to F21) are tier 2 and not built now | Each of the 8 can be injected without changing M1 service code and without a new container |
| D44 | 2026-10-11 | The fault runner is a command-line tool (standard library only) that calls the docker and docker compose CLIs through subprocess. No Docker SDK, stress-ng, Toxiproxy or HTTP trigger API in tier 1 | Host Python has no packages (stdlib rule); the HTTP trigger is built when M10 needs it |
| D45 | 2026-10-11 | Ground truth is append-only JSON lines in `chaos/data/ground_truth.jsonl` (events inject, clear, recovered, aborted). The inject and clear timestamps are taken just before the first docker action; recovery_s = seconds from the clear timestamp to the recovered timestamp. Active state (`chaos/data/active.json`) is written before the docker action | M12 computes MTTD and MTTR from these lines; a crash or Ctrl+C can always be cleared |
| D46 | 2026-10-11 | Catalogue conventions: `$target` placeholder in expected_root_cause.service and in symptom queries, default_duration_s null = the fault stays until clear, symptom `source` is prometheus or loki, an empty query result counts as 0 | One schema for the CLI, chaos_check.py and M4/M12 |
| D47 | 2026-10-11 | F01 and F02 run python programs inside the target container with `docker compose exec -d`. Each program is passed as one base64 argument; its processes carry a marker argument and are found through /proc/<pid>/cmdline (the slim images have no ps); they end at their duration or when a stop file in /tmp exists. F02 reads the memory limit with `docker inspect` and stops allocating when the container's cgroup usage plus one step would pass 90 % of that limit | No change to M1 code or images; no quoting problem on Windows; the ceiling prevents an OOM kill by default (it is a soft ceiling: measured container usage 240 MiB of 256 MiB, no OOM kill) |
| D48 | 2026-10-11 | A clear on a record whose effect was already gone writes `"late": true` on the clear and recovered events; M12 excludes late events from MTTR and orders events by file position (ts values can be equal). The clear timestamp is taken after the read-only observation commands (`docker compose ps`, process count) and before the first command that changes the stack (refines D45). The ground-truth file masks password patterns only (URL passwords, `NAME_PASSWORD=value`); printed output also masks the known secret values. Docker child processes run in their own process group and `run` holds back further Ctrl+C presses while it clears | Found in the owner's CP1 run: an interrupted clear left an incomplete record, and masking a plain word could corrupt a field |
| D49 | 2026-10-13 | F06 and F08 recreate one service from a generated override file (`chaos/overrides/<fault>-<service>.yml`, git-ignored) passed with explicit -f flags after the two base files, no -p. F06 removes DATABASE_URL with `!reset null`; F08 sets a wrong password (`chaos-wrong-password-F08`, not a secret); both are verified on the rendered config and on the new container without printing any value. No restart policy is added (the base file has unless-stopped). Clear recreates from the base files with --force-recreate, so a late clear costs the same time as a normal one. Stale = the container environment is clean or the container is missing; a plain restart is not stale; removing only the override file is not stale. clear --all recreates any orders/payments/inventory container whose DATABASE_URL is absent or carries the marker password | Verified without printing any value; works without a record; the late flag (D48) covers config faults |
| D50 | 2026-10-15 | F11 starts a detached psql in the postgres container (`docker compose exec -d -T -e PGAPPNAME=chaos-F11 postgres psql ...`, application name chaos-F11) that takes an ACCESS EXCLUSIVE lock on `items` and sleeps for the duration. psql uses the local socket (trust), so no password is read, printed or stored; user and database come from the postgres container environment. The session ends by itself (COMMIT) or by clear: pg_terminate_backend found by application name only, then verified gone. Stale = postgres not running or no chaos-F11 session left | Verified on the owner's machine: the lock fills inventory's whole connection pool (10 lock waiters = DB_POOL_MAX_SIZE); no password and no M1 change |
| D51 | 2026-10-15 | F18 runs the M1 load test image in its own container: `docker compose --profile load run -d --no-deps --name sentinelops-chaos-load loadtest`, with USERS, SPAWN_RATE and DURATION in the environment of the docker compose process (printed as `(env ...)`, never `run -e`: compose reads `${...}` from its own environment, which wins over `.env`). Only a container with that name and the compose project, service and one-off labels is touched, and it is removed by id. Stale = the container has exited or does not exist. clear --all also ends leftover chaos-F11 sessions and removes a leftover load container. Defaults stay 100 users, 20 per s, 120 s | Verified: guarantees nothing else is touched; the effect is latency, the gateway saturates first; exit code 1 of the load container is normal |

## 9. Current focus

- **Module:** M3 Chaos / fault injection, in progress
- **Last completed:** M3 CP4 (F11 lock contention, F18 traffic spike, stale and late detection for both, `run` lifecycle for all 8 faults), verified on the owner's Docker Desktop on 2026-10-15: 178 unit tests on host Python 3.14.7, F11 (lock, 10 lock waiters, gateway 5xx % up to 96, inventory p95 up to 2.43 s, recovery 1.6 to 2.0 s, refusal while active and while stale, session ended by hand, duration over), F18 at 100 users (39.6 req/s, p95 3700 ms, 0.40 % failures, gateway the first service above 100 % CPU, no M1 container exited), clear while running, container exited and removed by hand, `clear --all` with leftovers and no record, `run` for F11 and F18, `run` with held-back Ctrl+C (exit code 130), no password in the ground-truth and state files, smoke test 22/22, observability check 29/29. Earlier checkpoint CP3 (F06 missing environment variable, F08 invalid database credential, stale and late detection for config faults), verified on 2026-10-14: 131 unit tests on host Python 3.14.7, `!reset null` probe, F06 on inventory and payments (crash loop, `up` 0, log text, Loki), F08 on orders (healthy but failing queries, dependency errors, gateway 5xx, Loki in orders and postgres), recovery by recreate, stale and late clears (recreated by hand, container removed, only the override file removed, F08 recreated by hand), `clear --all` with and without a record, `run` with a held-back second Ctrl+C (exit code 130), no password in the ground-truth, override and state files, smoke test 22/22, observability check 29/29, dashboards checked
- **Next task:** M3 CP5 (`scripts/chaos_check.py`, `docs/CHAOS.md` with the F18 table from the section 10 CP4 note, Definition of Done review; first change: the two sweeps of `clear --all` in separate try blocks, see the section 10 CP4 note)

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
- M3 CP1: `expected_symptoms` of F11 and F18 were drafts; each was calibrated in the checkpoint that built the fault (F01 and F02 in CP2, F06 and F08 in CP3, F11 and F18 in CP4, see below). Verified on the owner's machine: the F10 symptoms (postgres series 0 within 10 s; gateway 5xx % 10.5 at 10 s and 100 % from 80 s with 10 load-test users; orders/inventory WARNING/ERROR lines rising) and the F13 symptoms (`dependency_requests_total{dependency="redis",outcome!="success"}` about 4.5 per s; Loki lines matching "redis" in inventory/payments). Both faults recovered 7.7 s and 7.8 s after the clear.
- M3 CP1 finding: with Redis down the gateway 5xx % stayed at 0.0 for 90 s (the services degrade, see the /ready note above). Detection of F13 must use dependency failures, logs and /ready, not the error rate.
- Do not run `scripts/smoke_test.py` while a background load test is running: its stock check ("stock decreased by the ordered quantity") fails because the load test orders the same SKU-001 (seen: 984 -> 980).
- M3 CP1: `clear --all` restarts stopped containers but cannot yet recreate a service changed by a fault file (F06/F08, CP3). Faults that stay until clear (F06, F08, F10, F13) reject --duration; timed experiments use `run --hold`.
- M3 CP1: `mask_secrets` replaces every occurrence of a .env secret value in printed output and in ground-truth lines. A password equal to a printed word (for example "postgres") would corrupt the target field. The owner's POSTGRES_PASSWORD was checked and does not occur in `chaos/` files. CP2 restricted the ground-truth masking to known password patterns (D48); printed output still masks the secret values.
- `chaos/data/` (git-ignored) holds the test events written during CP1 verification (the inject/clear/recovered/aborted lines of the F10 and F13 runs); delete the folder before evaluation runs so that the ground truth only contains real experiments.
- M3 CP2: F01 and F02 depend on `python` inside the orders and payments images (verified: Python 3.12.15, user `app` uid 10001, `/tmp` writable) and on a readable cgroup memory file (verified: cgroup v2 `/sys/fs/cgroup/memory.current`; the cgroup v1 fallback was only exercised in the builder's sandbox).
- M3 CP2 calibration, verified on the owner's machine with 10 load-test users. F01, orders CPU % of limit (`100 * sum(rate(container_cpu_usage_seconds_total{service="orders"}[1m])) / (max(container_spec_cpu_quota{service="orders"}) / max(container_spec_cpu_period{service="orders"}))`): baseline 24.7; after the injection 35.5 at 10 s, 56.7 at 20 s, 67.2 at 30 s, 86.9 at 40 s, 95.9 at 60 s, 104.0 at 90 s, 110.1 at 100 s. The condition `> 85` within 90 s holds; values above 100 occur, so never assume a maximum of 100. F02, payments working set % of the limit (`100 * max(container_memory_working_set_bytes{service="payments"}) / max(container_spec_memory_limit_bytes{service="payments"})`): baseline 23.9; 36.2 at 10 s, 50.1 at 20 s, 73.8 at 30 s, 87.0 at 40 s, then 86.3 to 86.5 until 80 s. The condition `> 80` within 120 s holds. The allocator status file showed `holding`, 152 MiB held, container usage 240 MiB: above the 230.4 MiB (90 %) ceiling, because the ceiling is checked once before each 8 MiB step and the container usage also contains file cache and the service's own memory. `OOMKilled` false and restart count 0 before and after the clear. Recovery after a normal clear: F01 3.1 to 5.1 s, F02 2.4 s; late clears: F01 2.5 s, F10 10.1 s, F01 after a manual restart 5.3 s.
- M3 CP2: `run` sets the duration of a self-ending fault (F01, F02, later F11 and F18) to hold + 30 s unless `--duration` is given, so that the clear and not the timer ends it; otherwise every run would end with a late clear.
- M3 CP2: a Ctrl+C in the instant before `run` installs its guard can still interrupt the tool; `clear --all` repairs that state. Verified once on the owner's machine: a Ctrl+C pressed during the clear was held back (events inject, aborted, clear, recovered in one run id, exit code 130). The short gap before the guard is installed was not tested.
- M3 CP2: `scripts/observability_check.py` failed once with `Loki is ready (HTTP 503)` (21 of 22 printed checks OK, exit code 1; only 22 check lines were printed instead of 29). This is the second observed occurrence (see the earlier "Observed once" line). The Loki log tail shows no error from that time and no restart (container up 41 minutes); five direct `/ready` calls and the next run of the script gave `200 ready` and 29/29. The cause is still not found.
- M3 CP2: the Loki log contains `entry too far behind` errors (push answered with HTTP 400, entries with timestamps hours old are dropped) for the postgres and redis streams, the two containers that F10 and F13 stop and start. The timing fits those tests and the earlier note that Alloy positions are not persisted; the mechanism was not verified. The observability check still passes 29/29.
- M3 CP3: `!reset null` removes DATABASE_URL on Compose v5.5.1 (probe on the owner's machine: the rendered environment of inventory had no DATABASE_URL, orders kept it); the tool also checks the rendered config and the new container before it records the fault.
- M3 CP3 calibration, F06, verified on the owner's machine with 10 load-test users: restart count 2 about 13 s after the recreate and 7 thirty seconds later (Docker doubles the delay between restarts, each failing attempt takes about 6 to 7 s); `min(up{job="inventory"})` and `min(up{job="payments"})` are 0 at every reading from the first on (at least 13 s after the recreate); the log shows `RuntimeError: DATABASE_URL is required for this service` raised in the startup lifespan (`common/db.py`), then `Application startup failed. Exiting.`; the Loki count of `(?i)required` in a 2 m window was 7 (inventory) and 9 (payments). Recovery after a normal clear (recreate and wait until healthy): 8.7 to 10.8 s; after a late clear: 8.6 and 12.1 s.
- M3 CP3 calibration, F08 on orders: Docker shows `running healthy` the whole time (liveness-only health check), so only the active.json record and the container environment show the fault. `sum(rate(dependency_requests_total{job="orders",dependency="postgres",outcome!="success"}[1m]))` is empty before the fault, 0.41 per s at the first reading (10 s after the inject returned) and 1.85 to 1.93 per s from 60 s on. The catalogue's gateway 5xx % query: clean baseline 0, then 9.1 at 10 s, 13.0 at 20 s, 18.7 at 30 s, 26.3 at 40 s, 36.0 at 50 s and 32.7 to 36.6 from 60 s on (the condition `> 5` within 90 s holds). The Loki count of `(?i)(password authentication|authentication failed)` in a 2 m window about 100 s after the inject was 56 in the orders logs and 56 in the postgres logs. Recovery after a clear: 10.3 to 13.9 s; after a late clear: 12.1 s.
- M3 CP3: a gateway 5xx % symptom reads the previous minute: right after F06 on payments the baseline was 17.7 %. Measurements and `chaos_check.py` must wait about 100 s between faults before they read a baseline.
- M3 CP3: the base files define DATABASE_URL for orders, payments and inventory in one form without a query string. `clear --all` treats a missing DATABASE_URL in one of them as an F06 leftover, and F08 compares user, host, port and path of the URL with the base value. If the compose files change this, update `faults.py`.
- M3 CP4 calibration, F11, verified on the owner's machine with 10 load-test users and `--duration 180`: baseline gateway 5xx % 0 and inventory p95 0.0115 s, 0 lock waiters (`wait_event_type = 'Lock'`). With the lock held: lock waiters 10 in all 8 readings (equal to DB_POOL_MAX_SIZE 10, the whole pool of inventory is blocked), gateway 5xx % 12.0, 24.4, 42.8, 70.9, 93.0, 95.1, 95.8, 96.2 (10 s steps), inventory p95 1.67 s rising to 2.43 s (the 2 s pool timeout). Both catalogue symptoms hold (5xx above 5, p95 above 1); no change to the catalogue. The session shows `chaos-F11|active|Timeout` and the lock `AccessExclusiveLock|t`. Recovery 1.6 to 2.0 s after the clear in 6 clears (late or not).
- M3 CP4 measurement, F18 at the defaults (100 users, 20 per s, 120 s), no other load, baseline and surge read from Prometheus: Locust 4733 requests, 19 failures (0.40 %), 39.57 req/s, p50 1200 ms, p95 3700 ms, p99 5400 ms, max 7800 ms (BASELINE.md at 20 users: 18.97 req/s, p95 120 ms, 0 %). Prometheus 1-minute values: rps plateau about 38 (peak 42.1), above 30 at 50 s after the inject; p95 1.69 s in the first reading after the ramp, 2.96 s ten seconds later, 4.4 to 4.5 s from 70 s; gateway 5xx % at most 0.6; CPU % of limit: gateway 90 to 98 with one sample of 111 (first service at its limit), orders peak 71, inventory 35, payments 24; idle baseline 13 to 15 %. No M1 container exited, restarted or was OOM-killed. The gateway saturates first, not orders (the earlier estimate that both reach 100 % together at about 45 req/s was wrong). The effect is latency, almost no errors. Defaults stay (the 60 and 150 user repeats were not needed). The load container exited with code 1 (`FAIL: 19 failed requests`), which is normal for a surge; it writes `loadtest/results/run*` like every other load run and git stays clean.
- M3 CP4: stale and late were seen on the real machine for F11 (duration over, session ended by hand) and F18 (container exited, container removed by hand); a clear while the F18 container still runs has no late flag; `clear --all` without a record ended a leftover F11 session and removed a leftover load container (exit 0). `run F18 --warmup 5 --hold 60` with several Ctrl+C presses: `interrupted: clearing ...` then the held-back press line, exit code 130, ground truth inject, aborted, clear, recovered.
- M3 CP4: the unit tests use 64-character container ids; real Docker prints 12 characters, so the real echo is `+ docker rm -f <12 hex characters>` (shortened only above 60 characters). The line after the status table ("service recreated or restarted, or the fault's duration is over") only fits F11 and F18 roughly; reword it in CP5.
- M3 CP4 known gap (found in review with the fake docker, not reproduced on the real machine): in `clear --all` the F11 and F18 sweeps share one try block. If the F11 sweep fails (for example a psql error), the F18 sweep is skipped, a warning is printed, the exit code is 0 and the last line says the stack is healthy while a load container may still run. Fix in CP5: two separate try blocks, with a test.
- M3 CP4: third occurrence of `scripts/observability_check.py` with `Loki is ready (HTTP 503)` (Step 10 after the Ctrl+C test; 21 of 22 printed checks OK, exit code 1). Evidence: Loki up about 30 minutes (started 23:49 local), `restarts=0`, `OOMKilled` false, no Loki log line at the time of the failure (the last lines are the `entry too far behind` errors from the stack start), five direct `/ready` calls gave 200 and the next run gave 29/29. The cause is still not found; the CP4 code does not touch Loki or Alloy.

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
