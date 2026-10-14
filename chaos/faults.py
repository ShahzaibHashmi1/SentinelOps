"""SentinelOps M3: helpers around docker / docker compose and the fault implementations.

Built so far:
  F10  PostgreSQL down   (docker compose stop postgres)                        CP1
  F13  Redis down        (docker compose stop redis)                           CP1
  F01  CPU saturation    (busy-loop python processes inside orders)            CP2
  F02  Memory growth     (allocator python process inside payments)            CP2
  F06  Missing variable  (service recreated without DATABASE_URL: crash loop)  CP3
  F08  Wrong credential  (service recreated with a wrong DATABASE_URL password) CP3
The other two faults of the catalogue (F11, F18) are added in CP4.

Design decisions (explain these in the viva):
  * Standard library only (decision D44). Every docker call is `subprocess` with an
    argument list (shell=False), so there is no quoting problem on Windows.
  * All docker access goes through the `Docker` class. A test can replace its runner
    with a fake, so the command sequences can be checked without Docker.
  * Every docker call has a timeout and every wait loop has a deadline.
  * Compose is always called with explicit -f flags (base file + override file). The
    project name is NOT passed: it comes from `name: sentinelops` in docker-compose.yml.
"""

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent

# Same files that a plain "docker compose" loads, but passed explicitly (see module docstring).
BASE_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.override.yml"]

# The 6 M1 containers, in a sensible start order (datastores first).
M1_SERVICES = ["postgres", "redis", "inventory", "payments", "orders", "gateway"]

# "Recovered" means: all 6 containers healthy and this request answers 200 (see docs/CHAOS.md).
GATEWAY_PROBE_URL = "http://127.0.0.1:8000/api/products/SKU-001"

DOCKER_TIMEOUT_S = 60       # one docker call
HEALTHY_TIMEOUT_S = 120     # waiting for containers to become healthy
POLL_INTERVAL_S = 2


class FaultError(RuntimeError):
    """A docker call or a wait failed. The message is meant to be shown to the user."""


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


def default_runner(args, timeout):
    """Run one command (argument list, no shell) in the repo root and capture its output."""
    # The docker child gets its own process group: a Ctrl+C typed in the console then reaches only
    # this Python process, so `run` can finish its clear even when Ctrl+C is pressed again.
    if os.name == "nt":
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group = {"start_new_session": True}
    try:
        done = subprocess.run(
            args,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            **group,
        )
    except FileNotFoundError:
        raise FaultError("the 'docker' command was not found. Is Docker Desktop installed and on PATH?")
    except subprocess.TimeoutExpired:
        raise FaultError(f"command timed out after {timeout} s: {' '.join(args[:4])} ...")
    return CmdResult(done.returncode, done.stdout or "", done.stderr or "")


