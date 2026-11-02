# Cumulative regression: rule 10, the gates and the ledger

**Status:** built (step R1). `scripts/regression.py` runs the gates, this file holds the rule, the expected numbers and the record of green runs.
**Related:** `PROJECT_CONTEXT.md` (rule 10, decision D66), `docs/DETECTOR.md`, `docs/CHAOS.md`, `docs/OBSERVABILITY.md`.

## 1. Why

Every new module must be proven compatible with all older modules, mechanically, with raw output for the reviewer. The runner executes the three cumulative gates; the table in section 4 is the ledger of expected counts. A test (`regression/tests/test_regression.py`) pins that table to the `SUITES` list of the runner: a count or a command that changes in one place and not in the other makes the unit tests fail.

## 2. The rule (rule 10 of `PROJECT_CONTEXT.md`)

```
10. **Cumulative regression rule (M5 and every later module).** A checkpoint or module is not done until the older modules still pass.
    a. Gate A (every checkpoint): all unit suites of all modules pass with 0 failures (M1 service tests, chaos/tests, detector/tests, regression/tests and the new module's tests), plus the rule validation and the allowed-imports scan.
    b. Gate B (every checkpoint that changes a file outside the new module's own folder, and at module completion): refill the stock, then smoke_test.py 22/22, observability_check.py 29/29, chaos_check.py --only F10 --quick 36/36 and detector_check.py --quick --only F10 --skip-nodata --skip-persistence, against the live stack with the new module running. The expected numbers are in docs/REGRESSION.md.
    c. Gate C (module completion only, on the final commit): chaos_check.py full, detector_check.py full and the new module's own full check, raw output given to the reviewer.
    d. An older test that fails is fixed in the NEW code. An older test or script may be changed only through a recorded decision (D-number) that says why the contract changed; it may be extended, never loosened. A new container gets its own compose profile, and the container-count expectations of older scripts are extended in the same decision.
    e. The reviewer does not mark a checkpoint verified without the gate output pasted raw, and records date, commit and counts in docs/REGRESSION.md.
```

## 3. The three gates

Gates are cumulative: B runs A and then B, C runs A, B and then C. Inside a gate the unit suites come first and the live checks after them.

