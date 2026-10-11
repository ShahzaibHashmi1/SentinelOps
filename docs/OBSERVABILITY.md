# SentinelOps observability (module M2)

**Status:** M2 complete: `scripts/observability_check.py` passes (29/29) and Definition of Done 1 to 9 are verified on the owner's Docker Desktop (see section 9).
**Last updated:** 2026-10-11

This file is the **contract** that later modules rely on: M4 (detection) and M12 (evaluation) query Prometheus, M5 (evidence builder) queries Loki, M10 (SentinelOps dashboard) reuses the queries in section 4. Do not change a label name or a service value without updating this file and the dashboards.

## 1. Start, stop, reset

| Task | Command (PowerShell, repo root) |
|------|--------------------------------|
| Start M1 only (6 containers) | `docker compose up -d --build --wait` |
| Start M1 + observability (11 containers) | `docker compose --profile observability up -d --build --wait` |
| Stop (data kept) | `docker compose --profile observability down` |
| Stop and delete ALL data | `docker compose --profile observability down -v` |
| Verify the stack | `python scripts/observability_check.py` |

`docker compose down -v` deletes the named volumes `pgdata` (shop database), `prometheus-data` (7 days of metrics), `loki-data` (7 days of logs) and `grafana-data` (Grafana database and admin password). Without `--profile observability`, `down` leaves the observability containers running if they were started with the profile.

`loki` and `alloy` have no Compose healthcheck (their images contain no shell, wget or curl), so `docker compose ps` shows them as `Up` without `(healthy)`, and `--wait` only waits for them to be running. Loki needs about 15 s after start until `/ready` returns 200.

## 2. Components, ports, limits

| Service | Image (pinned) | Host URL (127.0.0.1 only) | URL inside the Docker network | CPU / memory limit | Healthcheck |
|---------|----------------|---------------------------|-------------------------------|--------------------|-------------|
| prometheus | `prom/prometheus:v3.15.0` | http://127.0.0.1:9090 | http://prometheus:9090 | 0.5 CPU / 512m | wget `/-/ready` |
| loki | `grafana/loki:3.7.8` | http://127.0.0.1:3100 | http://loki:3100 | 0.5 CPU / 512m | none |
| alloy | `grafana/alloy:v1.20.1` | http://127.0.0.1:12345 (override file only) | http://alloy:12345 | 0.25 CPU / 256m | none |
| cadvisor | `ghcr.io/google/cadvisor:v0.60.6` | http://127.0.0.1:8081 (override file only) | http://cadvisor:8080 | 0.25 CPU / 256m | wget `/healthz` |
| grafana | `grafana/grafana:13.2.3` | http://127.0.0.1:3000 | http://grafana:3000 | 0.5 CPU / 512m | curl `/api/health` |

M1 services inside the network: `gateway:8000` (also published on 127.0.0.1:8000), `orders:8001`, `payments:8002`, `inventory:8003`, `postgres:5432`, `redis:6379`. The override file `docker-compose.override.yml` also publishes orders, payments and inventory on 127.0.0.1 for debugging. All new containers carry the labels `sentinelops.service=<name>` and `sentinelops.tier=observability`. No new container sends telemetry to the internet (Grafana usage reporting, update checks, news feed and plugin preinstall are off; Alloy runs with `--disable-reporting`; Loki usage reporting is off).

**Grafana login:** user `admin`, password `GRAFANA_ADMIN_PASSWORD` from `.env` (default `change_me_dev_only`). The password only applies when the `grafana-data` volume is created. Change it later with
`docker compose --profile observability exec grafana grafana cli admin reset-admin-password <new>` (no `$` in the value).

## 3. Label contract

### 3.1 The service values

`service` is one of: `gateway`, `orders`, `payments`, `inventory` (app tier) and `postgres`, `redis` (infra tier). The same six values appear in the app metrics (`service` label), the container metrics (`service` label) and the Loki streams (`service` label). The dependency graph is in `docs/topology.yaml`.

### 3.2 Prometheus

