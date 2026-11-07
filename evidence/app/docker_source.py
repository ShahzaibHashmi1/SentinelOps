"""The container runtime source (decision D69). This is the ONLY module that names the runtime or talks to
its socket; every other module sees only ContainerObservation objects (evidence/app/model.py).

Why the safety is in this code: mounting the socket read-only (:ro) does not limit what a client may send,
so the limits are enforced here and checked by unit tests:

  (a) One module: no other file in evidence/app names the runtime (checked by tests/test_safety.py).
  (b) Only GET, and only three paths: /containers/json (with a label filter), /containers/<id>/json and
      /events (which must have since and until, so it is never a stream). check_call() raises
      PolicyViolation before anything is sent for any other method or path.
  (c) From the inspect answer only an allow-list is kept (ALLOWED_FIELDS below). Config.Env, Config.Cmd,
      Mounts, NetworkSettings, logs and all other labels are never read.
  (d) Only the services of the topology count. A container, name, label or event attribute that contains the
      fault-injection marker is dropped before anything is stored.
  (e) The transport is injected. The real one is a small http.client over AF_UNIX; tests use a fake and never
      open a socket.

Setting: EVIDENCE_DOCKER_SOURCE=on|off (default on). "off" or an unreachable socket gives the source state
"off" or "no_data"; the evidence builder continues with Prometheus only.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from .model import ContainerObservation
from .redact import has_marker
from .sources import SourceStatus

MODE_ENV = "EVIDENCE_DOCKER_SOURCE"
SOCKET_ENV = "EVIDENCE_DOCKER_SOCKET"
DEFAULT_SOCKET = "/var/run/docker.sock"
SERVICE_LABEL = "sentinelops.service"
CONFIG_HASH_LABEL = "com.docker.compose.config-hash"
ONEOFF_LABEL = "com.docker.compose.oneoff"
TIMEOUT_S = 3.0
MAX_BODY_BYTES = 4 * 1024 * 1024

ALLOWED_FIELDS = (
    "service label", "Image", "Created", "State.Status", "State.StartedAt", "State.FinishedAt", "State.ExitCode",
    "State.OOMKilled", "State.Health.Status", "RestartCount", "HostConfig.RestartPolicy.Name", "HostConfig.Memory",
    "HostConfig.NanoCpus", "compose config-hash label (12 characters)",
)
EVENT_ACTIONS = ("create", "start", "die", "stop", "kill", "oom", "restart", "pause", "unpause", "destroy", "health_status")

_ID = re.compile(r"^[0-9a-f]{12,64}$")
_PATHS = (re.compile(r"^/containers/json$"), re.compile(r"^/containers/[0-9a-f]{12,64}/json$"), re.compile(r"^/events$"))


class PolicyViolation(Exception):
    """A call that the policy does not allow was attempted. Nothing was sent."""


class RuntimeUnavailable(Exception):
    """The runtime socket could not be reached or did not answer in time."""


def check_call(method: str, target: str) -> None:
    """Raise PolicyViolation unless this is one of the three allowed GET calls."""
    if method != "GET":
        raise PolicyViolation(f"method {method!r} is not allowed")
    parts = urllib.parse.urlsplit(target)
    if parts.scheme or parts.netloc or parts.fragment:
        raise PolicyViolation("only a plain path is allowed")
    if not any(rule.match(parts.path) for rule in _PATHS):
        raise PolicyViolation(f"path {parts.path!r} is not allowed")
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if parts.path == "/events" and not ({"since", "until"} <= set(query)):
        raise PolicyViolation("/events needs since and until (never a stream)")


# ------------------------------------------------------------------- real transport

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise RuntimeUnavailable("unix sockets are not available on this platform")
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class UnixSocketTransport:
    """(status, body) of one GET over the socket. Any failure to talk to it raises RuntimeUnavailable."""

    def __init__(self, path: str = DEFAULT_SOCKET):
        self.path = path

    def __call__(self, method: str, target: str, timeout: float):
        check_call(method, target)  # also checked here: the real transport can never send anything else
        conn = _UnixConnection(self.path, timeout)
        try:
            conn.request(method, target, headers={"Host": "localhost"})
            response = conn.getresponse()
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise RuntimeUnavailable("response body too large")
            return response.status, body
        except RuntimeUnavailable:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeUnavailable(f"{exc.__class__.__name__}: {exc}") from exc
        finally:
            conn.close()


# ------------------------------------------------------------------- parsing helpers

def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _limit(value) -> int | None:
    number = _int(value)
    return number if number else None  # 0 means "no limit"


def _plain(item) -> dict:
    return item if isinstance(item, dict) else {}


def extract_observation(service: str, inspect: dict) -> ContainerObservation | None:
    """The allow-listed fields of one inspect answer, or None when the answer carries the marker."""
    state = _plain(inspect.get("State"))
    health = _plain(state.get("Health"))
    host = _plain(inspect.get("HostConfig"))
    policy = _plain(host.get("RestartPolicy"))
    labels = _plain(_plain(inspect.get("Config")).get("Labels"))
    digest = _str(labels.get(CONFIG_HASH_LABEL))
    oom = state.get("OOMKilled")
    obs = ContainerObservation(
        service=service,
        image=_str(inspect.get("Image")),
        created=_str(inspect.get("Created")),
        status=_str(state.get("Status")),
        started_at=_str(state.get("StartedAt")),
        finished_at=_str(state.get("FinishedAt")),
        exit_code=_int(state.get("ExitCode")),
        oom_killed=oom if isinstance(oom, bool) else None,
        health=_str(health.get("Status")),
        restart_count=_int(inspect.get("RestartCount")),
        restart_policy=_str(policy.get("Name")),
        memory_limit=_limit(host.get("Memory")),
        nano_cpus=_limit(host.get("NanoCpus")),
        config_hash=None if digest is None else digest[:12],
    )
    if any(isinstance(v, str) and has_marker(v) for v in vars(obs).values()):
        return None
    return obs


@dataclass(frozen=True)
class ContainerEvent:
    service: str
    action: str
    ts: datetime


def parse_events(body: bytes, services: set) -> list:
    """Newline-delimited event objects -> ContainerEvent for the topology services, allow-listed actions only.
    Events with the marker, exec events (their text can hold commands) and other services are dropped."""
    out = []
    for raw in body.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw or has_marker(raw):
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        attrs = _plain(_plain(item.get("Actor")).get("Attributes"))
        service = attrs.get(SERVICE_LABEL)
        action = _str(item.get("Action"))
        stamp = item.get("time")
        if service not in services or action is None or not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            continue
        base = action.split(":", 1)[0].strip()
        if base not in EVENT_ACTIONS:
            continue
        out.append(ContainerEvent(service, base, datetime.fromtimestamp(stamp, tz=timezone.utc)))
    out.sort(key=lambda e: (e.ts, e.service, e.action))
    return out


# ------------------------------------------------------------------- the source

class DockerSource:
    """Container observations for the topology services. status() is "off", "no_data" or "ok"."""

    name = "docker"

    def __init__(self, services: Iterable[str], transport: Callable | None = None, mode: str = "on", timeout_s: float = TIMEOUT_S):
        if mode not in ("on", "off"):
            raise ValueError(f"{MODE_ENV} must be on or off")
        self._services = set(services)
        self._transport = transport if transport is not None else UnixSocketTransport()
        self._mode = mode
        self._timeout_s = float(timeout_s)
        self._state = "off" if mode == "off" else "no_data"
        self._since: datetime | None = None
        self._detail = f"disabled by {MODE_ENV}=off" if mode == "off" else "not checked yet"

    # -- state
    def status(self) -> SourceStatus:
        return SourceStatus(self._state, self._since, self._detail)

    def _set(self, state: str, detail: str, now: datetime | None) -> None:
        if state != self._state:
            self._state = state
            self._since = now
        self._detail = detail

    # -- calls
    def _get(self, target: str) -> tuple:
        check_call("GET", target)
        try:
            return self._transport("GET", target, self._timeout_s)
        except RuntimeUnavailable:
            raise
        except OSError as exc:  # an injected transport may let OS errors through
            raise RuntimeUnavailable(f"{exc.__class__.__name__}: {exc}") from exc

    def observe(self, now: datetime | None = None) -> list | None:
        """One poll: the observation of every topology service that has a container. Returns None when the
        runtime cannot be asked (state no_data) or the source is off (state off)."""
        if self._mode == "off":
            return None
        listing_target = "/containers/json?" + urllib.parse.urlencode({"all": "true", "filters": json.dumps({"label": [SERVICE_LABEL]})})
        try:
            status, body = self._get(listing_target)
        except RuntimeUnavailable as exc:
            self._set("no_data", f"socket not reachable: {str(exc)[:120]}", now)
            return None
        if status != 200:
            self._set("no_data", f"container listing answered HTTP {status}", now)
            return None
        try:
            entries = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            entries = None
        if not isinstance(entries, list):
            self._set("no_data", "container listing is not a list", now)
            return None
        chosen = self._choose(entries)
        found: dict = {}
        failed = 0
        for service, (cid, _created) in sorted(chosen.items()):
            try:
                status, body = self._get(f"/containers/{cid}/json")
            except RuntimeUnavailable as exc:
                self._set("no_data", f"socket not reachable: {str(exc)[:120]}", now)
                return None
            inspect = None
            if status == 200:
                try:
                    inspect = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    inspect = None
            if not isinstance(inspect, dict):
                failed += 1
                continue
            obs = extract_observation(service, inspect)
            if obs is not None:
                found[service] = obs
        if chosen and failed == len(chosen):
            self._set("no_data", "every container inspection failed", now)
            return None
        self._set("ok", "" if not failed else f"{failed} container inspection(s) failed", now)
        return [found[name] for name in sorted(found)]

    def _choose(self, entries: list) -> dict:
        """service -> (container id, Created) for the listing entries that count (newest Created wins)."""
        chosen: dict = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if has_marker(json.dumps(entry, ensure_ascii=False)):
                continue
            labels = _plain(entry.get("Labels"))
            service = labels.get(SERVICE_LABEL)
            cid = entry.get("Id")
            if service not in self._services or not isinstance(cid, str) or not _ID.match(cid):
                continue
            if labels.get(ONEOFF_LABEL) == "True":
                continue
            created = entry.get("Created") if isinstance(entry.get("Created"), (int, float)) else 0
            if service not in chosen or created > chosen[service][1]:
                chosen[service] = (cid, created)
        return chosen

    def events(self, since: float, until: float) -> list | None:
        """Container events of the topology services in [since, until] (never a stream), or None when unavailable."""
        if self._mode == "off":
            return None
        target = "/events?" + urllib.parse.urlencode({"since": f"{since:.0f}", "until": f"{until:.0f}", "filters": json.dumps({"type": ["container"]})})
        try:
            status, body = self._get(target)
        except RuntimeUnavailable:
            return None
        if status != 200:
            return None
        return parse_events(body, self._services)


def create(environ, services: Iterable[str]) -> DockerSource:
    """Build the source from the environment (EVIDENCE_DOCKER_SOURCE, EVIDENCE_DOCKER_SOCKET)."""
    mode = (environ.get(MODE_ENV) or "on").strip().lower()
    if mode not in ("on", "off"):
        raise ValueError(f"{MODE_ENV} must be on or off, not {mode!r}")
    transport = UnixSocketTransport(environ.get(SOCKET_ENV) or DEFAULT_SOCKET)
    return DockerSource(services, transport, mode)


__all__ = ["DockerSource", "ContainerEvent", "PolicyViolation", "RuntimeUnavailable", "UnixSocketTransport", "check_call",
           "create", "extract_observation", "parse_events"]
