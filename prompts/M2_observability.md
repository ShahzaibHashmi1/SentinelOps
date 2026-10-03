ROLE
You are a senior DevOps/SRE engineer helping me build Module M2 (observability stack) of my final year project "SentinelOps" (AI-powered incident detection, root-cause analysis and remediation for containerised apps). I am a BS-IT student strong in Python, Docker and Azure. I must be able to explain every part in my viva, so keep configuration simple, readable and commented where a design decision matters.

HOST ENVIRONMENT
Windows with Docker Desktop (WSL2). Every command must work in PowerShell. Do not use a Makefile or bash-only scripts.
Host facts: Docker 29.8.1 with Docker Compose v5.5.1 (Compose file format without a "version:" key). The host Python is 3.14.7, so any script that runs on the host must use only the Python standard library. Docker Desktop has about 7.6 GiB RAM and 12 CPUs; the M1 containers already use up to about 1.7 GiB. Keep everything free and local. No telemetry or usage reporting may be sent to the internet by any new container.

PROJECT CONTEXT
Module M1 (target application: gateway, orders, payments, inventory, postgres, redis, with JSON logs, /metrics, request ids and a measured baseline in docs/BASELINE.md) is done and verified. M2 adds the data collection and dashboards that later modules depend on: M4 (detection) and M12 (evaluation) will query Prometheus, M5 (evidence builder) will query Loki, and M10 (SentinelOps dashboard, design target docs/ui-mockup/dashboard.png) will reuse the same queries. Read PROJECT_CONTEXT.md first; its rules apply. Read docs/BASELINE.md; the dashboards must show numbers that agree with it.

SCOPE: MODULE M2 ONLY
Build: Prometheus, cAdvisor, Loki, Grafana Alloy, Grafana with provisioned datasources and dashboards, a verification script and documentation.
Do NOT build: alert rules, Alertmanager, Grafana alerting, tracing (Jaeger/Tempo/OpenTelemetry), custom exporters, anomaly detection, fault injection, LLM or agent code, the SentinelOps dashboard (M10), or anything that changes M1 service code, common/, db/, or the M1 behaviour of docker-compose.yml. If an M1 change seems necessary, STOP and ask me. Do not add features that are not listed here.

TECH (fixed)
Prometheus, Loki (single binary, filesystem storage), Grafana Alloy (log collector; Promtail is end-of-life), cAdvisor, Grafana. Use the current stable major version of each and pin an exact image tag you are sure exists (never "latest"). List every tag in your CP1 report; if a tag fails to pull on my machine I will tell you and you replace it. Config files live in the repo and are bind-mounted; dashboards and datasources are provisioned from files (no manual clicking). Use Compose profile "observability".

REPOSITORY LAYOUT (additions only)
  observability/prometheus/prometheus.yml
  observability/loki/loki-config.yml
  observability/alloy/config.alloy
  observability/cadvisor/            (only if a file is needed)
  observability/grafana/provisioning/datasources/datasources.yml
  observability/grafana/provisioning/dashboards/dashboards.yml
  observability/grafana/dashboards/sentinelops-overview.json
  observability/grafana/dashboards/sentinelops-service-detail.json
  scripts/observability_check.py     (standard library only)
  docs/OBSERVABILITY.md              (the contract that later modules rely on)

COMPOSE INTEGRATION
- All new services have profiles: ["observability"], attach to the existing network "sentinelops", and have healthchecks where the image provides wget or curl (verify per image; if an image has neither, skip the healthcheck for it and say so).
- Start command: docker compose --profile observability up -d --build --wait
- A plain "docker compose up -d --wait" must still start exactly the 6 M1 containers, and M1's smoke test (python scripts/smoke_test.py) must still pass 22/22 with and without the profile.
- Labels on every new service: sentinelops.service=<name> and sentinelops.tier=observability.
- Named volumes for prometheus, loki and grafana data. Note in docs/OBSERVABILITY.md that "docker compose down -v" deletes them.
- Published ports, bound to 127.0.0.1 only: grafana 3000, prometheus 9090, loki 3100 (base compose file). cAdvisor (8081) and Alloy (12345) are published only in docker-compose.override.yml for debugging.
- Resource limits (deploy.resources.limits): prometheus 0.5 CPU / 512m, loki 0.5 CPU / 512m, alloy 0.25 CPU / 256m, cadvisor 0.25 CPU / 256m, grafana 0.5 CPU / 512m.
- Add GRAFANA_ADMIN_PASSWORD to .env.example with a placeholder (change_me_dev_only) and use ${GRAFANA_ADMIN_PASSWORD:-change_me_dev_only} in compose, so a missing .env value never breaks the M1-only workflow. Anonymous access disabled; Grafana usage reporting and update checks disabled.