| Series | Labels you can rely on | How to select a service | Notes |
|--------|------------------------|-------------------------|-------|
| `up` | `job`, `instance` | `up{job="orders"}` | job = service name, for gateway, orders, payments, inventory, cadvisor, prometheus. postgres and redis have no scrape target. |
| `http_requests_total` | `service`, `method`, `route`, `status` | `{service="orders"}` | exactly as exposed by the service; `route` is the route template |
| `http_request_duration_seconds_bucket / _sum / _count` | `service`, `route`, `le` | `{service="orders"}` | histogram |
| `http_requests_in_flight` | `job` (no `service` label) | `{job="orders"}` | |
| `dependency_requests_total` | `dependency`, `outcome`, `job` (no `service` label) | `{job="orders"}` | dependency: postgres, redis or a downstream service name |
| `dependency_request_duration_seconds_bucket` | `dependency`, `le`, `job` | `{job="orders"}` | |
| `db_pool_connections_in_use`, `db_pool_connections_max` | `job` | `{job="orders"}` | |
| `app_info` | `service`, `version` | `{service="orders"}` | |
| `container_cpu_usage_seconds_total` | `service`, `job="cadvisor"` | `{service="orders"}` | counter: `sum by (service) (rate(...[1m]))` |
| `container_spec_cpu_quota`, `container_spec_cpu_period` | `service` | `{service="orders"}` | CPU limit in cores = quota / period; use `max by (service)` |
| `container_memory_working_set_bytes`, `container_spec_memory_limit_bytes` | `service` | `{service="orders"}` | gauges: `max by (service)` |
| `container_network_receive_bytes_total`, `container_network_transmit_bytes_total` | `service` | `{service="orders"}` | |
| `container_start_time_seconds` | `service` | `{service="orders"}` | changes when a container restarts: the restart signal (cAdvisor has no restart counter) |

cAdvisor only exports the six M1 containers with the `service` label; series of all other containers are dropped by `metric_relabel_configs`, so the observability containers themselves cannot be seen in Prometheus (measure them with `docker stats`, section 9.3).

### 3.3 The "service" label gotcha

The M1 metrics already carry a label named `service`. **Never add a target label called `service`** in `scrape_configs` or `relabel_configs`: Prometheus would rename the metric's own label to `exported_service`. The scrape job name (`job`) is the service name instead. `scripts/observability_check.py` fails if any series has an `exported_service` label.

### 3.4 Loki

Stream labels (low cardinality only): `service`, `container`, `tier`, `level`.

- `tier` is `app` or `infra`. `level` exists only for the four app services (taken from the JSON field `level`); postgres and redis logs are plain text and have no `level`.
- **Never** make `request_id`, `path` or `event` a label. They stay inside the log line.
- App log lines are one-line JSON with the fields `ts`, `level`, `service`, `version`, `request_id`, `event`, `method`, `path`, `status`, `duration_ms`, `error`.
- Read JSON fields with explicit extraction: `| json request_id="request_id"`. A plain `| json` renames the JSON fields `service` and `level` to `service_extracted` and `level_extracted` because they clash with the stream labels.
- Collected containers: those with `sentinelops.tier` = `app` or `infra`, except the services `loadtest` and `tests` and the observability tier.
- Trace one request through all four app services:
  ```logql
  {service=~"gateway|orders|payments|inventory"} |= "<request-id>" | json request_id="request_id" | request_id="<request-id>"
  ```

## 4. Query catalogue

Every query below is used by a dashboard panel and is copied from the dashboard JSON files by the generator, so this list matches `observability/grafana/dashboards/`. PromQL syntax of all 43 Prometheus queries was parsed offline; execution against real data is verified by the owner (section 9). Conventions (decision D37): 1-minute rate windows; the routes `/health`, `/ready` and `/metrics` are excluded from traffic panels; 5xx % uses `or ... * 0` so services without errors show 0; metrics without a `service` label are selected by `job`. `$service` and `$request_id` are Grafana variables of the Service Detail dashboard.

### Dashboard 1: SentinelOps Overview (`sentinelops-overview`)

- **Services up (of 4)** (stat): Number of app services (gateway, orders, payments, inventory) whose Prometheus scrape target is up.

  ```promql
  count(up{job=~"gateway|orders|payments|inventory"} == 1) or vector(0)
  ```
