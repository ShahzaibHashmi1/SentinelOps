ROLE
You are a senior backend/DevOps engineer helping me build Module M1 of my final year project "SentinelOps" (AI-powered incident detection, root-cause analysis and remediation for containerised apps). I am a BS-IT student strong in Python, Docker and Azure. I must be able to explain every part in my viva, so keep code simple, readable and commented where a design decision matters.

HOST ENVIRONMENT
Windows with Docker Desktop (WSL2). Every command must work in PowerShell. Do not use a Makefile or bash-only scripts. Add a .gitattributes with "* text=auto eol=lf".

PROJECT CONTEXT
M1 builds the "target application" that later modules will monitor and break. Later modules will attach WITHOUT changing this code: Prometheus scrapes /metrics, a log collector reads container stdout, a fault injector breaks it, an agent inspects and restarts/rolls back its containers.

SCOPE: MODULE M1 ONLY
Do NOT build: Prometheus, Grafana, Loki, Jaeger, fault injection, anomaly detection, LLM or agent code, dashboard, message queue, Kubernetes, Azure deployment. Do not add features that are not listed here.

TECH (fixed)
Python 3.12, FastAPI with lifespan (not deprecated on_event), Pydantic v2, pydantic-settings, uvicorn with 1 worker, httpx AsyncClient, psycopg 3 with psycopg_pool AsyncConnectionPool, redis.asyncio, prometheus-client, Locust, pytest. PostgreSQL 16, Redis 7. Docker Compose v2 syntax (no "version:" key). Pin dependency versions to ones you have verified work together on Python 3.12.

CALL GRAPH (write it also to docs/topology.yaml as service -> list of dependencies)
  gateway   -> orders, payments
  orders    -> inventory, payments, postgres
  inventory -> postgres, redis
  payments  -> postgres, redis

SERVICES AND ENDPOINTS
gateway (public, port 8000):
  GET  /api/products, GET /api/products/{sku}  -> orders
  POST /api/orders                             -> orders
  GET  /api/orders/{order_id}                  -> orders
  GET  /api/payments/{payment_id}              -> payments
  Returns 504 on downstream timeout and 502 on connection error, with JSON body {error, downstream, request_id}.
orders (8001):
  GET /products, GET /products/{sku} -> inventory
  POST /orders {sku, quantity, customer_id}: 1) inventory reserve, 2) insert order PENDING in postgres, 3) payments charge, 4) update order to PAID or FAILED. If payment fails, release inventory (best effort) and mark FAILED. Return 201 with the order.
  GET /orders/{id}
inventory (8003):
  GET /items (list), GET /items/{sku} (Redis cache, TTL 30s, fallback to postgres)
  POST /items/{sku}/reserve {quantity} (atomic UPDATE ... WHERE stock >= quantity, 409 if insufficient)
  POST /items/{sku}/release {quantity}
  Seed 50 SKUs (SKU-001..SKU-050) with stock 1000.
payments (8002):
  POST /payments/charge {order_id, amount, idempotency_key}: idempotency via Redis, insert payment row SUCCEEDED, simulated processing delay random between PAYMENT_DELAY_MS_MIN and PAYMENT_DELAY_MS_MAX (defaults 20 and 80).
  GET /payments/{payment_id}
All services: GET /health, GET /ready, GET /metrics.