PROMETHEUS
- scrape_interval 5s, scrape_timeout 4s, retention 7 days. No rule_files, no alerting section.
- Scrape jobs: one job per M1 service named exactly gateway, orders, payments, inventory (targets gateway:8000, orders:8001, payments:8002, inventory:8003, path /metrics, reached over the Docker network, so no published backend ports are needed), plus cadvisor and prometheus.
- IMPORTANT: the M1 metrics already carry a label named "service". Never add a target label called "service" in scrape or relabel config, because Prometheus would rename the metric's own label to "exported_service". Verify that http_requests_total still has service, method, route and status exactly as exposed.
- cAdvisor series must also be queryable with a "service" label whose values are gateway, orders, payments, inventory, postgres, redis (create it with metric_relabel_configs from container_label_sentinelops_service; cAdvisor series have no "service" label, so this is safe). Drop series that do not belong to the six M1 containers to limit cardinality.

CADVISOR (the riskiest part on Docker Desktop with WSL2 and cgroup v2)
- Needed series per container: CPU usage (rate of container_cpu_usage_seconds_total), memory working set, memory limit, CPU quota/period (to compute CPU as % of the container's limit), network bytes, restart count if available.
- Use only the container labels you need (whitelist com.docker.compose.service and the sentinelops.* labels), docker-only mode, a 5s housekeeping interval, and disable the metric groups we do not use. Verify every flag exists for the pinned version.
- If cAdvisor cannot produce per-container series with the service label on this host, STOP at that checkpoint, report exactly what you see, and propose a fallback (for example a tiny docker-stats exporter). Do not build the fallback until I approve it.

LOGS: LOKI + ALLOY
- Alloy discovers containers through the Docker socket (read-only mount of /var/run/docker.sock) and ships logs to Loki. Collect only containers that have the label sentinelops.tier with value app or infra and exclude the ephemeral loadtest and tests services and the observability tier itself.
- Loki labels (low cardinality only): service (from sentinelops.service), container, tier, level (taken from the JSON field "level" of the app logs; postgres and redis logs are not JSON and simply have no level). Never make request_id, path or event a label; they stay inside the log line and are used with LogQL "| json".
- Loki: auth disabled, single binary, TSDB schema v13 on the filesystem, 7-day retention, usage reporting off, sensible rate limits for a laptop.

GRAFANA
- Datasources provisioned with fixed uids: "prometheus" (default, http://prometheus:9090) and "loki" (http://loki:3100).
- Dashboards provisioned from the repo, stable uids, default refresh 5s, default range last 30 minutes, editable. Use simple panels (stat, time series, table, bar gauge, logs). Every panel has a one-line description saying what its query means.
- Dashboard 1 "SentinelOps Overview" (uid sentinelops-overview), aligned with the mockup:
    top stats: services up (x of 4), requests/s at the gateway, error % at the gateway (5xx / all), gateway p95 latency, average CPU % of limit (app services), average memory % of limit (app services);
    service health table: one row per service with up, req/s, error %, p95, CPU % of limit, memory % of limit;
    resource usage: CPU % of limit and memory working set per container (all 6);
    traffic: requests/s, error rate and p50/p95/p99 latency per service; in-flight requests;
    dependencies: calls per second by outcome and p95 latency per dependency (postgres, redis, downstream services); db pool in use versus max per service;
    logs: recent WARNING and ERROR lines from all services and log volume by level.
- Dashboard 2 "SentinelOps Service Detail" (uid sentinelops-service-detail): variable "service" (gateway, orders, payments, inventory); per-route request rate, error % and p95/p99; dependency panels and db pool for that service; its container CPU and memory; a logs panel for that service with a text variable "request_id" that filters with | json.
- Do not build an "Active incidents" panel; incidents come from M4.

DOCUMENTATION (docs/OBSERVABILITY.md)
Ports and URLs (host and in-network), the label contract (service values, labels per metric and per log stream), a catalogue of the PromQL and LogQL queries used by the dashboards (each tested), how to add a scrape target, how to reload config, retention, resource limits, the "service" label gotcha, and known limitations. Later modules will be told to use this file as their contract.

VERIFICATION SCRIPT (scripts/observability_check.py, standard library only, exit 0 = pass)
Checks, with one [OK]/[FAIL] line each: Prometheus ready; targets gateway, orders, payments, inventory, cadvisor all up; http_requests_total has the "service" label and no "exported_service"; container CPU, memory and memory-limit series exist for all 6 containers with the service label; Loki ready and has recent logs for gateway, orders, payments, inventory (and postgres, redis); a request id sent through the gateway is found by LogQL in all four app services; Grafana healthy, both datasources healthy, both dashboards present (via the HTTP API with the admin password from .env); and M1's smoke test still passes.

DEFINITION OF DONE (all must be true)
1. From a clean state, docker compose --profile observability up -d --build --wait makes all 11 containers healthy within 120 seconds (6 M1 + prometheus, loki, alloy, cadvisor, grafana). Without the profile only the 6 M1 containers start. The M1 smoke test passes 22/22 in both modes.
2. Prometheus shows 4 app targets and cadvisor up; no "exported_service" label anywhere.
3. Container CPU, memory working set and memory limit are queryable for all 6 containers by the "service" label.
4. Loki has logs for the 4 app services with labels service, container, tier and level; the JSON fields are queryable; one request id returns lines from all four app services.
5. Grafana opens at http://localhost:3000 with both datasources and both dashboards present after "up", without any manual setup.
6. During a 2-minute load test (DURATION=2m), the Overview dashboard shows about 19 requests/s (15 to 25), 0 errors, gateway p95 below 200 ms, and inventory with the highest CPU % of its limit. These agree with docs/BASELINE.md.
7. Failure visibility (no new tooling): with docker compose stop payments, within 30 seconds the dashboards show payments down (up = 0), 5xx/504 on orders and the gateway, and WARNING/ERROR logs from orders; after docker compose start payments everything recovers.
8. Overhead: all new containers stay inside their limits during the load test; their CPU and memory are recorded in docs/OBSERVABILITY.md; the 5-minute load test still passes (0 failures, p95 below 500 ms) with the stack running, and the p95 is recorded next to the M1 baseline.
9. scripts/observability_check.py passes; docs/OBSERVABILITY.md is complete; a fresh clone works with the README quick start (README gets a short "Observability" section).

KNOWN RISKS TO HANDLE
- cAdvisor on Docker Desktop (see above). Alloy and the Docker socket on Docker Desktop. Healthcheck commands that do not exist in an image. Config edits need a container restart or reload. The "service" label conflict. Label cardinality (never request ids as labels). Images that fail to pull (tell me, I will report the error).
- You cannot run Docker. Say clearly which parts you could not run. Validate what you can natively (YAML and JSON syntax, promtool or equivalent for the Prometheus config, python -m json.tool for dashboards) and write the verification commands so that I can prove each result on my machine.

WORKING METHOD: 5 CHECKPOINTS
After each checkpoint STOP. Print: (1) files created or changed, one line each on what it does, (2) exact PowerShell verification commands, (3) the expected output, (4) any assumption you made, (5) a ready-to-paste update for PROJECT_CONTEXT.md sections 7 (module status), 8 (decision log), 9 (current focus: which checkpoint is next) and 10 (known issues), (6) a suggested git commit message. Deliver changed files as a zip of only the new and changed files, laid out for the repo root. Then wait for me to reply "continue".
CP1: compose profile skeleton, Prometheus scraping the 4 app services and itself, README stub, verification of targets and key PromQL (DoD 2 partly).
CP2: cAdvisor, container series with the service label, verification for all 6 containers (DoD 3). If it fails, stop and report as described above.
CP3: Loki and Alloy, labels, JSON parsing, request-id search across services (DoD 4).
CP4: Grafana provisioning, the Overview dashboard, GRAFANA_ADMIN_PASSWORD, override ports (DoD 5, DoD 6 for the Overview).
CP5: Service Detail dashboard, scripts/observability_check.py, docs/OBSERVABILITY.md, README section, the failure-visibility and overhead checks, final PROJECT_CONTEXT.md update including sections 2b (standard commands), 3 (ports table), 6 (repo structure) (DoD 1, 7, 8, 9).
Start with CP1 now.
