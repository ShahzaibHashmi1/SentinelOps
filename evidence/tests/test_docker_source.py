import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone

import _bootstrap  # noqa: F401
import fakes
import test_bundle

from app import docker_source as ds

SERVICES = set(fakes.TOPOLOGY.services)
NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
SECRET = "secret123XYZ"


def cid(n):
    return f"{n:012x}" + "a" * 20


def listing_entry(service, n, *, created=1_700_000_000, labels=None, names=None):
    lab = {"sentinelops.service": service, "com.docker.compose.service": service}
    lab.update(labels or {})
    return {"Id": cid(n), "Names": names or [f"/sentinelops-{service}-1"], "Image": f"sentinelops/{service}:1.0.0", "Created": created,
            "Labels": lab, "State": "running", "Command": "run"}


def inspect_payload(service, *, status="running", created="2026-11-03T09:00:00.123456789Z", started="2026-11-03T09:00:01.5Z",
                    finished="0001-01-01T00:00:00Z", restart_count=0, oom=False, health="healthy", memory=268435456, nano=500000000,
                    config_hash="a" * 64, image="sha256:" + "b" * 64, extra_label=None):
    state = {"Status": status, "StartedAt": started, "FinishedAt": finished, "ExitCode": 0, "OOMKilled": oom}
    if health is not None:
        state["Health"] = {"Status": health, "Log": [{"Output": f"POSTGRES_PASSWORD={SECRET}"}]}
    labels = {"com.docker.compose.config-hash": config_hash, "sentinelops.service": service, "secret.label": SECRET}
    labels.update(extra_label or {})
    return {"Id": "x", "Created": created, "Image": image, "RestartCount": restart_count, "State": state,
            "Config": {"Env": [f"POSTGRES_PASSWORD={SECRET}", f"DATABASE_URL=postgres://app:{SECRET}@postgres/shop"], "Cmd": ["python", "-m", SECRET],
                       "Labels": labels},
            "HostConfig": {"Memory": memory, "NanoCpus": nano, "RestartPolicy": {"Name": "unless-stopped"}, "Binds": [f"/host/{SECRET}:/x"]},
            "Mounts": [{"Source": f"/host/{SECRET}"}], "NetworkSettings": {"Networks": {"n": {"Aliases": [SECRET]}}}}


class FakeRuntime:
    """Transport(method, target, timeout) for the source. Every call must pass the policy."""

    def __init__(self):
        self.entries = []
        self.inspect = {}
        self.calls = []
        self.fail = None
        self.listing_status = 200
        self.events_body = b""

    def __call__(self, method, target, timeout):
        ds.check_call(method, target)  # the fake enforces nothing the real transport would not
        self.calls.append((method, target))
        if self.fail:
            raise ds.RuntimeUnavailable(self.fail)
        path = urllib.parse.urlsplit(target).path
        if path == "/containers/json":
            return self.listing_status, json.dumps(self.entries).encode()
        if path == "/events":
            return 200, self.events_body
        container = path.split("/")[2]
        payload = self.inspect.get(container)
        if payload is None:
            return 404, b"{}"
        return 200, json.dumps(payload).encode()

    def add(self, service, n, **kw):
        self.entries.append(listing_entry(service, n, **{k: v for k, v in kw.items() if k in ("created", "labels", "names")}))
        self.inspect[cid(n)] = inspect_payload(service, **{k: v for k, v in kw.items() if k not in ("created", "labels", "names")})

    def full(self):
        for i, name in enumerate(sorted(SERVICES), start=1):
            self.add(name, i)
        return self


class PolicyTests(unittest.TestCase):
    def test_allowed_calls(self):
        for target in ("/containers/json", "/containers/json?all=true&filters=%7B%7D", f"/containers/{cid(1)}/json",
                       "/containers/0123456789ab/json", "/events?since=1&until=2", "/events?until=2&since=1&filters=%7B%7D"):
            with self.subTest(target):
                ds.check_call("GET", target)

    def test_other_methods_are_refused(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "get"):
            with self.subTest(method), self.assertRaises(ds.PolicyViolation):
                ds.check_call(method, "/containers/json")

    def test_other_paths_are_refused(self):
        for target in (f"/containers/{cid(1)}/exec", f"/containers/{cid(1)}/logs", f"/containers/{cid(1)}/start", f"/containers/{cid(1)}/stop",
                       f"/containers/{cid(1)}/json/extra", "/containers/ABCDEF123456/json", "/containers/abc/json", "/containers/../json",
                       "/containers/json/", "/images/json", "/info", "/version", "/_ping", "/v1.43/containers/json", "/volumes", "/exec/abc/json",
                       "http://localhost/containers/json", "//evil/containers/json", "/containers/json#x", "", "containers/json"):
            with self.subTest(target), self.assertRaises(ds.PolicyViolation):
                ds.check_call("GET", target)

    def test_events_needs_since_and_until(self):
        for target in ("/events", "/events?since=1", "/events?until=2", "/events?filters=%7B%7D"):
            with self.subTest(target), self.assertRaises(ds.PolicyViolation):
                ds.check_call("GET", target)

    def test_the_real_transport_checks_the_policy_before_connecting(self):
        transport = ds.UnixSocketTransport("/no/such/socket")
        with self.assertRaises(ds.PolicyViolation):
            transport("POST", "/containers/json", 1.0)
        with self.assertRaises(ds.PolicyViolation):
            transport("GET", "/info", 1.0)

    def test_missing_socket_is_runtime_unavailable(self):
        with self.assertRaises(ds.RuntimeUnavailable):
            ds.UnixSocketTransport("/no/such/socket")("GET", "/containers/json", 1.0)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no unix sockets on this platform")