def http_status(url, timeout=3.0):
    """GET a URL and return the HTTP status code, or None when it cannot be reached."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Docker:
    """Thin wrapper: builds compose command lines, prints them (secrets masked), runs them."""

    def __init__(self, runner=default_runner, echo=print, mask=lambda text: text,
                 sleep=time.sleep, monotonic=time.monotonic, http=http_status, repo_root=REPO_ROOT):
        self.runner = runner
        self.echo = echo
        self.mask = mask
        self.sleep = sleep
        self.monotonic = monotonic
        self.http = http
        self.repo_root = Path(repo_root)

    def compose_base(self, extra_files=()):
        """docker compose -f <base> -f <override> [-f <fault file> ...]: always explicit -f flags, never -p."""
        cmd = ["docker", "compose"]
        for name in BASE_COMPOSE_FILES:
            if (self.repo_root / name).exists():
                cmd += ["-f", name]
        for name in extra_files:
            cmd += ["-f", name]
        return cmd

    def _run(self, cmd, timeout, show):
        shown = self.mask(display_command(cmd))
        if show:
            self.echo("+ " + shown)
        result = self.runner(cmd, timeout)
        if result.returncode != 0:
            detail = self.mask(result.stderr.strip() or result.stdout.strip())
            hint = ""
            lowered = detail.lower()
            if "cannot connect" in lowered or "error during connect" in lowered or "daemon" in lowered:
                hint = " (Is Docker Desktop running?)"
            raise FaultError(f"command failed with exit code {result.returncode}: {shown}\n{detail}{hint}")
        return result

    def compose(self, *args, timeout=DOCKER_TIMEOUT_S, show=True, files=()):
        """Run `docker compose <args>`. `files` are extra -f files (a generated fault file).
        Raises FaultError on a non-zero exit code."""
        return self._run(self.compose_base(files) + [str(a) for a in args], timeout, show)

    def docker(self, *args, timeout=DOCKER_TIMEOUT_S, show=True):
        """Run plain `docker <args>` (used only to read `docker inspect` of a compose container)."""
        return self._run(["docker"] + [str(a) for a in args], timeout, show)

    def exec_python(self, service, code, *args, detach=False, show=True, timeout=DOCKER_TIMEOUT_S):
        """Run a small python program inside a running service container (docker compose exec)."""
        flags = ["-d", "-T"] if detach else ["-T"]
        return self.compose("exec", *flags, service, *python_command(code, *args), timeout=timeout, show=show)

    def ps(self):
        """All containers of the project (also stopped ones) as a list of dicts. Not echoed: it is polled."""
        result = self.compose("ps", "--all", "--format", "json", show=False)
        return parse_compose_ps(result.stdout)


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------

def display_command(cmd, limit=60):
    """Command line for printing: very long arguments (the base64 program) are shortened."""
    return " ".join(a if len(a) <= limit else f"{a[:24]}...({len(a)} chars)" for a in cmd)


# A program is sent to the container as one base64 argument, so there is no quoting problem on
# Windows (no quotes, no newlines in the command line). Inside the program sys.argv[2:] are its arguments.
PY_WRAPPER = "import base64,sys;exec(base64.b64decode(sys.argv[1]).decode())"


def python_command(code, *args):
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    return ["python", "-c", PY_WRAPPER, encoded] + [str(a) for a in args]


def parse_compose_ps(text):
    """`docker compose ps --format json` prints either one JSON array or one JSON object per line."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            raise FaultError(f"cannot parse the output of 'docker compose ps': {line[:80]}")
    return rows


def service_states(rows):
    """{service: (state, health)} for the M1 services; state and health are lower case, health may be ''."""
    states = {}
    for row in rows:
        service = row.get("Service")
        if service in M1_SERVICES:
            states[service] = (str(row.get("State", "")).lower(), str(row.get("Health", "")).lower())
    return states


def not_healthy(states, services=None):
    """Names (with the reason) of the services that are not running AND healthy."""
    problems = []
    for name in (services or M1_SERVICES):
        entry = states.get(name)
        if entry is None:
            problems.append(f"{name} (no container)")
        elif entry[0] != "running":
            problems.append(f"{name} ({entry[0]})")
        elif entry[1] != "healthy":
            problems.append(f"{name} (health: {entry[1] or 'none'})")
    return problems


def running_services(states):
    return {name for name, (state, _health) in states.items() if state == "running"}


def effect_gone(fault_id, target, running, process_counts=None, config_states=None):
    """Stale-state decision: True when the effect of an active fault is no longer there.

    F10 and F13 stop a container, so the effect is gone as soon as that container runs again
    (for example because someone restarted it by hand, or M8 restarted it later).
    F01 and F02 are processes inside a container: the effect is gone when the container is not
    running, or when no process with the fault's marker is left (duration over, or container restarted).
    process_counts is {fault_id: number of marker processes}; a missing entry means "could not be
    counted", which is NOT reported as stale. A fault that is not known here is not reported as stale.
    F06 and F08 change the environment of a recreated container, so the effect is gone when the
    container no longer has the faulted environment: it was recreated from the base files (by hand, by
    `clear`, by M8) or it does not exist any more. config_states is {fault_id: "faulted" | "clean" |
    "missing"}; a missing entry means "could not be read" and is NOT reported as stale. Restarting the
    container keeps its environment, so a restart alone does not make F06/F08 stale.
    """
    if fault_id in ("F10", "F13"):
        return target in running
    if fault_id in CONFIG_FAULTS:
        return (config_states or {}).get(fault_id) in ("clean", "missing")
    if fault_id in PROCESS_FAULTS:
        if target not in running:
            return True
        count = (process_counts or {}).get(fault_id)
        return count == 0
    return False


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------

