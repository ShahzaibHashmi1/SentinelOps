ROLE
You are a senior SRE / chaos-engineering engineer helping me build Module M3 (chaos / fault injection, tier 1) of my final year project "SentinelOps" (AI-powered incident detection, root-cause analysis and remediation for containerised apps). I am a BS-IT student strong in Python, Docker and Azure. I must be able to explain every part in my viva, so keep the code simple, readable and commented where a design decision matters.

HOST ENVIRONMENT
Windows with Docker Desktop (WSL2). Every command must work in PowerShell (version 5.1: no "&&", one command per line). Do not use a Makefile or bash-only scripts.
Host facts: Docker 29.8.1 with Docker Compose v5.5.1 (Compose file format without a "version:" key). The host Python is 3.14.7, so everything that runs on the host uses only the Python standard library (no pip installs, no PyYAML, no Docker SDK for Python). Docker Desktop has about 7.6 GiB RAM and 12 CPUs. Everything is free and local. Docker Desktop must be running before any compose command. Do NOT wrap "docker compose up" in PowerShell Measure-Command (Compose v5.5.1 fails with "failed to get console"); time with Get-Date.

PROJECT CONTEXT
M1 (target application: gateway, orders, payments, inventory, postgres, redis) and M2 (Prometheus, cAdvisor, Loki, Alloy, Grafana; contract in docs/OBSERVABILITY.md) are done and verified. M3 creates the labelled faults and the ground truth that later modules depend on: M4 (detection) and M6 (agent) must find the injected fault, and M12 (evaluation) computes MTTD, MTTR and RCA accuracy by comparing their output with the ground truth that M3 records. Read PROJECT_CONTEXT.md first; its rules apply. Read docs/OBSERVABILITY.md (the label contract and queries) and docs/PROPOSAL.md section 8 (fault catalogue) if it is provided.

DECISIONS ALREADY MADE (record them as D43 and D44 in the decision log in CP1)
- D43: Tier 1 contains exactly these 8 faults of the proposal catalogue: F01, F02, F06, F08, F10, F11, F13, F18. Every one can be injected without changing M1 service code and without a new container. The other 13 faults (F03, F04, F05, F07, F09, F12, F14 to F17, F19 to F21) are tier 2 and are NOT built now.
- D44: The fault runner is a command-line tool that uses only the Python standard library and calls the "docker" and "docker compose" CLIs through subprocess. The Docker SDK for Python, stress-ng, Toxiproxy and an HTTP API for triggering faults are NOT part of tier 1 (host stdlib rule; the HTTP trigger is built when M10 needs it).

SCOPE: MODULE M3 ONLY
Build: the fault catalogue file, the CLI (list, status, inject, clear, run), the 8 fault implementations, ground-truth recording, a verification script, unit tests for the pure logic, documentation.
Do NOT build: other faults, an HTTP/WebSocket API, a container for the injector, Toxiproxy, a message queue, tracing, detection, alert rules, LLM or agent code, remediation, dashboards for faults, or anything that changes M1 service code, common/, db/, services/, loadtest/, the M2 configs, or the existing behaviour of docker-compose.yml and docker-compose.override.yml. If an M1 or M2 change seems necessary, STOP and ask me. Do not add features that are not listed here.

REPOSITORY LAYOUT (additions only)
  chaos/chaos.py                 CLI entry point: argument parsing, state, ground truth, safety checks
  chaos/faults.py                one inject and one clear function per fault, plus helpers around docker and docker compose
  chaos/catalogue.json           the 8 faults (schema below); JSON because PyYAML is not available
  chaos/tests/test_chaos_unit.py unittest, standard library, for the pure logic only
  chaos/data/                    runtime output (ground_truth.jsonl, active.json); git-ignored, created on first use
  chaos/overrides/               generated compose override files for F06 and F08; git-ignored
  scripts/chaos_check.py         verification script (standard library only)
  docs/CHAOS.md                  the contract that M4, M6 and M12 rely on
  .gitignore                     add: chaos/data/ and chaos/overrides/ (append only)