class RealTransportTests(unittest.TestCase):
    def test_get_over_a_real_unix_socket(self):
        import socketserver
        from http.server import BaseHTTPRequestHandler

        seen = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = b'[{"ok": true}]'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

            def address_string(self):
                return "unix"

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        folder = tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None)
        path = os.path.join(folder, "d.sock")
        server = Server(path, H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = ds.UnixSocketTransport(path)("GET", "/containers/json?all=true", 2.0)
        finally:
            server.shutdown()
            server.server_close()
            os.remove(path)
            os.rmdir(folder)
        self.assertEqual((status, json.loads(body)), (200, [{"ok": True}]))
        self.assertEqual(seen, ["/containers/json?all=true"])


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.rt = FakeRuntime().full()
        self.src = ds.DockerSource(SERVICES, self.rt)

    def test_observes_the_six_services(self):
        out = self.src.observe(NOW)
        self.assertEqual([o.service for o in out], sorted(SERVICES))
        self.assertEqual(self.src.status().state, "ok")
        self.assertEqual(self.src.status().since, NOW)

    def test_only_allow_listed_calls_were_made(self):
        self.src.observe(NOW)
        self.assertTrue(all(m == "GET" for m, _t in self.rt.calls))
        paths = {urllib.parse.urlsplit(t).path for _m, t in self.rt.calls}
        self.assertEqual(len(self.rt.calls), 1 + len(SERVICES))
        self.assertEqual(sum(1 for p in paths if p == "/containers/json"), 1)
        listing = urllib.parse.parse_qs(urllib.parse.urlsplit(self.rt.calls[0][1]).query)
        self.assertEqual(listing["all"], ["true"])
        self.assertEqual(json.loads(listing["filters"][0]), {"label": ["sentinelops.service"]})

    def test_extracted_fields(self):
        obs = {o.service: o for o in self.src.observe(NOW)}["orders"]
        self.assertEqual(obs.status, "running")
        self.assertEqual(obs.created, "2026-11-03T09:00:00.123456789Z")
        self.assertEqual(obs.started_at, "2026-11-03T09:00:01.5Z")
        self.assertEqual(obs.restart_count, 0)
        self.assertIs(obs.oom_killed, False)
        self.assertEqual(obs.health, "healthy")
        self.assertEqual((obs.memory_limit, obs.nano_cpus), (268435456, 500000000))
        self.assertEqual(obs.restart_policy, "unless-stopped")
        self.assertEqual(obs.config_hash, "a" * 12)
        self.assertEqual(obs.image, "sha256:" + "b" * 64)

    def test_the_observation_type_holds_only_the_allow_list(self):
        obs = self.src.observe(NOW)[0]
        self.assertEqual(set(vars(obs)), {"service", "image", "created", "status", "started_at", "finished_at", "exit_code", "oom_killed",
                                          "health", "restart_count", "restart_policy", "memory_limit", "nano_cpus", "config_hash"})

    def test_secrets_in_the_inspect_answer_never_reach_the_observation_or_a_bundle(self):
        observations = self.src.observe(NOW)
        self.assertNotIn(SECRET, repr(observations))
        services = fakes.stack()
        services["orders"] = fakes.svc("orders", [fakes.sig("latency_p95")], container=[o for o in observations if o.service == "orders"][0])
        text = json.dumps(test_bundle.build(services=services))
        self.assertNotIn(SECRET, text)
        self.assertNotIn("POSTGRES_PASSWORD", text)
        self.assertNotIn("DATABASE_URL", text)

    def test_health_missing_and_limits_zero(self):
        rt = FakeRuntime()
        rt.add("orders", 1, health=None, memory=0, nano=0)
        obs = ds.DockerSource(SERVICES, rt).observe(NOW)[0]
        self.assertIsNone(obs.health)
        self.assertIsNone(obs.memory_limit)
        self.assertIsNone(obs.nano_cpus)

    def test_wrong_types_become_none(self):
        rt = FakeRuntime()
        rt.add("orders", 1)
        payload = rt.inspect[cid(1)]
        payload["RestartCount"] = "three"
        payload["State"]["OOMKilled"] = "no"
        payload["State"]["ExitCode"] = True
        obs = ds.DockerSource(SERVICES, rt).observe(NOW)[0]
        self.assertIsNone(obs.restart_count)
        self.assertIsNone(obs.oom_killed)
        self.assertIsNone(obs.exit_code)

    def test_other_services_and_one_off_containers_are_ignored(self):
        self.rt.entries.append(listing_entry("prometheus", 90))
        self.rt.entries.append(listing_entry("orders", 91, labels={"com.docker.compose.oneoff": "True"}, created=1_800_000_000))
        self.rt.inspect[cid(90)] = inspect_payload("prometheus")
        self.rt.inspect[cid(91)] = inspect_payload("orders", status="exited")
        out = {o.service: o for o in self.src.observe(NOW)}
        self.assertEqual(sorted(out), sorted(SERVICES))
        self.assertEqual(out["orders"].status, "running")

    def test_newest_container_of_a_service_wins(self):
        rt = FakeRuntime()
        rt.add("orders", 1, created=1_700_000_000, status="exited")
        rt.add("orders", 2, created=1_700_000_500, status="running")
        out = ds.DockerSource(SERVICES, rt).observe(NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].status, "running")

    def test_bad_ids_are_ignored(self):
        rt = FakeRuntime()
        rt.entries.append(dict(listing_entry("orders", 1), Id="../../etc"))
        rt.entries.append(dict(listing_entry("payments", 2), Id=5))
        self.assertEqual(ds.DockerSource(SERVICES, rt).observe(NOW), [])
        self.assertEqual(len(rt.calls), 1)  # only the listing

    def test_marker_containers_are_dropped_before_anything_else(self):
        rt = FakeRuntime().full()
        rt.entries.append(listing_entry("orders", 80, names=["/chaos-loadgen"], created=1_800_000_000))
        rt.entries.append(listing_entry("payments", 81, labels={"note": "CHAOS run"}, created=1_800_000_000))
        rt.inspect[cid(80)] = inspect_payload("orders", status="exited")
        rt.inspect[cid(81)] = inspect_payload("payments", status="exited")
        out = {o.service: o for o in ds.DockerSource(SERVICES, rt).observe(NOW)}
        self.assertEqual(out["orders"].status, "running")
        self.assertEqual(out["payments"].status, "running")
        self.assertNotIn(cid(80), " ".join(t for _m, t in rt.calls))  # never even inspected

    def test_marker_inside_an_inspected_value_drops_the_observation(self):
        rt = FakeRuntime()
        rt.add("orders", 1, image="registry/chaos-tool:1")
        self.assertEqual(ds.DockerSource(SERVICES, rt).observe(NOW), [])

    def test_a_service_without_a_container_is_simply_absent(self):
        rt = FakeRuntime()
        rt.add("orders", 1)
        out = ds.DockerSource(SERVICES, rt).observe(NOW)
        self.assertEqual([o.service for o in out], ["orders"])

    def test_empty_listing_is_ok(self):
        src = ds.DockerSource(SERVICES, FakeRuntime())
        self.assertEqual(src.observe(NOW), [])
        self.assertEqual(src.status().state, "ok")