def wait_until(dk, check, what, timeout_s=HEALTHY_TIMEOUT_S):
    """Call check() -> (done, detail) every few seconds until done, or fail at the deadline."""
    deadline = dk.monotonic() + timeout_s
    while True:
        done, detail = check()
        if done:
            return
        if dk.monotonic() >= deadline:
            raise FaultError(f"timeout after {timeout_s} s waiting for {what}: {detail}")
        dk.sleep(POLL_INTERVAL_S)


def wait_services_healthy(dk, services, timeout_s=HEALTHY_TIMEOUT_S):
    def check():
        bad = not_healthy(service_states(dk.ps()), services)
        return (not bad, "not healthy: " + ", ".join(bad))
    wait_until(dk, check, "healthy " + "/".join(services), timeout_s)


def wait_recovered(dk, timeout_s=HEALTHY_TIMEOUT_S):
    """All 6 containers healthy and the gateway answers 200 for a product request."""
    def check():
        bad = not_healthy(service_states(dk.ps()))
        if bad:
            return (False, "not healthy: " + ", ".join(bad))
        code = dk.http(GATEWAY_PROBE_URL)
        if code != 200:
            return (False, f"GET {GATEWAY_PROBE_URL} returned {code}")
        return (True, "")
    wait_until(dk, check, "the stack to recover", timeout_s)


def heal_stopped_services(dk):
    """For `clear --all`: start every M1 container that exists but is stopped. Returns the names started."""
    states = service_states(dk.ps())
    stopped = [name for name in M1_SERVICES if name in states and states[name][0] in ("exited", "created", "dead")]
    for name in stopped:
        dk.compose("start", name)
    return stopped


# ---------------------------------------------------------------------------
# F10 and F13: stop a container, start it again
# ---------------------------------------------------------------------------

def _stop_and_verify(dk, target):
    dk.compose("stop", target)
    if target in running_services(service_states(dk.ps())):
        raise FaultError(f"{target} is still running after 'docker compose stop {target}'")


def _start_and_wait(dk, target):
    # `start` on a running container does nothing, so this is safe when someone already restarted it.
    dk.compose("start", target)
    wait_services_healthy(dk, [target])


def inject_f10(dk, request):
    """PostgreSQL down. Data volumes are untouched (`stop`, not `down`)."""
    _stop_and_verify(dk, request["target"])


def clear_f10(dk, record):
    _start_and_wait(dk, record["target"])


def inject_f13(dk, request):
    """Redis down."""
    _stop_and_verify(dk, request["target"])


def clear_f13(dk, record):
    _start_and_wait(dk, record["target"])


# ---------------------------------------------------------------------------
# F01 and F02: python processes inside a running container (docker compose exec)
# ---------------------------------------------------------------------------
# How it works, in short:
#   * inject starts the process(es) detached (`exec -d`). Each one gets a marker argument
#     (chaos-F01-proc) so that they can be found again through /proc/<pid>/cmdline. The images are
#     python:3.12-slim based, which has no ps or pgrep, so a small python scanner does the lookup.
#   * the process ends by itself after its duration, or as soon as the stop file exists in /tmp.
#   * clear creates the stop file and waits until no marker process is left.
#   * if the container was restarted, the processes died with it (that is the expected remediation).