- **Gateway requests/s** (stat): Requests per second handled by the gateway over the last minute, without /health, /ready and /metrics.

  ```promql
  sum(rate(http_requests_total{service="gateway",route!~"/(health|ready|metrics)"}[1m]))
  ```
- **Gateway error %** (stat): Share of gateway requests answered with a 5xx status over the last minute (5xx / all requests).

  ```promql
  100 * (sum(rate(http_requests_total{service="gateway",status=~"5..",route!~"/(health|ready|metrics)"}[1m])) or vector(0)) / clamp_min(sum(rate(http_requests_total{service="gateway",route!~"/(health|ready|metrics)"}[1m])), 0.001)
  ```
- **Gateway p95 latency** (stat): 95th percentile of the gateway request duration over the last minute (95% of requests were faster). Shows "-" when the gateway received no requests in the last minute.

  ```promql
  histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="gateway",route!~"/(health|ready|metrics)"}[1m]))) and on() (sum(rate(http_request_duration_seconds_count{service="gateway",route!~"/(health|ready|metrics)"}[1m])) > 0)
  ```
- **Avg CPU % of limit (app)** (stat): Average over the 4 app containers of CPU use as a percentage of each container's own CPU limit (1-minute rate).

  ```promql
  avg(100 * sum by (service) (rate(container_cpu_usage_seconds_total{service=~"gateway|orders|payments|inventory"}[1m])) / (max by (service) (container_spec_cpu_quota{service=~"gateway|orders|payments|inventory"}) / max by (service) (container_spec_cpu_period{service=~"gateway|orders|payments|inventory"})))
  ```
- **Avg memory % of limit (app)** (stat): Average over the 4 app containers of the memory working set as a percentage of each container's memory limit.

  ```promql
  avg(100 * max by (service) (container_memory_working_set_bytes{service=~"gateway|orders|payments|inventory"}) / max by (service) (container_spec_memory_limit_bytes{service=~"gateway|orders|payments|inventory"}))
  ```

**Row: Health at a glance**

- **Service health** (table): One row per service: scrape up/down, requests/s, 5xx %, p95 latency (app services with traffic only) and container CPU and memory as % of limit. "-" means no value (no traffic, or the service has no metrics target).
  - query A:

    ```promql
    max by (service) (label_replace(up{job=~"gateway|orders|payments|inventory"}, "service", "$1", "job", "(.*)"))
    ```
  - query B:

    ```promql
    sum by (service) (rate(http_requests_total{route!~"/(health|ready|metrics)"}[1m]))
    ```
  - query C:

    ```promql
    100 * (sum by (service) (rate(http_requests_total{status=~"5..",route!~"/(health|ready|metrics)"}[1m])) or sum by (service) (rate(http_requests_total{route!~"/(health|ready|metrics)"}[1m])) * 0) / clamp_min(sum by (service) (rate(http_requests_total{route!~"/(health|ready|metrics)"}[1m])), 0.001)
    ```
  - query D:

    ```promql
    histogram_quantile(0.95, sum by (le, service) (rate(http_request_duration_seconds_bucket{route!~"/(health|ready|metrics)"}[1m]))) and on(service) (sum by (service) (rate(http_request_duration_seconds_count{route!~"/(health|ready|metrics)"}[1m])) > 0)
    ```
  - query E:

    ```promql
    100 * sum by (service) (rate(container_cpu_usage_seconds_total[1m])) / (max by (service) (container_spec_cpu_quota) / max by (service) (container_spec_cpu_period))
    ```
  - query F:

    ```promql
    100 * max by (service) (container_memory_working_set_bytes) / max by (service) (container_spec_memory_limit_bytes)
    ```

**Row: Resource usage**

- **CPU % of limit per container** (timeseries): CPU use of each of the 6 containers as a percentage of its own CPU limit (1-minute rate).

  ```promql
  100 * sum by (service) (rate(container_cpu_usage_seconds_total[1m])) / (max by (service) (container_spec_cpu_quota) / max by (service) (container_spec_cpu_period))
  ```