class FailureTests(unittest.TestCase):
    def test_socket_not_reachable(self):
        rt = FakeRuntime().full()
        rt.fail = "PermissionError: [Errno 13] Permission denied"
        src = ds.DockerSource(SERVICES, rt)
        self.assertIsNone(src.observe(NOW))
        self.assertEqual(src.status().state, "no_data")
        self.assertIn("Permission denied", src.status().detail)

    def test_recovers_when_the_socket_returns(self):
        rt = FakeRuntime().full()
        src = ds.DockerSource(SERVICES, rt)
        rt.fail = "gone"
        src.observe(NOW)
        rt.fail = None
        later = datetime(2026, 11, 3, 10, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(len(src.observe(later)), 6)
        self.assertEqual((src.status().state, src.status().since), ("ok", later))

    def test_listing_errors(self):
        rt = FakeRuntime().full()
        rt.listing_status = 403
        src = ds.DockerSource(SERVICES, rt)
        self.assertIsNone(src.observe(NOW))
        self.assertIn("HTTP 403", src.status().detail)
        rt.listing_status = 200
        rt.entries = {"not": "a list"}
        self.assertIsNone(src.observe(NOW))
        self.assertIn("not a list", src.status().detail)

    def test_every_inspection_failing_is_no_data_a_partial_failure_is_not(self):
        rt = FakeRuntime().full()
        saved = dict(rt.inspect)
        rt.inspect = {}
        src = ds.DockerSource(SERVICES, rt)
        self.assertIsNone(src.observe(NOW))
        self.assertEqual(src.status().state, "no_data")
        rt.inspect = {k: v for i, (k, v) in enumerate(saved.items()) if i != 0}
        out = src.observe(NOW)
        self.assertEqual(len(out), 5)
        self.assertEqual(src.status().state, "ok")
        self.assertIn("1 container inspection(s) failed", src.status().detail)

    def test_garbage_inspection(self):
        rt = FakeRuntime()
        rt.entries.append(listing_entry("orders", 1))
        rt.inspect[cid(1)] = ["not", "a", "dict"]
        src = ds.DockerSource(SERVICES, rt)
        self.assertIsNone(src.observe(NOW))

    def test_a_transport_that_lets_os_errors_through(self):
        def transport(method, target, timeout):
            raise ConnectionRefusedError("refused")

        src = ds.DockerSource(SERVICES, transport)
        self.assertIsNone(src.observe(NOW))
        self.assertEqual(src.status().state, "no_data")


class ModeTests(unittest.TestCase):
    def test_off_makes_no_call(self):
        rt = FakeRuntime().full()
        src = ds.DockerSource(SERVICES, rt, mode="off")
        self.assertIsNone(src.observe(NOW))
        self.assertIsNone(src.events(0, 10))
        self.assertEqual(rt.calls, [])
        self.assertEqual(src.status().state, "off")
        self.assertIn("EVIDENCE_DOCKER_SOURCE=off", src.status().detail)

    def test_create_from_environment(self):
        self.assertEqual(ds.create({}, SERVICES).status().state, "no_data")
        self.assertEqual(ds.create({"EVIDENCE_DOCKER_SOURCE": "OFF"}, SERVICES).status().state, "off")
        self.assertEqual(ds.create({"EVIDENCE_DOCKER_SOURCE": " on "}, SERVICES).status().state, "no_data")
        with self.assertRaises(ValueError):
            ds.create({"EVIDENCE_DOCKER_SOURCE": "maybe"}, SERVICES)
        with self.assertRaises(ValueError):
            ds.DockerSource(SERVICES, FakeRuntime(), mode="maybe")

    def test_create_uses_the_configured_socket(self):
        src = ds.create({"EVIDENCE_DOCKER_SOCKET": "/tmp/x.sock"}, SERVICES)
        self.assertEqual(src._transport.path, "/tmp/x.sock")
        self.assertEqual(ds.create({}, SERVICES)._transport.path, "/var/run/docker.sock")


class EventTests(unittest.TestCase):
    def line(self, service="orders", action="die", t=1_700_000_000, extra=None):
        item = {"Type": "container", "Action": action, "time": t, "Actor": {"ID": cid(1), "Attributes": {"sentinelops.service": service, "name": "n"}}}
        item.update(extra or {})
        return json.dumps(item)

    def test_events_are_parsed_and_allow_listed(self):
        rt = FakeRuntime()
        rt.events_body = "\n".join([
            self.line(action="start", t=20), self.line(action="die", t=10), self.line(action="health_status: unhealthy", t=30),
            self.line(action="exec_start: sh -c 'echo secret'", t=40), self.line(service="prometheus"), self.line(action="attach"),
            self.line(extra={"Actor": {"Attributes": {"sentinelops.service": "orders", "image": "chaos/x"}}}), "not json", "", "[1]",
        ]).encode()
        events = ds.DockerSource(SERVICES, rt).events(0, 100)
        self.assertEqual([(e.service, e.action, int(e.ts.timestamp())) for e in events],
                         [("orders", "die", 10), ("orders", "start", 20), ("orders", "health_status", 30)])
        method, target = rt.calls[-1]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
        self.assertEqual((method, query["since"], query["until"]), ("GET", ["0"], ["100"]))
        self.assertEqual(json.loads(query["filters"][0]), {"type": ["container"]})

    def test_events_unavailable(self):
        rt = FakeRuntime()
        rt.fail = "down"
        self.assertIsNone(ds.DockerSource(SERVICES, rt).events(0, 1))


if __name__ == "__main__":
    unittest.main()