| Gate | What it proves | When | Needs the stack | Durations |
|---|---|---|---|---|
| A | Every unit suite of every module, the rules validation and the allowed-imports scan | Every checkpoint | No (Docker is needed only for the M1 service tests) | Not measured |
| B | Gate A, then the live checks of M1, M2, M3 and M4 against the running stack (smoke test, observability check, chaos check F10 quick, detector check F10 quick) | Every checkpoint that changes a file outside the new module's own folder, and at module completion | Yes: 12 containers | The two measured live suites take about 4 min (chaos quick) and about 10 min (detector quick); the other suites and the prerequisites are not measured |
| C | Gates A and B, then the full runs of chaos check and detector check (and later the new module's own full check) | Module completion only, on the final commit | Yes: 12 containers | Full runs 18 min 40 s (chaos) and 68 minutes (detector) |

## 4. The suites (the ledger of expected counts)

The table is generated from `SUITES` in `scripts/regression.py` (`python scripts/regression.py --gate C --list` prints the same suites). "Gate" is the gate that introduces the suite. "Expected count": a number is compared with the count found in the output, `None` means "not recorded yet" (the runner prints `[NEW]` and passes; record the number here at the first green run), `-` means that no count is tracked. "Typical duration" is the measured duration of one run on the owner's machine.

| Id | Module | Gate | Command | Expected count | Typical duration |
|---|---|---|---|---|---|
| `M1-unit-gateway` | M1 | A | `docker compose --profile test run --rm -e SERVICE=gateway tests` | 11 | not measured |
| `M1-unit-orders` | M1 | A | `docker compose --profile test run --rm -e SERVICE=orders tests` | 10 | not measured |
| `M1-unit-payments` | M1 | A | `docker compose --profile test run --rm -e SERVICE=payments tests` | 10 | not measured |
| `M1-unit-inventory` | M1 | A | `docker compose --profile test run --rm -e SERVICE=inventory tests` | 11 | not measured |
| `M3-unit` | M3 | A | `python -m unittest discover -s chaos/tests` | 243 | not measured |
| `M4-unit` | M4 | A | `python -m unittest discover -s detector/tests` | 445 | not measured |
| `R1-unit` | R1 | A | `python -m unittest discover -s regression/tests` | 180 | not measured |
| `M4-rules` | M4 | A | `PYTHONPATH=detector python -m app.rules detector/rules/rules.json` | 9 | not measured |
| `M4-imports` | M4 | A | `in process: scan detector/app/*.py` | - | not measured |
| `M1-smoke` | M1 | B | `python scripts/smoke_test.py` | 22 | not measured |
| `M2-observability` | M2 | B | `python scripts/observability_check.py` | 29 | not measured |
| `M3-chaos-quick` | M3 | B | `python scripts/chaos_check.py --only F10 --quick` | 36 | 4 min |
| `M4-detector-quick` | M4 | B | `python scripts/detector_check.py --quick --only F10 --skip-nodata --skip-persistence` | 41 | 10 min |
| `M3-chaos-full` | M3 | C | `python scripts/chaos_check.py` | 135 | 18 min 40 s |
| `M4-detector-full` | M4 | C | `python scripts/detector_check.py` | 164 | 68 min |

Notes on the table:
- The M1 service tests (`M1-unit-*`) pass by exit code; their count is informational (a difference is printed as a `[WARN]`, never a failure). The counts 11 (gateway), 10 (orders), 10 (payments) and 11 (inventory) were measured in the first gate runs of 2026-11-02.
- `M4-rules` runs with the environment variable `PYTHONPATH=detector`; the count is the number of valid rules.
- `M4-imports` is an in-process scan, case-insensitive, of `detector/app/*.py`: the pattern `psycopg|redis|docker|subprocess|requests` must not match, and the pattern `urllib\.request|http\.server|threading` must match at least once (positive control).
- `R1-unit` counts the tests of `regression/tests` (this runner and this file).
- `M4-detector-quick` is 41 checks (first green run of gate B, 2026-11-02, 425.6 s in the runner).

## 5. Result rules of the runner

| Rule | Detail |
|---|---|
| A suite passes only if its exit code is 0 and its own verdict is found | `unittest`: `Ran N tests` and `OK` (`OK (skipped=K)` passes with a `[WARN]`). Check scripts: `N/N checks passed` with N equal on both sides, no `[FAIL]` line, and the verdict line (`SMOKE TEST PASSED`, `OBSERVABILITY CHECK PASSED`, `CHAOS CHECK PASSED`, `DETECTOR CHECK PASSED`). M1 service tests: exit code 0. Rules: `N rules valid` and `pending provenance: none` |
| A count that differs from the ledger fails | `count differs from the ledger: expected 445, found 446; change the ledger by a decision` |
| An expected count of `None` passes | `[NEW] record this count in docs/REGRESSION.md: <n>` |
| A suite that times out or cannot start fails with the reason | `timed out after N s`, `command not found: <program>` |
| The runner does not stop at the first failure | The remaining independent suites run and all results are shown; a failed prerequisite stops the live suites only (they are reported as `not run`, which counts as a failure) |
| A partial run says so | `--only` prints `REGRESSION GATE B PASSED (partial: 2 of 13 suites)` |

## 6. Prerequisites of the live suites (gates B and C)

Each line is an `[OK]` or `[FAIL]` check of the run:

| Step | Command or condition |
|---|---|
| 1 | Docker answers: `docker info` |
| 2 | The stack is rebuilt and started (skipped with `--no-build`): `docker compose --profile observability --profile detector up -d --build --wait`. Rebuild first: the detector image contains `detector/app` and `rules.json` |
| 3 | `docker compose ps -q` lists `EXPECTED_CONTAINERS` (12) containers. A new module that adds containers extends this number by a decision |
| 4 | Both detector sources `prometheus` and `loki` are `ok` (waits up to 120 s) |
| 5 | The stock is refilled and the runner waits 35 s (section 9) |
| 6 | `python chaos/chaos.py status` prints `no active faults` (the command exits 0 in every case, so the text decides). Otherwise `clear --all` runs once and a `[WARN]` is printed |
| 7 | No open incident (waits up to 300 s, polls every 10 s). Between two live suites the runner waits for this again |

Cleanup (`finally`, only if a live suite was started): `python chaos/chaos.py clear --all`, then `docker rm -f` for the leftover load containers `sentinelops-check-load` and `sentinelops-detector-check-load`. Ctrl+C ends the current suite, runs the cleanup and exits with code 130. The runner never injects a fault itself; faults are injected only by the suites (the check scripts).

## 7. How to run (PowerShell, repo root)

One command per line.

```powershell
python scripts/regression.py --gate A
```
Gate A: nine suites, no stack needed (Docker only for the four M1 service tests). Closing lines: `N/N checks passed`, `REGRESSION GATE A PASSED`.

```powershell
python scripts/regression.py --gate B
```
Gate B: gate A, then the live suites. It rebuilds and starts the stack itself (`--no-build` skips that).

```powershell
python scripts/regression.py --gate C
```
Gate C: gates A and B, then the two full runs. Total typical live time at least about 101 minutes.

| Option | Meaning |
|---|---|
| `--list` | Print the suites of the gate and the estimate, run nothing |
| `--dry-run` | Print every command that would run, run nothing, write no report |
| `--only <suite-id> ...` | Run selected suites of the gate (case-insensitive); the verdict says `partial` |
| `--no-build` | Skip the rebuild of the stack (repeated runs; the stack must already be up) |
| `--report PATH` | Report file (default `regression_report.md` in the repo root, git-ignored) |

Exit codes: 0 pass, 1 at least one check failed, 2 bad arguments, 130 interrupted.

The report file `regression_report.md` contains: timestamp (UTC), `git rev-parse --short HEAD`, whether tracked files are modified, the gate, the table (suite, command, expected, found, seconds, PASS or FAIL), the last 60 lines of every failed suite, the total time and one ready-to-paste ledger row for section 10. The runner changes no other file.

## 8. When a gate fails

| Case | Action |
|---|---|
| A suite of an older module fails | Fix the NEW code. Older tests and scripts change only through a recorded decision (D-number) that says why the contract changed; they may be extended, never loosened |
| `count differs from the ledger` | The number of tests or checks of an older suite changed. If the change is intended, record a decision and update section 4 and `SUITES` together (the docs test fails until both agree). Otherwise find the test that disappeared or appeared |
| `[NEW] record this count` | Not a failure. Put the number into section 4 and into `SUITES` at the first green run |
| A prerequisite fails | Read the `[FAIL]` line: Docker not running, a container missing or an extra one (a leftover load container: `docker rm -f sentinelops-check-load sentinelops-detector-check-load`), a detector source not `ok`, a fault that cannot be cleared (`python chaos/chaos.py clear --all`), an incident that stays open |
| A live suite fails with `insufficient_stock` | Refill the stock (section 9) and run the suite again: `python scripts/regression.py --gate B --only <suite-id> --no-build` |
| The new container breaks an older script | A new container gets its own compose profile; the container-count expectations of older scripts are extended in the same decision as the new module |
| Interrupted run | Ctrl+C cleans up. If something looks wrong afterwards: `python chaos/chaos.py status`, `python chaos/chaos.py clear --all` |

## 9. Stock refill

The load tests consume the stock of the 50 products; at 0 the smoke test fails with HTTP 409 `insufficient_stock`. The runner refills before the live suites. By hand:

```powershell
"UPDATE items SET stock = 1000;" | docker compose exec -T postgres sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB'
```
It prints `UPDATE 50`. Then wait 35 s: the inventory service caches stock reads for 30 s.

## 10. Adding the suites of a new module

1. Add entries to `SUITES` in `scripts/regression.py` and nothing else: id, module, gate, command (argument list), kind (`unittest`, `checks`, `exit`, `python`), expected count (or `None`), typical duration (or `None`), timeout, needs-stack flag, description. Unit suites go to gate A (before the live checks of gate A's list ends), the module's quick live check goes to gate B, its full check to gate C.
2. Add the rows to the table in section 4 (the generated lines of `python -c "import sys; sys.path.insert(0, 'scripts'); import regression; print('\n'.join(regression.suite_table_markdown()))"` show the exact text).
3. Run `python -m unittest discover -s regression/tests` (the docs test compares both) and `python scripts/regression.py --gate C --list`.
4. A module that adds containers extends `EXPECTED_CONTAINERS` and the container counts of older scripts in the same decision, and gets its own compose profile.
5. After the first green run record the `[NEW]` counts and add a row to section 11.

## 11. Last green runs

First green runs of the runner on the owner's machine: gate A and gate B on 2026-11-02 (the files of step R1 were not committed yet at that time, so the commit column names the base commit). Gate C has not been run yet; its first green run is added at the completion of module M5.

| Date | Commit | Gate | Result |
|---|---|---|---|
| 2026-10-31 | 449f66c | manual: M3 chaos full 135/135, M4 detector full 164/164, M4 unit 445 | PASS |
| 2026-11-02 | 449f66c + R1 files | A (9 suites, 158 s): M1 service tests 11, 10, 10, 11; M3 unit 243; M4 unit 445; R1 unit 180; rules 9; imports scan clean | PASS |
| 2026-11-02 | 449f66c + R1 files | B (13 suites, 898 s): gate A, then smoke 22, observability 29, chaos F10 quick 36, detector F10 quick 41 | PASS |

Ledger rows of the runner use the columns `| Date | Commit | Gate | Result | Counts |` (printed at the end of `regression_report.md`).

## 12. Known facts

| Fact | Value |
|---|---|
| Full chaos check | 135/135 checks in 18 min 40 s (owner's machine) |
| Full detector check | 164/164 checks in 68 minutes (its banner estimates about 75, at most about 87) |
| Chaos check, F10 quick | 36/36 checks in about 4 minutes |
| Detector check, F10 quick without the no-data and persistence steps | About 10 minutes in the banner; 41/41 checks in 425.6 s (7 min 6 s) in the first runner run |
| Containers | 6 without profiles, 11 with `--profile observability`, 12 with `--profile observability --profile detector` |
| Not measured | The unit suites, the smoke test, the observability check and the prerequisites; the runner's estimate counts only measured durations |