- **Memory working set per container** (timeseries): Memory working set (memory in active use, as reported by cAdvisor) of each of the 6 containers.

  ```promql
  max by (service) (container_memory_working_set_bytes)
  ```

**Row: Traffic**

- **Requests/s per service** (timeseries): Requests per second handled by each app service, without /health, /ready and /metrics.

  ```promql
  sum by (service) (rate(http_requests_total{route!~"/(health|ready|metrics)"}[1m]))
  ```
- **Error rate per service (5xx %)** (timeseries): Share of each app service's requests answered with a 5xx status (1-minute window).

  ```promql
  100 * (sum by (service) (rate(http_requests_total{status=~"5..",route!~"/(health|ready|metrics)"}[1m])) or sum by (service) (rate(http_requests_total{route!~"/(health|ready|metrics)"}[1m])) * 0) / clamp_min(sum by (service) (rate(http_requests_total{route!~"/(health|ready|metrics)"}[1m])), 0.001)
  ```
- **In-flight requests per service** (timeseries): Requests currently being handled by each app service (gauge exposed by the service).

  ```promql
  sum by (job) (http_requests_in_flight{job=~"gateway|orders|payments|inventory"})
  ```
- **Latency p50 per service** (timeseries): p50 request duration of each app service (1-minute window, without /health, /ready and /metrics).

  ```promql
  histogram_quantile(0.5, sum by (le, service) (rate(http_request_duration_seconds_bucket{route!~"/(health|ready|metrics)"}[1m])))
  ```
- **Latency p95 per service** (timeseries): p95 request duration of each app service (1-minute window, without /health, /ready and /metrics).

  ```promql
  histogram_quantile(0.95, sum by (le, service) (rate(http_request_duration_seconds_bucket{route!~"/(health|ready|metrics)"}[1m])))
  ```
- **Latency p99 per service** (timeseries): p99 request duration of each app service (1-minute window, without /health, /ready and /metrics).

  ```promql
  histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket{route!~"/(health|ready|metrics)"}[1m])))
  ```

**Row: Dependencies**

- **Dependency calls/s by outcome** (timeseries): Calls per second from the app services to postgres, redis and other services, split by outcome (success, error, timeout).

  ```promql
  sum by (dependency, outcome) (rate(dependency_requests_total{job=~"gateway|orders|payments|inventory"}[1m]))
  ```
- **Dependency p95 latency** (timeseries): 95th percentile duration of calls to each dependency (postgres, redis, downstream services), 1-minute window.

  ```promql
  histogram_quantile(0.95, sum by (le, dependency) (rate(dependency_request_duration_seconds_bucket{job=~"gateway|orders|payments|inventory"}[1m])))
  ```
- **DB pool: in use vs max per service** (timeseries): Postgres pool connections checked out (solid) against the configured pool maximum (dashed) for each service.
  - query A:

    ```promql
    max by (job) (db_pool_connections_in_use{job=~"gateway|orders|payments|inventory"})
    ```
  - query B:

    ```promql
    max by (job) (db_pool_connections_max{job=~"gateway|orders|payments|inventory"})
    ```

**Row: Logs**

- **Recent WARNING and ERROR logs** (logs): Latest WARNING and ERROR lines of the app services (level label), plus error-like lines of postgres and redis (text match, their logs are not JSON).
  - query A:

    ```logql
    {service=~"gateway|orders|payments|inventory", level=~"WARNING|ERROR"}
    ```
  - query B:

    ```logql
    {service=~"postgres|redis"} |~ "(?i)(error|fatal|panic|warning)"
    ```
- **Log volume by level** (timeseries): Log lines per second of the 4 app services by level (INFO, WARNING, ERROR), 1-minute window.

  ```logql
  sum by (level) (rate({service=~"gateway|orders|payments|inventory"}[1m]))
  ```

### Dashboard 2: SentinelOps Service Detail (`sentinelops-service-detail`)

- **Up** (stat): Prometheus scrape status of the selected service: UP when the last scrape of its /metrics succeeded.

  ```promql
  max(up{job="$service"})
  ```