CATALOGUE SCHEMA (chaos/catalogue.json, one object per fault)
  id, name, category (Resource | Configuration | Dependency | Load, as in the proposal), description,
  default_target, allowed_targets (service names),
  default_duration_s, max_duration_s (hard cap for any fault: 600),
  params (name, default, min, max for each tunable value),
  expected_root_cause: { service, type }  (type is one of: cpu_exhaustion, memory_exhaustion, misconfiguration, db_auth_failure, database_unavailable, db_lock_contention, cache_unavailable, capacity_limit),
  expected_action (one name from the remediation allowlist: restart_service, scale_service, rollback_deployment, revert_config, restore_network_path, rotate_logs, terminate_blocking_query) and acceptable_actions (list),
  expected_symptoms: a short list of Prometheus/Loki signatures, each { query, condition, within_s } built from the metric names in docs/OBSERVABILITY.md (used by scripts/chaos_check.py and documented in docs/CHAOS.md).

THE 8 FAULTS (verify every command against the real compose file, images and database schema before you rely on it)
  F01 Resource: CPU saturation in orders. Default target orders. Start 2 busy-loop Python processes inside the running container (docker compose exec -d orders python -c ...). The loop stops by itself after the duration and also when a stop file in /tmp exists; clear creates the stop file and waits until the processes have ended. Container CPU limits already apply, so the host is not saturated. expected_root_cause orders / cpu_exhaustion; expected_action restart_service; acceptable_actions [restart_service, scale_service].
  F02 Resource: memory growth in payments. Default target payments. A Python process inside the container allocates memory in steps (default 8 MiB every 2 s) up to a ceiling of 90 % of the container memory limit (read the limit from docker inspect), then holds. The ceiling prevents an OOM kill by default; the allocator is the largest process, so if the kernel does kill something, document which process died. Stop file and duration as in F01. expected_root_cause payments / memory_exhaustion; expected_action restart_service.
  F06 Configuration: missing required environment variable, crash loop. Default target inventory (allowed: orders, payments, inventory). Recreate the service with DATABASE_URL removed so that the service fails at startup with its own "required" error. Use a generated override file in chaos/overrides/ passed with explicit -f flags (docker-compose.yml, docker-compose.override.yml, then the fault file; docker compose only auto-loads the override file when no -f is given). Compose v5 supports "!reset" to remove a variable; verify that the variable is really absent, not empty. Check the restart policy: if the service has none, the fault file must set one (restart: on-failure) for the faulted service only, so that a crash LOOP is visible. Clear: recreate the service from the base files without the fault file (docker compose up -d --no-deps <service>) and wait until it is healthy. expected_root_cause <target> / misconfiguration; expected_action revert_config.
  F08 Configuration: invalid database credential. Default target orders (allowed: orders, payments, inventory). Same mechanism as F06 but DATABASE_URL gets a wrong password for this service only; the real password in .env and in postgres is NOT changed. The service starts (the pool opens without waiting) but its queries fail with an authentication error. expected_root_cause <target> / db_auth_failure; expected_action revert_config.
  F10 Dependency: PostgreSQL down. Target postgres only. docker compose stop postgres. Clear: docker compose start postgres and wait until healthy. Data volumes are untouched. expected_root_cause postgres / database_unavailable; expected_action restart_service.
  F11 Dependency: lock contention. Target postgres only. A background psql session (set application_name to chaos-F11) opens a transaction, takes an ACCESS EXCLUSIVE lock on the table that inventory reserves stock from (read db/init.sql for the table name), and sleeps for the duration. Reservations then block until the 2 s timeouts fire. Clear: terminate that session with pg_terminate_backend, found through application_name, and verify it is gone. Use the database user and name from the compose file and .env, never hard-coded secrets. expected_root_cause postgres / db_lock_contention; expected_action terminate_blocking_query.
  F13 Dependency: Redis down. Target redis only. docker compose stop redis. Clear: start and wait until healthy. expected_root_cause redis / cache_unavailable; expected_action restart_service.
  F18 Load: traffic spike. Target gateway (the load enters there). Run the M1 load test image with more users than the baseline of 20: docker compose --profile load run with a fixed container name (--name sentinelops-chaos-load) and USERS, SPAWN_RATE and DURATION passed per run (defaults USERS=100, SPAWN_RATE=20, duration 120 s; hard cap USERS 150). Clear: stop and remove that container. In CP4, measure on my machine what 100 users do (req/s, p95, error %, which service saturates first) and set the default so that the effect is visible but no container exits. expected_root_cause: service null and type capacity_limit; document the service that saturates first as an observation. expected_action scale_service (the proposal's action). Note in docs/CHAOS.md that M1 has no scale-out mechanism; how M8 executes scale_service is an open question for M8, not for M3.

CLI BEHAVIOUR (python chaos/chaos.py ..., standard library only)
- list: table of the 8 faults (id, category, default target, expected root cause, expected action).
- status: active faults with age, or "no active faults"; reads chaos/data/active.json and cross-checks with docker (a fault whose effect is gone is reported as stale, not as active).
- inject <ID> [--target <service>] [--duration <s>] [--param name=value ...]: validates everything first (known fault, allowed target, parameter range, duration <= max_duration_s, stack running, no active fault with the same ID), then injects, writes the ground-truth record and the active state. Returns immediately; the fault stays until clear or until its own duration ends (F01, F02, F11, F18). F06, F08, F10 and F13 stay until clear.
- clear <ID> | clear --all: idempotent; works after a crash, after the container was restarted by someone else (for example by M8 later), and when nothing is active. clear --all returns the stack to the base compose configuration (recreate any service that was changed by a fault file, restart any stopped service, end F01/F02/F11/F18 effects) and waits until all 6 M1 containers are healthy (timeout 120 s). Exit code 0 only if the stack is healthy again.
- run <ID> [--warmup 60] [--hold <s>] [--cooldown 60] [same options as inject]: warm-up, inject, hold, clear, cool-down, as one labelled experiment; try/finally so that Ctrl+C always runs clear and marks the record aborted. This is the command M12 will call.
- Safety: only act on containers of this compose project; never touch the host outside docker; never read or print secrets (mask passwords in every printed command and in the ground-truth file); never modify tracked files (overrides go to chaos/overrides/, git-ignored).

GROUND TRUTH (chaos/data/ground_truth.jsonl, append-only, one JSON object per line, UTF-8, "\n" line ends)
Each inject, clear and run writes events. Fields: run_id (short unique id), event (inject | clear | recovered | aborted), fault_id, category, target, params, ts (UTC, ISO 8601 with milliseconds and Z), expected_root_cause, expected_action, acceptable_actions, and on "recovered" the field recovery_s. "recovered" is written when all 6 containers are healthy and a GET /api/products/SKU-001 through the gateway returns 200. The timestamp for "inject" is taken immediately before the first docker action. Document the exact semantics in docs/CHAOS.md, because M12 computes MTTD and MTTR from them.

VERIFICATION SCRIPT (scripts/chaos_check.py, standard library only, exit 0 = pass)
Starts a light background load (the M1 load test with USERS=10 under a fixed container name), then for each fault in the catalogue (or --only F10): runs inject, waits for each expected_symptoms entry to be true within its within_s (queries Prometheus on 127.0.0.1:9090 and, where listed, Loki on 127.0.0.1:3100), runs clear, waits for recovery, checks the ground-truth records, and prints one [OK]/[FAIL] line per step. At the end it stops the background load, runs clear --all, and runs scripts/smoke_test.py (22/22). It requires the observability profile and says so clearly when Prometheus is not reachable. Expect a run time of about 15 minutes for all 8 faults; support --quick (shorter durations).

UNIT TESTS (chaos/tests/test_chaos_unit.py, unittest, runs on the host with: python -m unittest discover -s chaos/tests)
Cover only pure logic: catalogue validation (required fields, allowed values, limits), argument validation (unknown fault, target not allowed, duration over the cap, bad param), ground-truth line formatting, masking of secrets, the stale-state decision. No docker calls in tests.

DOCUMENTATION (docs/CHAOS.md)
How to run (list, inject, clear, run, clear --all), the catalogue table (id, category, target, injection method, clear method, expected_symptoms, expected root cause, expected action), the ground-truth schema with an example line per event type, the meaning of each timestamp, parameters and limits, the safety rules, known limitations (for example: F01 and F02 processes die when the container is restarted, which is the expected remediation; F18 and scale_service; faults need the 6 M1 containers running), and the list of the 13 tier-2 faults that are not built. Also add a README section "Fault injection (module M3)" with the quick start.

DEFINITION OF DONE (all must be true)
1. python chaos/chaos.py list shows the 8 faults; status on a clean stack says "no active faults".
2. For each of F01, F02, F06, F08, F10, F11, F13, F18: inject produces its documented signature in Prometheus or Loki within the documented time (checked with the PromQL/LogQL from docs/CHAOS.md and visible on the Overview and Service Detail dashboards), and clear returns the stack to healthy: 6 containers healthy and python scripts/smoke_test.py passes 22/22 within 120 s of the clear.
3. chaos/data/ground_truth.jsonl contains the documented events for every injection; every line parses with json.loads; inject ts <= clear ts <= recovered ts; no password appears in the file or in any printed output.
4. Safety: the CLI refuses an unknown fault, a target that is not allowed, a duration above the cap, and a second inject of an active fault; clear --all restores a healthy stack from every state, including after Ctrl+C during run and after a manually restarted container; git status after a full cycle shows no modified tracked file except the files this module is allowed to change.
5. A plain "docker compose up -d --wait" still starts exactly the 6 M1 containers, "docker compose --profile observability up -d --build --wait" still starts 11, python scripts/smoke_test.py passes 22/22 and python scripts/observability_check.py passes 29/29 after the M3 files are added.
6. F18: the measured effect of the default surge (req/s, p95, error %, first saturated service, compared with docs/BASELINE.md) is recorded in docs/CHAOS.md, and no container exits during the surge.
7. python -m unittest discover -s chaos/tests passes; python scripts/chaos_check.py passes (all 8 faults, exit code 0).
8. docs/CHAOS.md is complete and the README has the "Fault injection (module M3)" section.
9. PROJECT_CONTEXT.md is updated (sections 2b standard commands, 6 repo structure, 7 status, 8 decisions D43 and up, 9 current focus, 10 known issues).

KNOWN RISKS TO HANDLE
- docker compose exec into the service images: verify that python exists in every image you use and what user the process runs as; if "exec" is not possible for a fault, STOP and tell me before choosing another method.
- Memory: the allocator in F02 must respect the container limit; check the limit with docker inspect, do not assume a number.
- Overrides: explicit -f flags are required; the project name must stay the one I already use (same directory), so do not pass a different -p.
- F06 and F08 recreate a container; recovery must recreate it from the base files, not just restart it.
- F11 needs the real table name and the real database user and name; read db/init.sql and the compose file.
- Windows quoting: use subprocess with argument lists (shell=False); avoid shell string building; print the commands you run with secrets masked.
- Timeouts: every docker call has a timeout; every wait loop has a deadline and a clear error text.
- cAdvisor series for a stopped container disappear; use "up" and the http_* series for stopped-service signatures (see docs/OBSERVABILITY.md section 7).
- You cannot run Docker. Say clearly which parts you could not run. Validate natively: python -m py_compile, JSON syntax, the unit tests, argument validation and the ground-truth writer with a fake command runner; write the verification commands so that I can prove each result on my machine.

WORKING METHOD: 5 CHECKPOINTS
After each checkpoint STOP. Print: (1) files created or changed, one line each on what it does, (2) exact PowerShell verification commands, (3) the expected output, (4) any assumption you made, (5) a ready-to-paste update for PROJECT_CONTEXT.md sections 7 (module status), 8 (decision log), 9 (current focus: which checkpoint is next) and 10 (known issues), (6) a suggested git commit message. Deliver changed files as a zip of only the new and changed files, laid out for the repo root. Then wait for me to reply "continue".
CP1: catalogue.json for all 8 faults, the CLI skeleton (list, status, inject, clear, run), state and ground-truth writer, the safety validation, unit tests, .gitignore additions, decisions D43 and D44, and the two simplest faults F10 and F13 (docker stop and start) with their verification (DoD 1, 3 and 4 partly).
CP2: F01 and F02 (processes inside the containers, stop file, memory ceiling) with their verification (DoD 2 partly).
CP3: F06 and F08 (generated override files, recreate, restart policy, clear by recreate) with their verification (DoD 2 partly; DoD 4 for clear --all).
CP4: F11 and F18, the full "run" lifecycle with recovery measurement, the F18 measurement against docs/BASELINE.md (DoD 2 partly, DoD 6).
CP5: scripts/chaos_check.py, docs/CHAOS.md, README section, final PROJECT_CONTEXT.md update including sections 2b, 6, 7, 8, 9, 10 (DoD 5, 7, 8, 9; re-check 1 to 4).
Start with CP1 now.
