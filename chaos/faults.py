"""SentinelOps M3: helpers around docker / docker compose and the fault implementations.

CP1 contains the two simplest faults:
  F10  PostgreSQL down  (docker compose stop postgres)
  F13  Redis down       (docker compose stop redis)
The other six faults of the catalogue are added in CP2 to CP4.

Design decisions (explain these in the viva):
  * Standard library only (decision D44). Every docker call is `subprocess` with an
    argument list (shell=False), so there is no quoting problem on Windows.
  * All docker access goes through the `Docker` class. A test can replace its runner
    with a fake, so the command sequences can be checked without Docker.
  * Every docker call has a timeout and every wait loop has a deadline.
  * Compose is always called with explicit -f flags (base file + override file). The
    project name is NOT passed: it comes from `name: sentinelops` in docker-compose.yml.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

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

    def compose_base(self):
        cmd = ["docker", "compose"]
        for name in BASE_COMPOSE_FILES:
            if (self.repo_root / name).exists():
                cmd += ["-f", name]
        return cmd

    def compose(self, *args, timeout=DOCKER_TIMEOUT_S, show=True):
        """Run `docker compose <args>`. Raises FaultError on a non-zero exit code."""
        cmd = self.compose_base() + [str(a) for a in args]
        if show:
            self.echo("+ " + self.mask(" ".join(cmd)))
        result = self.runner(cmd, timeout)
        if result.returncode != 0:
            detail = self.mask(result.stderr.strip() or result.stdout.strip())
            hint = ""
            lowered = detail.lower()
            if "cannot connect" in lowered or "error during connect" in lowered or "daemon" in lowered:
                hint = " (Is Docker Desktop running?)"
            raise FaultError(f"command failed with exit code {result.returncode}: "
                             f"{self.mask(' '.join(cmd))}\n{detail}{hint}")
        return result

    def ps(self):
        """All containers of the project (also stopped ones) as a list of dicts. Not echoed: it is polled."""
        result = self.compose("ps", "--all", "--format", "json", show=False)
        return parse_compose_ps(result.stdout)


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------

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


def effect_gone(fault_id, target, running):
    """Stale-state decision: True when the effect of an active fault is no longer there.

    F10 and F13 stop a container, so the effect is gone as soon as that container runs again
    (for example because someone restarted it by hand, or M8 restarted it later).
    For a fault that is not known here we cannot tell, so it is NOT reported as stale.
    """
    if fault_id in ("F10", "F13"):
        return target in running
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


# fault id -> (inject function, clear function). A fault that is not in this table is not built yet.
IMPLEMENTED = {
    "F10": (inject_f10, clear_f10),
    "F13": (inject_f13, clear_f13),
}

# Which checkpoint builds each missing fault (used in the "not built yet" message and in `list`).
PLANNED = {"F01": "CP2", "F02": "CP2", "F06": "CP3", "F08": "CP3", "F11": "CP4", "F18": "CP4"}