- **Requests/s** (stat): Requests per second handled by the selected service over the last minute, without /health, /ready and /metrics.

  ```promql
  sum(rate(http_requests_total{service="$service",route!~"/(health|ready|metrics)"}[1m]))
  ```
- **Error % (5xx)** (stat): Share of the selected service's requests answered with a 5xx status over the last minute (5xx / all requests).

  ```promql
  100 * (sum(rate(http_requests_total{service="$service",status=~"5..",route!~"/(health|ready|metrics)"}[1m])) or vector(0)) / clamp_min(sum(rate(http_requests_total{service="$service",route!~"/(health|ready|metrics)"}[1m])), 0.001)
  ```
- **p95 latency** (stat): p95 request duration of the selected service over the last minute; shows "-" when it received no requests in that minute.

  ```promql
  histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="$service",route!~"/(health|ready|metrics)"}[1m]))) and on() (sum(rate(http_request_duration_seconds_count{service="$service",route!~"/(health|ready|metrics)"}[1m])) > 0)
  ```
- **CPU % of limit** (stat): CPU use of the selected service's container as a percentage of its own CPU limit (1-minute rate).

  ```promql
  100 * sum(rate(container_cpu_usage_seconds_total{service="$service"}[1m])) / (max(container_spec_cpu_quota{service="$service"}) / max(container_spec_cpu_period{service="$service"}))
  ```
- **Memory % of limit** (stat): Memory working set of the selected service's container as a percentage of its memory limit.

  ```promql
  100 * max(container_memory_working_set_bytes{service="$service"}) / max(container_spec_memory_limit_bytes{service="$service"})
  ```

**Row: HTTP routes**

- **Requests/s per route** (timeseries): Requests per second of the selected service per route template, without /health, /ready and /metrics.

  ```promql
  sum by (route) (rate(http_requests_total{service="$service",route!~"/(health|ready|metrics)"}[1m]))
  ```
- **Error % per route (5xx)** (timeseries): Share of each route's requests answered with a 5xx status (1-minute window); 0 when a route has no errors.

  ```promql
  100 * (sum by (route) (rate(http_requests_total{service="$service",status=~"5..",route!~"/(health|ready|metrics)"}[1m])) or sum by (route) (rate(http_requests_total{service="$service",route!~"/(health|ready|metrics)"}[1m])) * 0) / clamp_min(sum by (route) (rate(http_requests_total{service="$service",route!~"/(health|ready|metrics)"}[1m])), 0.001)
  ```
- **Requests/s by status code** (timeseries): Requests per second of the selected service by HTTP status code: shows 502/504 appearing when a dependency fails.

  ```promql
  sum by (status) (rate(http_requests_total{service="$service",route!~"/(health|ready|metrics)"}[1m]))
  ```
- **Latency p95 per route** (timeseries): p95 request duration of the selected service per route (1-minute window, without /health, /ready and /metrics).

  ```promql
  histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket{service="$service",route!~"/(health|ready|metrics)"}[1m])))
  ```
- **Latency p99 per route** (timeseries): p99 request duration of the selected service per route (1-minute window, without /health, /ready and /metrics).

  ```promql
  histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket{service="$service",route!~"/(health|ready|metrics)"}[1m])))
  ```
- **In-flight requests** (timeseries): Requests currently being handled by the selected service (gauge exposed by the service).

  ```promql
  sum(http_requests_in_flight{job="$service"})
  ```

**Row: Dependencies and DB pool**

- **Dependency calls/s by outcome** (timeseries): Calls per second from the selected service to postgres, redis and other services, split by outcome (success, error, timeout).

  ```promql
  sum by (dependency, outcome) (rate(dependency_requests_total{job="$service"}[1m]))
  ```
- **Dependency p95 latency** (timeseries): 95th percentile duration of the selected service's calls to each dependency (1-minute window).

  ```promql
  histogram_quantile(0.95, sum by (le, dependency) (rate(dependency_request_duration_seconds_bucket{job="$service"}[1m])))
  ```