@dataclass
class ProcessFault:
    marker: str        # argument that identifies the processes in /proc/<pid>/cmdline
    stop_file: str     # creating this file makes the processes end
    status_file: str   # written by the F02 allocator (not used by F01)


PROCESS_FAULTS = {
    "F01": ProcessFault("chaos-F01-proc", "/tmp/chaos-F01.stop", "/tmp/chaos-F01.status.json"),
    "F02": ProcessFault("chaos-F02-proc", "/tmp/chaos-F02.stop", "/tmp/chaos-F02.status.json"),
}

PROCESS_START_TIMEOUT_S = 20
PROCESS_STOP_TIMEOUT_S = 30
F02_CEILING_FRACTION = 0.9      # the allocator stops at 90 % of the container memory limit

# Programs that run inside the container. sys.argv[2:] holds their arguments (see PY_WRAPPER).

# Count the processes whose command line contains the marker (the scanner itself is not counted).
PY_SCAN = r"""
import os, sys
marker = sys.argv[2].encode()
me = str(os.getpid())
n = 0
for pid in os.listdir('/proc'):
    if pid.isdigit() and pid != me:
        try:
            cmdline = open('/proc/' + pid + '/cmdline', 'rb').read()
        except OSError:
            continue
        if marker in cmdline:
            n += 1
print(n)
"""

PY_TOUCH = r"""
import sys
open(sys.argv[2], 'w').close()
"""

PY_REMOVE = r"""
import os, sys
for path in sys.argv[2:]:
    try:
        os.remove(path)
    except OSError:
        pass
"""

PY_READ = r"""
import sys
try:
    print(open(sys.argv[2]).read())
except OSError:
    print('')
"""

# F01: busy loop. Arguments: stop file, duration in seconds, marker.
F01_CODE = r"""
import os, sys, time
stop, duration = sys.argv[2], float(sys.argv[3])
end = time.time() + duration
while time.time() < end and not os.path.exists(stop):
    for _ in range(100000):
        pass
"""

# F02: allocate step_mib every interval seconds until the container uses `ceiling` bytes, then hold.
# Arguments: stop file, duration, step_mib, interval, ceiling bytes, status file, marker.
# The memory is really touched (one byte per 4 KiB page), otherwise the kernel would not commit it.
# The process raises its own oom_score_adj, so that it is the one the kernel kills if an OOM happens.
F02_CODE = r"""
import json, os, sys, time
stop, duration, step_mib, interval, ceiling, status = sys.argv[2:8]
end = time.time() + float(duration)
step = int(step_mib) * 1048576
interval = float(interval)
ceiling = int(ceiling)
try:
    open('/proc/self/oom_score_adj', 'w').write('500')
except OSError:
    pass

def used():
    for path in ('/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory/memory.usage_in_bytes'):
        try:
            return int(open(path).read())
        except (OSError, ValueError):
            pass
    return None

def report(state, held):
    tmp = status + '.tmp'
    with open(tmp, 'w') as handle:
        json.dump({'state': state, 'held_mib': held // 1048576, 'container_mib': (used() or 0) // 1048576}, handle)
    os.replace(tmp, status)

chunks = []
held = 0
if used() is None:
    report('error_no_cgroup', 0)
    sys.exit(3)
while time.time() < end and not os.path.exists(stop):
    if used() + step <= ceiling:
        block = bytearray(step)
        block[::4096] = b'\x01' * ((step + 4095) // 4096)
        chunks.append(block)
        held += step
        report('growing', held)
    else:
        report('holding', held)
    wake = time.time() + interval
    while time.time() < wake and time.time() < end and not os.path.exists(stop):
        time.sleep(0.2)
report('stopped', 0)
"""


