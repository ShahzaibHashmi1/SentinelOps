# SentinelOps: Chaos / fault injection (module M3)

M3 injects 8 faults into the running M1 stack and writes a ground-truth log of every experiment. Later modules use it: M4 to M6 must detect and explain the faults, M8 must repair them, M12 scores them against the ground truth. M3 changes no M1 service code and adds no container.

- Tool: `chaos/chaos.py` (standard library only, runs on the host with the host Python), catalogue: `chaos/catalogue.json`, fault code: `chaos/faults.py`
- Verification: `scripts/chaos_check.py` (section 10), unit tests: `python -m unittest discover -s chaos/tests`
- Needs the six M1 containers running and healthy. The symptoms below are read from Prometheus and Loki, so the observability profile is needed to see them (not to inject them).

## 1. Quick start (PowerShell, repo root)

```powershell
docker compose --profile observability up -d --build --wait
python chaos/chaos.py list
python chaos/chaos.py inject F10
python chaos/chaos.py status
python chaos/chaos.py clear F10
python chaos/chaos.py clear --all
```

`run` does warm-up, inject, hold, clear and cool-down as one labelled experiment: `python chaos/chaos.py run F11 --warmup 60 --hold 120 --cooldown 60`.

## 2. Commands

| Command | What it does | Exit code |
|---------|--------------|-----------|
| `list` | the catalogue: id, category, target, expected root cause, expected action, built | 0 |
| `status` | active faults with age and state (`active` or `stale`) | 0 |
| `inject ID [--target T] [--duration S] [--param name=value]` | injects one fault and returns when the fault is verified | 0 ok, 1 failed (the tool undoes a failed inject), 2 refused |
| `clear ID` | undoes one fault, waits until the stack is healthy, writes `clear` and `recovered` | 0 ok, 1 failed, 2 refused |
| `clear --all` | repairs everything, with or without a record (section 7) | 0 healthy, 1 not healthy or a sweep could not be completed |
| `run ID [--warmup S] [--hold S] [--cooldown S] [--duration S] [--target T] [--param ...]` | one labelled experiment (defaults 60 s warm-up, hold = the fault's duration else 60 s, 60 s cool-down) | 0 ok, 1 failed, 2 refused, 130 interrupted |

- `run` for a self-ending fault (F01, F02, F11, F18) uses `duration = hold + 30 s` unless `--duration` is given, so that the clear and not the timer ends it.
- Ctrl+C during `run`: the first press aborts and clears; further presses during the clear are held back until the events are written; exit code 130.
- Faults that stay until clear (F06, F08, F10, F13) reject `--duration`; use `run --hold` for a timed experiment.
- `status` prints one line per stale fault that says what "stale" means for it (section 9).

### Parameters and limits

| Fault | Parameter (default, min to max) | Duration (default, cap) |
|-------|--------------------------------|-------------------------|
| F01 | `processes` (2, 1 to 4) | 180 s, 600 s |
| F02 | `step_mib` (8, 1 to 32), `interval_s` (2, 1 to 10) | 240 s, 600 s |
| F06, F08, F10, F13 | none | stays until clear |
| F11 | none | 120 s, 600 s |
| F18 | `users` (100, 21 to 150), `spawn_rate` (20, 1 to 50) | 120 s, 600 s |

No fault, warm-up, hold or cool-down is longer than 600 s.

## 3. Catalogue

| ID | Category | Target (allowed) | Injection | Clear | Expected root cause | Expected action (acceptable) |
|----|----------|------------------|-----------|-------|---------------------|------------------------------|
| F01 CPU saturation | Resource | orders | `docker compose exec -d` starts busy-loop Python processes (marker `chaos-F01-proc`) | stop file in `/tmp`, wait until no marker process is left | orders / cpu_exhaustion | restart_service (restart_service, scale_service) |
| F02 Memory growth | Resource | payments | `exec -d` starts an allocator that grows in steps up to 90 % of the limit read with `docker inspect`, then holds | stop file, wait until the process is gone | payments / memory_exhaustion | restart_service |
| F06 Missing environment variable | Configuration | inventory (orders, payments, inventory) | generated override file `chaos/overrides/F06-<service>.yml` removes DATABASE_URL (`!reset null`); the service is recreated and crash-loops | recreate from the base files with `--force-recreate`, delete the override file | target / misconfiguration | revert_config |
| F08 Invalid database credential | Configuration | orders (orders, payments, inventory) | override file sets a wrong password in DATABASE_URL (marker `chaos-wrong-password-F08`, not a secret); the service recreates, stays healthy, its queries fail | recreate from the base files, delete the override file | target / db_auth_failure | revert_config |
| F10 PostgreSQL down | Dependency | postgres | `docker compose stop postgres` | `docker compose start postgres`, wait until healthy | postgres / database_unavailable | restart_service |
| F11 Lock contention | Dependency | postgres | detached `psql` (application name `chaos-F11`) takes an ACCESS EXCLUSIVE lock on `items` and sleeps | `pg_terminate_backend` of the `chaos-F11` session, verified gone | postgres / db_lock_contention | terminate_blocking_query |
| F13 Redis down | Dependency | redis | `docker compose stop redis` | `docker compose start redis`, wait until healthy | redis / cache_unavailable | restart_service |
| F18 Traffic spike | Load | gateway | M1 load test image in its own container `sentinelops-chaos-load` with more users than the baseline of 20 | remove that container by id | none (null) / capacity_limit | scale_service |

`expected_root_cause.service` is the target for F01, F02, F06 and F08, and `null` for F18.

## 4. Expected symptoms and measured values

`inject` returns only after its own verification. Every limit `within_s` counts from the moment `inject` returned. An empty query result counts as 0. The queries below are the ones in `chaos/catalogue.json` (`$target` is the target service). Values above 100 occur in the CPU-of-limit query: never assume a maximum of 100.

| Fault | Source | Query | Condition within | Measured on the owner's machine (10 load-test users unless stated) |
|-------|--------|-------|------------------|-------------------------------------------------------------------|
| F01 | Prometheus | `100 * sum(rate(container_cpu_usage_seconds_total{service="$target"}[1m])) / (max(container_spec_cpu_quota{service="$target"}) / max(container_spec_cpu_period{service="$target"}))` | `> 85` within 90 s | baseline 24.7; 35.5 at 10 s, 56.7 at 20 s, 67.2 at 30 s, 86.9 at 40 s, 95.9 at 60 s, 104.0 at 90 s, 110.1 at 100 s |
| F02 | Prometheus | `100 * max(container_memory_working_set_bytes{service="$target"}) / max(container_spec_memory_limit_bytes{service="$target"})` | `> 80` within 120 s | baseline 23.9; 36.2 at 10 s, 50.1 at 20 s, 73.8 at 30 s, 87.0 at 40 s, then 86.3 to 86.5 until 80 s |
| F06 | Prometheus | `min(up{job="$target"})` | `== 0` within 30 s | 0 at every reading from the first on (at least 13 s after the recreate) |
| F06 | Loki | `sum(count_over_time({service="$target"} \|~ "(?i)required" [2m]))` | `> 0` within 60 s | 7 (inventory), 9 (payments); log text `DATABASE_URL is required for this service` |
| F08 | Prometheus | `sum(rate(dependency_requests_total{job="$target",dependency="postgres",outcome!="success"}[1m]))` | `> 0` within 60 s | empty before the fault; 0.41 per s at 10 s; 1.85 to 1.93 per s from 60 s |
| F08 | Prometheus | gateway 5xx % (query A below) | `> 5` within 90 s | baseline 0; 9.1 at 10 s, 13.0 at 20 s, 18.7 at 30 s, 26.3 at 40 s, 36.0 at 50 s, 32.7 to 36.6 from 60 s |
| F08 | Loki | `sum(count_over_time({service="$target"} \|~ "(?i)(password authentication\|authentication failed)" [2m]))` | `> 0` within 60 s | 56 in orders and 56 in postgres about 100 s after the inject |
| F10 | Prometheus | `count(container_memory_working_set_bytes{service="postgres"}) or vector(0)` | `== 0` within 30 s | 0 within 10 s |
| F10 | Prometheus | gateway 5xx % (query A) | `> 5` within 90 s | 10.5 at 10 s, 100 from 80 s |
| F10 | Loki | `sum(count_over_time({service=~"orders\|inventory", level=~"WARNING\|ERROR"} [2m]))` | `> 0` within 60 s | rising |
| F11 | Prometheus | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="inventory",route!~"/(health\|ready\|metrics)"}[1m])))` | `> 1` within 90 s | baseline 0.0115 s; 1.67 s rising to 2.43 s (the 2 s pool timeout) |
| F11 | Prometheus | gateway 5xx % (query A) | `> 5` within 90 s | baseline 0; 12.0, 24.4, 42.8, 70.9, 93.0, 95.1, 95.8, 96.2 in 10 s steps |
| F13 | Prometheus | `count(container_memory_working_set_bytes{service="redis"}) or vector(0)` | `== 0` within 30 s | 0 within 10 s |
| F13 | Prometheus | `sum(rate(dependency_requests_total{dependency="redis",outcome!="success"}[1m]))` | `> 0` within 60 s | about 4.5 per s |
| F13 | Loki | `sum(count_over_time({service=~"inventory\|payments"} \|~ "(?i)redis" [2m]))` | `> 0` within 60 s | rising |
| F18 | Prometheus | `sum(rate(http_requests_total{service="gateway",route!~"/(health\|ready\|metrics)"}[1m]))` | `> 30` within 90 s | plateau about 38 per s (peak 42.1) with 100 users and no other load; above 30 after 50 s in both cases (with no other load, and in the check run with the 10-user background load of `chaos_check.py`, value 31.77 at 50 s); the limit was widened from 60 s to 90 s because 60 s left only 10 s of margin |
| F18 | Prometheus | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="gateway",route!~"/(health\|ready\|metrics)"}[1m])))` | `> 0.3` within 90 s | 1.69 s, then 2.96 s, then 4.4 to 4.5 s |