- **DB pool: in use vs max** (timeseries): Postgres pool connections checked out (solid) against the configured pool maximum (dashed) of the selected service.
  - query A:

    ```promql
    max(db_pool_connections_in_use{job="$service"})
    ```
  - query B:

    ```promql
    max(db_pool_connections_max{job="$service"})
    ```

**Row: Container**

- **CPU % of limit** (timeseries): CPU use of the selected service's container as a percentage of its own CPU limit (1-minute rate).

  ```promql
  100 * sum(rate(container_cpu_usage_seconds_total{service="$service"}[1m])) / (max(container_spec_cpu_quota{service="$service"}) / max(container_spec_cpu_period{service="$service"}))
  ```
- **Memory working set vs limit** (timeseries): Memory working set of the selected service's container (solid) against its memory limit (dashed).
  - query A:

    ```promql
    max(container_memory_working_set_bytes{service="$service"})
    ```
  - query B:

    ```promql
    max(container_spec_memory_limit_bytes{service="$service"})
    ```

**Row: Logs**

- **Logs of the selected service (filter: request_id)** (logs): Log lines of the selected service; type a request id in the request_id box at the top to show only its lines (empty = all lines).

  ```logql
  {service="$service"} | json request_id="request_id" | request_id=~".*$request_id.*"
  ```
- **Log volume by level** (timeseries): Log lines per second of the selected service by level (INFO, WARNING, ERROR), 1-minute window.

  ```logql
  sum by (level) (rate({service="$service"}[1m]))
  ```
- **Recent WARNING and ERROR logs of the selected service** (logs): Latest WARNING and ERROR lines of the selected service (level label). postgres and redis are not selectable here: their logs have no level.

  ```logql
  {service="$service", level=~"WARNING|ERROR"}
  ```

## 5. Dashboards

Provisioned from `observability/grafana/dashboards/` into the Grafana folder `SentinelOps` (decision D36); default refresh 5 s, default range last 30 minutes, editable. Changed JSON files are re-read every 10 s and override edits made in the UI. Datasources `prometheus` (default) and `loki` have fixed uids.

| Dashboard | URL | Use |
|-----------|-----|-----|
| SentinelOps Overview | http://127.0.0.1:3000/d/sentinelops-overview | health of all services at a glance |
| SentinelOps Service Detail | http://127.0.0.1:3000/d/sentinelops-service-detail | one service: routes, status codes, dependencies, DB pool, container, logs; variable `service`, optional text variable `request_id` filters the log panel |

Display rules: a value that does not exist is shown as `-` (p95 without traffic, the table columns of postgres and redis); the `Up` cell of postgres and redis is `n/a` without colour because they have no scrape target. The p95 stat panels use an instant query, so they show `-` as soon as the last minute had no requests.

## 6. Operations

### 6.1 Add a scrape target

1. Edit `observability/prometheus/prometheus.yml` and add a job. The job name is the service name; do **not** add a label called `service`:
   ```yaml
   - job_name: detector
     metrics_path: /metrics
     static_configs:
       - targets: ["detector:9100"]
   ```
2. The container must be on the Compose network `sentinelops-net`.
3. Reload: `docker compose --profile observability kill -s SIGHUP prometheus` (decision D26: no `--web.enable-lifecycle`, no HTTP reload).
4. Check: `docker compose --profile observability logs --tail 5 prometheus` shows "Completed loading of configuration file", and http://127.0.0.1:9090/targets lists the job as UP.
5. Add the job name to `APP_TARGETS` in `scripts/observability_check.py` if it must be checked.

### 6.2 Reload or restart after a config change

| Component | After editing | Command |
|-----------|---------------|---------|
| Prometheus | `observability/prometheus/` | `docker compose --profile observability kill -s SIGHUP prometheus` |
| Loki | `observability/loki/loki-config.yml` | `docker compose --profile observability restart loki` |
| Alloy | `observability/alloy/config.alloy` | `docker compose --profile observability restart alloy` |
| Grafana dashboards | `observability/grafana/dashboards/*.json` | nothing (re-read every 10 s) |
| Grafana datasources, `provisioning/` | `observability/grafana/provisioning/` | `docker compose --profile observability restart grafana` |

Config directories are bind-mounted as directories (not single files) because a single-file bind mount on Windows can keep showing the old content after an editor replaces the file.