def count_processes(dk, target, marker):
    """Number of processes in the container whose command line contains the marker."""
    out = dk.exec_python(target, PY_SCAN, marker, show=False).stdout.strip()
    try:
        return int(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise FaultError(f"cannot count the {marker} processes in {target}: unexpected output {out[:80]!r}")


def process_counts(dk, records, running):
    """{fault_id: process count} for the active F01/F02 records whose container is running.

    A failed count (exec error, timeout) is left out: unknown is never reported as stale.
    """
    counts = {}
    for record in records:
        fid = record["fault_id"]
        if fid in PROCESS_FAULTS and record["target"] in running:
            try:
                counts[fid] = count_processes(dk, record["target"], PROCESS_FAULTS[fid].marker)
            except FaultError:
                pass
    return counts


def _prepare_process_fault(dk, target, spec):
    """Remove an old stop file (this also proves that exec works) and refuse if processes are left over."""
    try:
        dk.exec_python(target, PY_REMOVE, spec.stop_file, spec.status_file)
        left = count_processes(dk, target, spec.marker)
    except FaultError as exc:
        raise FaultError(f"cannot run python inside {target} with 'docker compose exec': {exc}\n"
                         f"This fault needs exec. Stop here and report it; do not work around it.")
    if left:
        raise FaultError(f"{left} {spec.marker} process(es) are already running in {target}. "
                         f"Run first: python chaos/chaos.py clear --all")


def _wait_for_count(dk, target, spec, expected):
    def check():
        count = count_processes(dk, target, spec.marker)
        return (count == expected, f"{count} of {expected} {spec.marker} processes running in {target}")
    wait_until(dk, check, f"the {spec.marker} processes to start", PROCESS_START_TIMEOUT_S)


def _stop_process_fault(dk, target, spec):
    """Create the stop file, wait until the processes have ended, remove the files. Safe to repeat."""
    if target not in running_services(service_states(dk.ps())):
        dk.echo(f"{target} is not running: its {spec.marker} processes ended with the container")
        return
    dk.exec_python(target, PY_TOUCH, spec.stop_file)

    def check():
        count = count_processes(dk, target, spec.marker)
        return (count == 0, f"{count} {spec.marker} process(es) still running in {target}")
    wait_until(dk, check, f"the {spec.marker} processes to end", PROCESS_STOP_TIMEOUT_S)
    dk.exec_python(target, PY_REMOVE, spec.stop_file, spec.status_file)


def sweep_process_faults(dk, targets_by_fault):
    """For `clear --all`: end F01/F02 processes that have no record. Returns [(fault_id, target)] ended."""
    ended = []
    running = running_services(service_states(dk.ps()))
    for fid, spec in PROCESS_FAULTS.items():
        for target in targets_by_fault.get(fid, []):
            if target in running and count_processes(dk, target, spec.marker) > 0:
                _stop_process_fault(dk, target, spec)
                ended.append((fid, target))
    return ended


def inject_f01(dk, request):
    """CPU saturation: N busy-loop processes inside the container (default 2). The container CPU limit applies."""
    spec = PROCESS_FAULTS["F01"]
    target = request["target"]
    processes = request["params"]["processes"]
    _prepare_process_fault(dk, target, spec)
    for _ in range(processes):
        dk.exec_python(target, F01_CODE, spec.stop_file, request["duration_s"], spec.marker, detach=True)
    _wait_for_count(dk, target, spec, processes)


def clear_f01(dk, record):
    _stop_process_fault(dk, record["target"], PROCESS_FAULTS["F01"])


def container_memory_limit(dk, target):
    """Memory limit in bytes of the running container, read with docker inspect (never assumed)."""
    ids = dk.compose("ps", "-q", target, show=False).stdout.split()
    if not ids:
        raise FaultError(f"no running container found for {target}")
    out = dk.docker("inspect", "--format", "{{.HostConfig.Memory}}", ids[0]).stdout.strip()
    try:
        limit = int(out)
    except ValueError:
        raise FaultError(f"cannot read the memory limit of {target}: docker inspect printed {out[:40]!r}")
    if limit <= 0:
        raise FaultError(f"{target} has no memory limit: F02 refuses to run without one")
    return limit


def inject_f02(dk, request):
    """Memory growth: one allocator process inside the container, ceiling 90 % of the container limit."""
    spec = PROCESS_FAULTS["F02"]
    target = request["target"]
    params = request["params"]
    ceiling = int(container_memory_limit(dk, target) * F02_CEILING_FRACTION)
    _prepare_process_fault(dk, target, spec)
    dk.exec_python(target, F02_CODE, spec.stop_file, request["duration_s"], params["step_mib"],
                   params["interval_s"], ceiling, spec.status_file, spec.marker, detach=True)
    _wait_for_count(dk, target, spec, 1)

    def check():
        text = dk.exec_python(target, PY_READ, spec.status_file, show=False).stdout.strip()
        try:
            state = json.loads(text)["state"] if text else None
        except (ValueError, KeyError, TypeError):
            state = None
        if state and state.startswith("error"):
            raise FaultError(f"the allocator in {target} reported '{state}' "
                             f"(the container has no readable cgroup memory file)")
        return (state in ("growing", "holding"), f"allocator status: {state or 'no status file yet'}")
    wait_until(dk, check, "the memory allocator to start allocating", PROCESS_START_TIMEOUT_S)


def clear_f02(dk, record):
    _stop_process_fault(dk, record["target"], PROCESS_FAULTS["F02"])


# ---------------------------------------------------------------------------
# F06 and F08: recreate a service with a generated compose override file
# ---------------------------------------------------------------------------
# How it works, in short:
#   * inject writes chaos/overrides/<fault>-<service>.yml (git-ignored) that changes ONLY the
#     DATABASE_URL of the target service, then recreates that one service with explicit -f flags:
#     docker-compose.yml, docker-compose.override.yml, <fault file>. No -p flag: the project name
#     comes from `name: sentinelops` in docker-compose.yml.
#   * the base file already has `restart: unless-stopped` for the three services, so no restart
#     policy is added: a failing service is restarted by Docker with a growing delay (crash loop).
#   * the effect is verified before it is applied (docker compose config) and after (docker inspect of
#     the new container). The variable values are parsed in memory and NEVER printed.
#   * clear recreates the service from the base files only (never a plain restart) and deletes the file.

CONFIG_FAULTS = ("F06", "F08")
WRONG_PASSWORD = "chaos-wrong-password-F08"     # a made-up string, not a secret; the real password is never changed
OVERRIDE_DIR = Path("chaos") / "overrides"
CRASH_LOOP_MIN_RESTARTS = 2
CRASH_LOOP_TIMEOUT_S = 60
RECREATE_TIMEOUT_S = 180


def override_relpath(fault_id, target):
    """Path of the generated fault file, relative to the repo root (forward slashes work in compose on Windows)."""
    return (OVERRIDE_DIR / f"{fault_id}-{target}.yml").as_posix()


def override_text(fault_id, target):
    """Content of the generated fault file. Only the DATABASE_URL of one service is touched."""
    if fault_id == "F06":
        value = "!reset null"      # compose v5: removes the variable from the merged config (verified below)
    elif fault_id == "F08":
        value = '"postgresql://${POSTGRES_USER:-shop}:' + WRONG_PASSWORD + '@postgres:5432/${POSTGRES_DB:-shop}"'
    else:
        raise ValueError(f"{fault_id} has no override file")
    return (f"# Generated by chaos/chaos.py for fault {fault_id} (target {target}). Git-ignored; removed by 'clear'.\n"
            f"services:\n  {target}:\n    environment:\n      DATABASE_URL: {value}\n")


def write_override(dk, fault_id, target):
    path = dk.repo_root / override_relpath(fault_id, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(override_text(fault_id, target), encoding="utf-8", newline="\n")
    return override_relpath(fault_id, target)


def remove_generated_overrides(dk):
    folder = dk.repo_root / OVERRIDE_DIR
    if folder.is_dir():
        for path in folder.glob("F0[68]-*.yml"):
            path.unlink()


def database_url_from_env(env):
    """env is the list of 'KEY=VALUE' strings of a container. The DATABASE_URL value, or None when absent."""
    for item in env:
        if item.startswith("DATABASE_URL="):
            return item[len("DATABASE_URL="):]
    return None


def check_f06_rendered(environment):
    """The rendered config of the target must NOT contain DATABASE_URL at all (an empty value does not count)."""
    if "DATABASE_URL" in environment:
        raise FaultError("the fault file did not remove DATABASE_URL: it is still present in the rendered "
                         "compose config of the service (value not shown). The '!reset' tag does not work as "
                         "expected here. Stop and report this; do not work around it.")


def check_f08_rendered(base_url, fault_url):
    """The faulted URL may differ from the base URL in the password only. Values are never put in messages."""
    if base_url is None or fault_url is None:
        raise FaultError("DATABASE_URL is missing from the rendered compose config (value not shown)")
    base, fault = urlsplit(base_url), urlsplit(fault_url)
    if (base.username, base.hostname, base.port, base.path) != (fault.username, fault.hostname, fault.port, fault.path):
        raise FaultError("the faulted DATABASE_URL differs from the base one in more than the password")
    if fault.password != WRONG_PASSWORD:
        raise FaultError("the faulted DATABASE_URL does not contain the wrong password")
    if base.password == WRONG_PASSWORD:
        raise FaultError("the base DATABASE_URL already has the wrong password: the stack is not in its base state")


def rendered_environment(dk, target, files=()):
    """Environment of one service as compose would run it (docker compose config --format json), in memory only."""
    out = dk.compose("config", "--format", "json", files=files).stdout
    try:
        environment = json.loads(out)["services"][target].get("environment") or {}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise FaultError(f"cannot read the rendered compose config of {target}")
    if isinstance(environment, list):
        environment = {item.split("=", 1)[0]: (item.split("=", 1)[1] if "=" in item else None) for item in environment}
    return environment


def container_id(dk, service):
    """Id of the container of a service (running or not), or None when there is none."""
    ids = dk.compose("ps", "-q", "--all", service, show=False).stdout.split()
    return ids[0] if ids else None


def container_env(dk, service):
    """Environment list of the service container, or None when there is no container. Never printed."""
    cid = container_id(dk, service)
    if cid is None:
        return None
    out = dk.docker("inspect", "--format", "{{json .Config.Env}}", cid, show=False).stdout.strip()
    try:
        env = json.loads(out)
    except ValueError:
        raise FaultError(f"cannot read the environment of the {service} container")
    return env if isinstance(env, list) else []


def config_state(dk, fault_id, target):
    """'faulted' | 'clean' | 'missing' for F06/F08, from the real environment of the container."""
    env = container_env(dk, target)
    if env is None:
        return "missing"
    url = database_url_from_env(env)
    if fault_id == "F06":
        return "faulted" if url is None else "clean"
    return "faulted" if (url is not None and WRONG_PASSWORD in url) else "clean"


def config_states(dk, records):
    """{fault_id: state} for the F06/F08 records. A failed read is left out: unknown is never reported as stale."""
    states = {}
    for record in records:
        if record["fault_id"] in CONFIG_FAULTS:
            try:
                states[record["fault_id"]] = config_state(dk, record["fault_id"], record["target"])
            except FaultError:
                pass
    return states


def _wait_crash_loop(dk, target):
    def check():
        cid = container_id(dk, target)
        if cid is None:
            return (False, f"no container for {target}")
        out = dk.docker("inspect", "--format", "{{.RestartCount}} {{.State.Status}}", cid, show=False).stdout.split()
        try:
            restarts, status = int(out[0]), out[1]
        except (ValueError, IndexError):
            return (False, f"cannot read the restart count of {target}")
        return (restarts >= CRASH_LOOP_MIN_RESTARTS, f"restart count {restarts}, status {status}")
    wait_until(dk, check, f"{target} to start crash-looping", CRASH_LOOP_TIMEOUT_S)


def _recreate_from_base(dk, target):
    """Recreate (not just restart) one service from the base files only, then wait until it is healthy."""
    dk.compose("up", "-d", "--no-deps", "--force-recreate", target, timeout=RECREATE_TIMEOUT_S)
    wait_services_healthy(dk, [target])


def inject_f06(dk, request):
    """Missing environment variable: the service is recreated without DATABASE_URL and crash-loops."""
    target = request["target"]
    fault_file = write_override(dk, "F06", target)
    check_f06_rendered(rendered_environment(dk, target, files=[fault_file]))
    dk.echo(f"verified: DATABASE_URL is absent from the rendered config of {target}")
    dk.compose("up", "-d", "--no-deps", "--force-recreate", target, files=[fault_file], timeout=RECREATE_TIMEOUT_S)
    if config_state(dk, "F06", target) != "faulted":
        raise FaultError(f"the new {target} container still has DATABASE_URL")
    dk.echo(f"verified: DATABASE_URL is absent in the new {target} container")
    _wait_crash_loop(dk, target)
    dk.echo(f"verified: {target} is in a crash loop (restart count >= {CRASH_LOOP_MIN_RESTARTS})")


def inject_f08(dk, request):
    """Invalid credential: the service is recreated with a wrong DATABASE_URL password and keeps running."""
    target = request["target"]
    fault_file = write_override(dk, "F08", target)
    base_url = rendered_environment(dk, target).get("DATABASE_URL")
    fault_url = rendered_environment(dk, target, files=[fault_file]).get("DATABASE_URL")
    check_f08_rendered(base_url, fault_url)
    dk.echo(f"verified: the rendered DATABASE_URL of {target} differs from the base one only in the password")
    dk.compose("up", "-d", "--no-deps", "--force-recreate", target, files=[fault_file], timeout=RECREATE_TIMEOUT_S)
    wait_services_healthy(dk, [target])      # the health check is liveness only, so the service stays healthy
    if config_state(dk, "F08", target) != "faulted":
        raise FaultError(f"the new {target} container does not have the wrong password")
    dk.echo(f"verified: {target} is running with the wrong database password")


def clear_config_fault(dk, record):
    """Clear F06/F08: recreate from the base files, wait until healthy, delete the generated file."""
    _recreate_from_base(dk, record["target"])
    (dk.repo_root / override_relpath(record["fault_id"], record["target"])).unlink(missing_ok=True)


def clear_f06(dk, record):
    clear_config_fault(dk, record)


def clear_f08(dk, record):
    clear_config_fault(dk, record)


def sweep_config_faults(dk, targets):
    """For `clear --all`: recreate every target whose container still has an F06/F08 environment.

    This also works without a record (for example after active.json was deleted).
    Returns [(fault_id, service)] that were recreated. The generated files are deleted at the end.
    """
    recreated = []
    for target in targets:
        env = container_env(dk, target)
        if env is None:
            continue
        url = database_url_from_env(env)
        kind = "F06" if url is None else ("F08" if WRONG_PASSWORD in url else None)
        if kind:
            _recreate_from_base(dk, target)
            recreated.append((kind, target))
    remove_generated_overrides(dk)
    return recreated


# fault id -> (inject function, clear function). A fault that is not in this table is not built yet.
IMPLEMENTED = {
    "F01": (inject_f01, clear_f01),
    "F02": (inject_f02, clear_f02),
    "F06": (inject_f06, clear_f06),
    "F08": (inject_f08, clear_f08),
    "F10": (inject_f10, clear_f10),
    "F13": (inject_f13, clear_f13),
}

# Which checkpoint builds each missing fault (used in the "not built yet" message and in `list`).
PLANNED = {"F11": "CP4", "F18": "CP4"}