CROSS-CUTTING REQUIREMENTS (shared package common/, used by all 4 services)
1. Config via environment variables (pydantic-settings): SERVICE_NAME, APP_VERSION, LOG_LEVEL, DATABASE_URL, REDIS_URL, downstream *_URL, HTTP_TIMEOUT_SECONDS (default 2.0), DB_POOL_MIN_SIZE (2), DB_POOL_MAX_SIZE (10), DB_POOL_TIMEOUT_SECONDS (2.0).
2. Logging: JSON, one line per event, to stdout. Fields: ts, level, service, version, request_id, event, method, path, status, duration_ms, error. One access-log line per HTTP request. Errors include exception type and message.
3. Request ID: accept X-Request-ID or generate uuid4, keep in a contextvar, include in every log line, forward on every outbound call, return in the response header.
4. Metrics (/metrics, Prometheus format): http_requests_total{service,method,route,status}, http_request_duration_seconds (histogram), http_requests_in_flight, dependency_requests_total{dependency,outcome}, dependency_request_duration_seconds{dependency}, db_pool_connections_in_use, db_pool_connections_max, app_info{service,version}. Use route templates (e.g. /items/{sku}), not raw paths.
5. Health: /health returns 200 if the process is alive {status, service, version, uptime_seconds}. /ready checks only the service's own datastores (postgres and/or redis) with a 1 s timeout and returns 503 with details if one fails.
6. Resilience: all outbound HTTP calls use explicit timeouts and connection limits from config. DB access only through the pool with explicit size and timeout. If postgres or redis is down the service must answer quickly (503 or the gateway's 502/504), never hang, and must recover automatically when the dependency returns, without a container restart. Inventory with Redis down falls back to postgres. Payments with Redis down skips the idempotency check and logs a WARNING.
7. Versioning: APP_VERSION is a Docker build arg -> env var, shown in /health, app_info and every log line.

DOCKER
- Build context = repository root, dockerfile per service (services/<name>/Dockerfile), which also copies common/. Base python:3.12-slim, non-root user, pinned requirements.
- docker-compose.yml: project name sentinelops; services gateway, orders, payments, inventory, postgres, redis; healthchecks; depends_on with condition service_healthy; restart unless-stopped; resource limits via deploy.resources.limits (app services 0.5 CPU / 256m, postgres 1.0 CPU / 512m, redis 0.25 CPU / 128m); labels sentinelops.service=<name> and sentinelops.tier=app|infra; one shared network; only gateway publishes a port.
- docker-compose.override.yml (development only) additionally publishes 8001, 8002 and 8003 for direct debugging.
- db/init.sql mounted into the postgres init directory: tables items, orders, payments plus seed data.
- .env.example with all variables (no secrets in the repo), .gitignore.

LOAD TEST
loadtest/locustfile.py with weighted tasks: list products 40, get product 30, create order 25 (random SKU, quantity 1-3), get order 5 (reuse earlier order ids). Runnable headless through a compose profile "load" with USERS, SPAWN_RATE and DURATION variables; CSV results written to loadtest/results/.

TESTS AND DOCS
- pytest unit tests per service (at least: /health, request-id propagation, JSON log format, one business rule each), with mocked dependencies.
- scripts/smoke_test.py (works on Windows): waits for readiness, creates an order through the gateway, checks that the same request id appears in the logs of gateway, orders, inventory and payments (via docker compose logs), checks that each /metrics contains the required metric names. Exit code non-zero on failure.
- README.md: quick start in at most 3 commands, ASCII architecture diagram, endpoint tables, env var table, how to verify.
- docs/BASELINE.md: a template to fill after a load test (requests/s, p50/p95/p99, error %, docker stats CPU and memory per container idle and under load).
- Update PROJECT_CONTEXT.md at the end: module status, decision log, current focus.

DEFINITION OF DONE (all must be true)
1. docker compose up -d --build makes all 6 containers healthy within 90 seconds.
2. POST /api/orders through the gateway returns 201 and the order is PAID; stock decreased; payment row exists.
3. Every /metrics endpoint exposes all required metric names.
4. One X-Request-ID appears in the logs of all four services for one order; logs are valid one-line JSON.
5. Stop postgres: /ready of orders, inventory and payments returns 503 and the gateway returns a 5xx within the timeout. Start postgres again: everything recovers without restarting any service.
6. Stop payments: POST /api/orders fails fast (502/504 within the timeout) and does not hang.
7. Stop redis: inventory still serves reads (from postgres); payments logs a WARNING and still charges.
8. Locust, 20 users for 5 minutes: 0 failures and p95 below 500 ms; numbers recorded in docs/BASELINE.md.
9. Unit tests and smoke test pass; a fresh clone runs with the README quick start.

WORKING METHOD: 4 CHECKPOINTS
After each checkpoint STOP. Print: files created (one line each on what it does), exact PowerShell verification commands, the expected output, and any assumption you made. Wait for me to reply "continue".
CP1: repo skeleton, common/ package, db/init.sql, inventory service, postgres + redis + inventory in docker-compose.
CP2: payments and orders services.
CP3: gateway, request-id and log/metric verification, /ready and failure behaviour (Definition of Done items 1 to 7).
CP4: Locust, smoke test, unit tests, README, topology.yaml, BASELINE.md template, PROJECT_CONTEXT.md update.
Start with CP1 now.