### 6.3 Retention and storage

Prometheus keeps 7 days (`--storage.tsdb.retention.time=7d`), Loki keeps 7 days (compactor retention). Data lives in the volumes `prometheus-data` and `loki-data`. Alloy does not persist log positions: lines written while Alloy is down may be missed.

## 7. Known limitations

- cAdvisor on Docker Desktop (WSL2) with the containerd image store needs the VM's `/run/containerd/containerd.sock` mounted (decision D28). On other hosts that path may not exist: Docker then creates an empty directory and cAdvisor shows no container series. Adjust or remove the mount there.
- The `docker.sock` and `containerd.sock` mounts give cAdvisor (and `docker.sock` also Alloy) privileged access to the container runtime; `:ro` does not restrict API access. Acceptable only because the stack is local, localhost-only and profile-gated.
- Rates use 1-minute windows: values lag a change by up to a minute and are unreliable during the first 1 to 2 minutes after a start. Use averaging windows of at least 30 s for CPU thresholds (idle app services show about 11 to 13 % of their CPU limit because of the 5-second healthchecks and scrapes). `up` reacts within one scrape (5 s).
- postgres and redis have no scrape target (no `up`, no request metrics); only their container series and logs exist. They log rarely, so right after a start they may have no lines in Loki.
- Alloy shows its components as healthy even when it cannot read the Docker socket; the symptom is "Unable to refresh target groups" in its log.
- With payments or postgres stopped, orders answers 504 `downstream_timeout` after about 2 s (the Docker network gives no fast connection refusal); this is the typical fault signature for M3 to M6.
- Grafana is the container closest to its limit: peak memory 454.5 MiB of 512 MiB during the 5-minute load test with one dashboard open at 5 s refresh (section 9.3). No OOM kill or restart occurred. More open dashboards or heavier queries can exceed the limit.
- While a container is stopped (for example `docker compose stop payments`), cAdvisor has no series for it: the CPU and memory cells of its Overview row show `-`, and `up` is 0.
- There are no alert rules, no Alertmanager and no tracing in M2; incidents come from M4.

## 8. Reference numbers (normal load, 20 users, full stack)

Measured on the owner's Docker Desktop. See `docs/BASELINE.md` for the M1 baseline.

| Measure | Value |
|---------|-------|
| Requests/s at the gateway | about 19 (accepted range 15 to 25) |
| Gateway p95, Locust whole run, full stack | 160 ms |
| Gateway p95, Overview dashboard (1-minute window), full stack | about 203 ms |
| Gateway p95, M1 baseline without the stack | 120 ms |
| Limit for Definition of Done 6 (gateway p95) | 250 ms |
| CPU % of limit, 1-minute average | orders about 44, gateway about 40, inventory about 30, payments about 20 |

## 9. Verification

### 9.1 Automatic check

```powershell
python scripts/observability_check.py
```

Expected: 29 `[OK]` lines (28 without the smoke test), no `[FAIL]`, ending with `OBSERVABILITY CHECK PASSED` and exit code 0 (`$LASTEXITCODE` = 0). Use `--skip-smoke` to leave out the M1 smoke test.

### 9.2 Failure visibility (Definition of Done 7)

Start a load test in window 1, then stop payments in window 2 while the Overview dashboard is open.

```powershell
# window 1
$env:DURATION = '4m'; docker compose --profile load run --rm loadtest; Remove-Item Env:DURATION
```
```powershell
# window 2, after the load test has run for about 90 s
docker compose stop payments
$t0 = Get-Date
do {
  Start-Sleep -Seconds 1
  $r = (Invoke-RestMethod -Uri 'http://127.0.0.1:9090/api/v1/query' -Body @{ query = 'up{job="payments"}' }).data.result
  $v = if ($r) { $r[0].value[1] } else { 'none' }
} while ($v -ne '0' -and ((Get-Date) - $t0).TotalSeconds -lt 30)
"up{job=payments} = $v after $([int]((Get-Date) - $t0).TotalSeconds) s"
```

