# SentinelOps: Project Context (read this first)

**Owner:** Shahzaib, BS-IT, Minhaj University Lahore (FYP)
**Last updated:** 2026-10-01
**Current module:** M1 (Target application)
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
  common/            shared code (settings, logging, middleware, metrics, db, redis, http, health)
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
| M1 | Target application | 1 | In progress | CP1 done and verified (common/, db/init.sql, inventory, postgres + redis compose); next CP2. Prompt: `prompts/M1_target_app.md` |
| M2 | Observability stack | 1 | Not started | |
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

## 9. Current focus

- **Module:** M1 Target application
- **Checkpoint:** CP1 done and verified. Next: CP2 (payments and orders services)
- **Next task:** reply "continue" in the M1 chat, or start a new chat with the handoff prompt (section 11) and checkpoint CP2

## 10. Known issues

- Postgres-down requests take about DB_POOL_TIMEOUT_SECONDS (2 s), equal to HTTP_TIMEOUT_SECONDS. Check the nested timeouts in CP3 (gateway to orders to inventory).
- Inventory /ready returns 503 when only Redis is down, although reads still work (degraded).
- Starlette 1.x TestClient warns that it prefers httpx2. In CP4 use httpx.ASGITransport in tests.

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