**Query A** (gateway 5xx %): `100 * (sum(rate(http_requests_total{service="gateway",status=~"5..",route!~"/(health|ready|metrics)"}[1m])) or vector(0)) / clamp_min(sum(rate(http_requests_total{service="gateway",route!~"/(health|ready|metrics)"}[1m])), 0.001)`

In the table the pipe characters inside queries are escaped as `\|` only for Markdown; the real queries contain `|`.

Notes per fault:

- **F13**: the services keep serving in degraded mode. The gateway 5xx stays at 0.0 % (90 s observed). Detect F13 with dependency errors (about 4.5 per s), the missing redis container series, logs and `/ready`, not with the error rate.
- **F10**: the gateway 5xx rate reaches 10.5 % at 10 s and 100 % from 80 s with 10 users. With postgres down, POST /api/orders returns 504 or 502, depending on which 2 s timeout fires first.
- **F08**: `docker compose ps` shows the container `running` and `healthy` for the whole fault (the health check is liveness only). Only `chaos/data/active.json` and the container environment show it.
- **F06**: Docker doubles the delay between restarts; the restart count was 2 at about 13 s and 7 thirty seconds later. A stopped container's cAdvisor series disappear; use `up` and `http_*` series for stopped services.
- **F02**: the memory limit is a soft ceiling: measured container usage was 240 MiB of 256 MiB (the ceiling of 230.4 MiB is checked once before each step and the container usage also contains file cache and the service's own memory). `OOMKilled` stayed false and the restart count 0.
- The 1-minute rate windows read the previous minute. Wait about 100 s between two faults before reading a baseline (right after F06 on payments the gateway 5xx baseline was 17.7 %).

### Recovery after a clear

| Fault | Normal clear | Late clear |
|-------|--------------|------------|
| F01 | 3.1 to 5.1 s | 2.5 s; 5.3 s after a manual restart |
| F02 | 2.4 s | |
| F06 | 8.7 to 10.8 s | 8.6 and 12.1 s |
| F08 | 10.3 to 13.9 s | 12.1 s |
| F10, F13 | 7.7 s and 7.8 s | F10 10.1 s |
| F11 | 1.6 to 2.0 s (6 clears, late or not) | included |

## 5. Ground truth

File `chaos/data/ground_truth.jsonl`: append-only, UTF-8, one JSON object per line, `\n` line ends. Active state is in `chaos/data/active.json`. Both are git-ignored.

**`chaos/data` holds events from verification runs. Delete the folder before real evaluation runs**, so that the ground truth contains only real experiments.

### Fields

| Field | Meaning |
|-------|---------|
| `run_id` | `<fault id>-<8 hex characters>`; the same for all events of one experiment |
| `event` | `inject`, `clear`, `recovered` or `aborted` |
| `fault_id`, `category`, `target`, `params` | what was injected, where, with which parameters (defaults filled in) |
| `ts` | UTC, ISO 8601 with milliseconds and `Z`, for example `2026-10-16T09:12:04.318Z` |
| `expected_root_cause` | `{"service": ..., "type": ...}`; the service is the target, a fixed service, or `null` (F18) |
| `expected_action`, `acceptable_actions` | the repair M8 should choose, and the actions that also count as correct |
| `recovery_s` | only on `recovered`: seconds from the clear `ts` to the recovered `ts` |
| `reason` | only on `aborted` (`interrupted by Ctrl+C`) |
| `late` | only on `clear` and `recovered`, always `true` when present (see below) |

### Example lines

```json
{"run_id":"F10-3f9a1c2e","event":"inject","fault_id":"F10","category":"Dependency","target":"postgres","params":{},"ts":"2026-10-16T09:12:04.318Z","expected_root_cause":{"service":"postgres","type":"database_unavailable"},"expected_action":"restart_service","acceptable_actions":["restart_service"]}
{"run_id":"F10-3f9a1c2e","event":"clear","fault_id":"F10","category":"Dependency","target":"postgres","params":{},"ts":"2026-10-16T09:14:04.410Z","expected_root_cause":{"service":"postgres","type":"database_unavailable"},"expected_action":"restart_service","acceptable_actions":["restart_service"]}
{"run_id":"F10-3f9a1c2e","event":"recovered","fault_id":"F10","category":"Dependency","target":"postgres","params":{},"ts":"2026-10-16T09:14:12.118Z","expected_root_cause":{"service":"postgres","type":"database_unavailable"},"expected_action":"restart_service","acceptable_actions":["restart_service"],"recovery_s":7.7}
{"run_id":"F01-8d27b0aa","event":"aborted","fault_id":"F01","category":"Resource","target":"orders","params":{"processes":2},"ts":"2026-10-16T09:20:31.002Z","expected_root_cause":{"service":"orders","type":"cpu_exhaustion"},"expected_action":"restart_service","acceptable_actions":["restart_service","scale_service"],"reason":"interrupted by Ctrl+C"}
```

(The example values are illustrative; field names, field order and formats are exact.) A late clear carries `"late":true` after `acceptable_actions` on the `clear` line and after `recovery_s` on the `recovered` line (`late` comes last on `recovered`: `..."recovery_s":8.6,"late":true`).

### Meaning of each timestamp

| Event | `ts` is taken |
|-------|---------------|
| `inject` | immediately before the first docker action of the injection (so it is at or before the first effect) |
| `clear` | after the read-only observation commands (`docker compose ps`, process count) and before the first command that changes the stack |
| `recovered` | when the stack is healthy again: six containers healthy and the gateway answers; `recovery_s` is measured from the `clear` `ts` to this `ts` |
| `aborted` | when Ctrl+C interrupted `run` (it is followed by `clear` and `recovered`) |

### Rules for M4 and M12

- **Order events by their position in the file, never by `ts`.** `ts` values can be equal (for example `clear` and `recovered` of a very fast recovery).
- A normal experiment is `inject`, `clear`, `recovered` with one `run_id`. `aborted` appears between `inject` and `clear` when Ctrl+C ended a `run`.
- **M12 must exclude events with `"late": true` from MTTR.** A late clear means the effect was already gone before the clear (self-ending fault whose duration was over, container restarted or recreated by hand, session ended by hand): the time from the `clear` `ts` to `recovered` does not measure a repair.
- A `clear` can appear long after its `inject`, for example when an earlier clear was interrupted; it is then late.
- Time to detect (M4) is measured from the `inject` `ts`; time to repair (M8, M12) from the `inject` `ts` to the `recovered` `ts` of an experiment without the `late` flag. `recovery_s` alone is only the duration of the clear itself.
- The file masks password patterns only (URL passwords, `NAME_PASSWORD=value`). No password is in the file; `scripts/chaos_check.py` checks this.

## 6. Safety rules

- The CLI refuses (exit code 2, text on stderr, no docker action): an unknown fault, a target that is not allowed for the fault, a duration above the cap of the fault, a duration for a fault that has none, an unknown parameter or a value outside its range, a second inject of an active fault (also when its record is stale), and an inject while the stack is not running and healthy.
- Only the six M1 services and only these actions: stop/start (F10, F13), `exec` in orders or payments (F01, F02), `exec` of `psql` in postgres (F11), recreate of orders, payments or inventory from a generated file (F06, F08), the F18 load container. No `docker system prune`, no volume or image removal, no `-p`, no change to `.env`, `docker-compose.yml` or any tracked file.
- Every docker call is an argument list (no shell) with a timeout; every wait has a deadline and an error text. Docker child processes run in their own process group.
- Secrets are never printed or written: printed output masks every secret value from `.env`; the ground truth masks password patterns. F08 uses the fixed marker password `chaos-wrong-password-F08`; the real password in `.env` and in postgres is never changed. F11 uses the local socket of postgres (trust), so no password is read.
- The active record is written before the docker action: a crash or Ctrl+C can always be cleared.
- `clear` always recreates F06 and F08 services from the base files (`--force-recreate`), never from a fault file.

## 7. `clear --all`

`python chaos/chaos.py clear --all` returns the stack to healthy from every state, with or without a record: it clears every recorded fault (late if the effect is already gone), recreates every orders, payments or inventory container whose DATABASE_URL is absent or carries the marker password, starts stopped M1 containers, ends leftover F01 and F02 processes, ends leftover `chaos-F11` sessions in postgres, and removes a leftover F18 load container. Only a container with the name `sentinelops-chaos-load` and the compose project, service and one-off labels is ever removed.

- The F11 sweep (needs `psql` in postgres) and the F18 sweep (needs only docker) run in two separate steps: a failing F11 check does not skip the F18 sweep.
- If a sweep cannot be completed, `clear --all` prints one warning per sweep, finishes the recovery, does not print `no active faults; the stack is healthy`, and exits with code 1.
- On a clean stack the output is `waiting for all 6 containers to be healthy and the gateway to answer 200 ...` and `no active faults; the stack is healthy`.

## 8. F18 measurement (Definition of Done 6)

F18 at the defaults (100 users, spawn rate 20 per s, 120 s), no other load, compared with `docs/BASELINE.md` (20 users). Locust numbers are for the whole run; Prometheus numbers are 1-minute values.

| Measure | Baseline (20 users, `docs/BASELINE.md`) | F18 default surge (100 users) |
|---------|------------------------------------------|-------------------------------|
| Locust requests | | 4733 |
| Locust failures | 0 % | 19 (0.40 %) |
| Locust requests per s | 18.97 | 39.57 |
| Locust p50 | | 1200 ms |
| Locust p95 | 120 ms | 3700 ms |
| Locust p99 | | 5400 ms |
| Locust max | | 7800 ms |
| Prometheus gateway requests per s | about 19 | plateau about 38, peak 42.1, above 30 at 50 s after the inject |
| Prometheus gateway p95 | about 0.16 to 0.20 s | 1.69 s, then 2.96 s, then 4.4 to 4.5 s |
| Prometheus gateway 5xx % | 0 | at most 0.6 |
| CPU % of limit, gateway | about 40 | 90 to 98 (one sample 111: the first service at its limit) |
| CPU % of limit, orders | about 44 | peak 71 |
| CPU % of limit, inventory | about 30 | 35 |
| CPU % of limit, payments | about 20 | 24 |
| Idle CPU % of limit (all app services) | 13 to 15 | |
| Container exits, restarts, OOM kills | none | none |

- The gateway saturates first, not orders. The effect is latency with almost no errors.
- The load container exits with code 1 (`FAIL: 19 failed requests`). This is normal for a surge. It writes `loadtest/results/run*` like every load run (git-ignored).
- The defaults stay (100 users, 20 per s, 120 s). The 60 and 150 user repeats were not needed.
- F18's expected action `scale_service` is the proposal's remedy for a capacity limit. M1 has no scaling mechanism: M8 decides what it does with it.

## 9. Stale and late

A record in `active.json` is `stale` when its effect is gone although the record exists. `status` prints one line per stale fault:

| Fault | Stale means |
|-------|-------------|
| F01 | its busy-loop processes are gone (duration over, or the container was restarted) |
| F02 | its allocator process is gone (duration over, or the container was restarted) |
| F06, F08 | the container no longer has the faulted environment (recreated from the base files, or removed) |
| F10, F13 | the container is running again (started by hand or by a restart) |
| F11 | no `chaos-F11` session is left (duration over, ended by hand, or postgres was restarted) |
| F18 | the load container has exited (duration over) or was removed |

A stale record blocks a new inject of the same fault until `clear ID` is run. A clear of a stale record (and a clear of a record whose effect was gone at the moment of the clear) writes `"late": true`. A clear while the effect still runs has no `late` flag, also for an F18 container that is still running. Details: `chaos_check.py` fails a fault whose clear is late, because its durations are chosen so that the clear comes first.

## 10. Verification: `scripts/chaos_check.py`

```powershell
python scripts/chaos_check.py                  # all 8 faults
python scripts/chaos_check.py --only F10 F13   # selected faults
python scripts/chaos_check.py --quick          # settle 60 s instead of 100 s
```

Standard library only. Needs `docker compose --profile observability up -d --build --wait`; it says so when Prometheus is not reachable. Measured on the owner's machine: the full run of all 8 faults took 18 min 40 s; `--only F10 --quick` took about 4 minutes. No run time was measured for `--quick` over all faults. The first output line (`at most about N minutes`, 53 for the full run) is an upper bound that assumes every symptom appears at its limit, not a forecast. Exit code 0 = every check passed, 1 = a check failed, 130 = interrupted (the background load is removed and `clear --all` is run first).

| Phase | Steps (one `[OK]` or `[FAIL]` line each; `[WARN]` is not a failure) |
|-------|---------------------------------------------------------------------|
| Preflight | Prometheus ready; Loki ready (up to 18 attempts, 5 s apart, so about 90 s, because `/ready` answered HTTP 503 now and then; if Loki is still not ready the check stops with `[FAIL] Loki is ready`, nothing is injected and no load is started); 11 containers running; 6 M1 containers healthy; Prometheus sees 4 app targets and 6 containers; `list` shows all faults; `status` says `no active faults`; an unknown fault, a target that is not allowed and a duration above the cap are refused; a leftover background load container is removed; the background load starts |
| Per fault | wait until 100 s have passed since the last disturbance; baselines of the Prometheus symptoms (must be false before the injection); `inject`; every symptom within its limit; `status` shows it active; a second `inject` is refused; `clear`; stack healthy again; F06 and F08: DATABASE_URL restored and the override file deleted; F18: load container gone and no M1 container restarted, exited or OOM-killed; `status` shows no active faults; the three ground-truth lines are right; no password printed or written |
| Finish | background load removed (first: the smoke test's stock check fails while a load test runs); `clear --all` ends with `no active faults; the stack is healthy`; `status`; `scripts/smoke_test.py` passes 22/22; every line of `ground_truth.jsonl` parses and the file has no CRLF; no password in the file or in any printed output; `git status` is the same as at the start |

How it reads the numbers:

- **Background load:** the M1 load test with 10 users (spawn rate 2) in the container `sentinelops-check-load`. The name differs from the F18 container `sentinelops-chaos-load` on purpose: `clear --all` removes the F18 container and must never remove the check's load.
- **Container count:** with the observability containers running, `docker compose ps -q` and `docker compose --profile observability ps -q` both list 11 containers (6 M1 + 5 observability; "11" means without the detector profile); M3 adds none. With the incident detector of module M4 running (`--profile detector`), `docker compose ps -q` lists 12; `scripts/chaos_check.py` looks the 11 named services up by state, so a 12th container does not change its result. The one-off F18 and background load containers are not listed by `docker compose ps`.
- **Limits** count from the moment `inject` returned. **Empty results** count as 0. **No upper bound** is assumed for any value.
- **Self-ending faults** (F01, F02, F11, F18) are injected with `--duration` = longest `within_s` + 60 s (`--quick`: + 30 s), so the clear comes before the timer and is never late.
- **Loki windows:** the `[2m]` window of a Loki query is shortened to the time since the inject started. Lines of the previous fault (which ended about two minutes earlier) therefore cannot satisfy a symptom. Loki symptoms have no baseline step.
- **Baselines:** a Prometheus symptom that is already true before the injection is a `[FAIL]` (a `[WARN]` with `--quick`), because the symptom would prove nothing. With `--quick` the first fault starts after only 60 s of background load, and rate() underestimates during the first minute of data, so the Definition of Done run (7) uses the full mode.
- **Ground truth:** only the lines written during the run are judged: `inject`, `clear`, `recovered` in file order, one `run_id`, `ts` non-decreasing (`<=`, `ts` can be equal), no `late` flag, `recovery_s` equal to the time between the `clear` and `recovered` `ts` (within 0.06 s), root cause, action and acceptable actions equal to the catalogue.
- **Smoke test per fault:** the smoke test cannot run during the background load, so Definition of Done 2 is checked per fault as "six containers healthy and `GET /api/products/SKU-001` returns 200 after the clear" and once at the end with the 22/22 smoke test.

## 11. Known limitations

- F01 and F02 processes live inside the container and die with it. A restart of the container is the expected remediation, so a stale record after a restart is normal (`clear ID` removes it, with the `late` flag).
- F02 is a soft ceiling (240 of 256 MiB measured, not 230.4 MiB).
- F01 and F02 need `python` in the orders and payments images (Python 3.12, user `app`, writable `/tmp`) and a readable cgroup memory file (cgroup v2 verified; the cgroup v1 fallback was only exercised in the builder's sandbox).
- F06 and F08 need Docker Compose with `!reset` support (verified on Compose v5.5.1). `clear` always recreates from the base files. Docker's health state cannot show an active F08.
- The sweep of `clear --all` assumes that the base files define `DATABASE_URL` for orders, payments and inventory in one form without a query string. If the compose files change this, update `chaos/faults.py`.
- A Ctrl+C in the instant before `run` installs its guard can still interrupt the tool; `clear --all` repairs that state.
- `docker compose ps` does not list the one-off F18 load container; the tool finds it with `docker ps -a` and its name and labels.
- The faults need the six M1 containers running and healthy; `inject` refuses otherwise.
- `scripts/smoke_test.py` fails its stock check while a load test (or F18) runs.
- The Loki log can contain `entry too far behind` errors for the postgres and redis streams after F10 and F13 (Alloy positions are not persisted).
- Loki `/ready` answered HTTP 503 on a running stack five times. Three times it made `scripts/observability_check.py` print 21 of 22 checks (the next run gave 29/29). The fourth and fifth time were the Loki readiness check in the preflight of `chaos_check.py` during the CP5 verification. Fourth (07:23 local): Loki had been up 5.5 minutes (the stack was started at 07:17:55), the 503 lasted more than 12 seconds (5 attempts, 3 s apart), and Loki answered 200 on its own by 07:25:27, with no restart, no OOM kill and no log line except the usual `empty ring` at start. Fifth (about 22:03 local): Loki had been started at 22:01:00 (`restarts=0`; what restarted it was not recorded) and answered 200 only at attempt 4 of 18, so it was not ready for at least 15 seconds, about 1.5 to 2 minutes after its start. Three of the five cases came soon after a stack or Loki start (about 4 minutes, 5.5 minutes and under 2 minutes); the third came about 30 minutes after the stack was started, and the timing of the second is not recorded. The cause is not found. An earlier idea, that an F18 run causes it, is not supported: the fifth case came about 14 hours after the last F18 run. A later `observability_check.py` run, 6 minutes after the last F18 run, passed 29/29 on the first try. Because of this the preflight of `chaos_check.py` waits up to 18 attempts, 5 s apart (about 90 s); a Loki that needs longer stops the check cleanly before anything is injected, and the check can simply be run again.
- The 1-minute windows lag the fault by up to a minute and are unreliable during the first 90 s after a start.

## 12. Tier-2 faults (not built)

D43 puts 13 faults of the proposal catalogue into tier 2: F03, F04, F05, F07, F09, F12, F14, F15, F16, F17, F19, F20, F21. Their names and descriptions are in `docs/PROPOSAL.md`. They need changes this module avoids (M1 code, a message queue for F14, new containers) and are built only if a later module needs them.