Expected within 30 s: `up{job=payments} = 0`; on the Overview dashboard the payments row shows `DOWN`, "Gateway error %" and the orders row show 5xx above 0; the Service Detail dashboard (service `orders`) shows status 504 in "Requests/s by status code" and WARNING/ERROR lines in the logs panels. Check the logs from the shell:

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:3100/loki/api/v1/query_range' -Body @{ query = '{service="orders",level=~"WARNING|ERROR"}'; limit = 5 }).data.result | ForEach-Object { $_.values } | ForEach-Object { $_[1] }
```

Recovery:

```powershell
docker compose start payments
```

Within about 30 s `up{job="payments"}` is 1 and the cell is `UP`; the 5xx values return to 0 within about 2 minutes (1-minute windows).

### 9.3 Overhead of the observability containers (Definition of Done 8)

cAdvisor does not export the observability containers (section 3.2), so sample them with `docker stats` during the 5-minute load test (`docker compose --profile load run --rm loadtest` in another window, default `DURATION=5m`). Start this about 20 s after the load test starts:

```powershell
$samples = @()
1..30 | ForEach-Object {
  docker stats --no-stream --format '{{.Name}};{{.CPUPerc}};{{.MemUsage}}' |
    Where-Object { $_ -match 'sentinelops-(prometheus|loki|alloy|cadvisor|grafana)-1;' } |
    ForEach-Object { $samples += $_ }
  Start-Sleep -Seconds 5
}
function ToMiB($s) { $v = [double]($s -replace '[^0-9.]', ''); if ($s -match 'GiB') { $v * 1024 } elseif ($s -match 'KiB') { $v / 1024 } elseif ($s -match 'MiB') { $v } else { $v / 1MB } }
$samples | ForEach-Object {
  $n, $c, $m = $_ -split ';'
  [pscustomobject]@{ Container = ($n -replace '^sentinelops-|-1$', ''); Cpu = [double]($c -replace '%', ''); MemMiB = (ToMiB (($m -split ' / ')[0])) }
} | Group-Object Container | ForEach-Object {
  [pscustomobject]@{
    Container  = $_.Name
    PeakCpuPct = [math]::Round(($_.Group | Measure-Object Cpu -Maximum).Maximum, 1)
    AvgCpuPct  = [math]::Round(($_.Group | Measure-Object Cpu -Average).Average, 1)
    PeakMemMiB = [math]::Round(($_.Group | Measure-Object MemMiB -Maximum).Maximum, 1)
  }
} | Format-Table
```

`docker stats` reports CPU in percent of ONE core (100 % = 1 core), so the limits are: prometheus 50 %, loki 50 %, alloy 25 %, cadvisor 25 %, grafana 50 %; memory limits 512 MiB, 512 MiB, 256 MiB, 256 MiB, 512 MiB. Accept when every peak is below its limit.

Result of the owner's run (2026-10-11), with one browser tab on the Overview dashboard (5 s refresh) open during the sampling:

| Container | CPU limit (% of one core) | Peak CPU % | Average CPU % | Memory limit (MiB) | Peak memory (MiB) |
|-----------|---------------------------|-----------|---------------|--------------------|-------------------|
| prometheus | 50 | 13.7 | 2.2 | 512 | 57.0 |
| loki | 50 | 2.2 | 1.3 | 512 | 86.7 |
| alloy | 25 | 5.8 | 4.2 | 256 | 158.3 |
| cadvisor | 25 | 3.6 | 1.5 | 256 | 29.4 |
| grafana | 50 | 38.7 | 4.8 | 512 | 454.5 |

5-minute load test with the stack running (Locust output, `loadtest/results/run_stats.csv`): failures must be 0 and p95 below 500 ms.

| Run | Failures | Gateway p95 (ms) |
|-----|----------|------------------|
| M1 baseline, no stack (`docs/BASELINE.md`) | 0 | 120 |
| M2 full stack, 5 minutes | 0 | 160 |

The 5-minute run with the full stack: 5654 requests, 18.9 requests/s, 0 failures. After the run `docker inspect` showed `OOMKilled=false` and `RestartCount=0` for all five observability containers. All peaks are below their limits (Definition of Done 8).
