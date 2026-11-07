"""End to end without sockets: fake Prometheus, Loki, detector and container runtime behind the real wiring
(build_runtime), the real collector, loop, store and API."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.parse

import _bootstrap  # noqa: F401
from _bootstrap import EVIDENCE, TOPOLOGY_PATH
from fakes import TOPOLOGY, at

import test_collector as tc
import test_docker_source as tds
import test_loop as tl
from fakes_io import FakeHttp, ok, streams_payload, vector_payload

from app import api
from app import docker_source as ds
from app import main as mn
from app.model import fmt_ts


class MultiHttp:
    def __init__(self):
        self.hosts = {"prometheus:9090": FakeHttp(), "loki:3100": FakeHttp(), "detector:9100": FakeHttp()}

    def __call__(self, method, url, body, timeout):
        return self.hosts[urllib.parse.urlsplit(url).netloc](method, url, body, timeout)


class Stack(unittest.TestCase):
    docker_mode = "on"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = os.path.join(self.tmp.name, "config")
        shutil.copytree(os.path.join(EVIDENCE, "config"), config)
        env = {"DB_PATH": os.path.join(self.tmp.name, "e.db"), "TOPOLOGY_PATH": TOPOLOGY_PATH, "CONFIG_DIR": config, "WARMUP_S": "0",
               "SETTLE_S": "0", "EVIDENCE_DOCKER_SOURCE": self.docker_mode}
        self.http = MultiHttp()
        self.prom = tc.PromScript(self.http.hosts["prometheus:9090"])
        self.runtime_fake = tds.FakeRuntime().full()
        runtime_source = ds.DockerSource(TOPOLOGY.services, self.runtime_fake, mode=self.docker_mode)
        self.logger = mn.setup_logging("DEBUG", "t", __import__("io").StringIO())
        self.rt = mn.build_runtime(mn.parse_settings(env), env, self.logger, transport=self.http, runtime_source=runtime_source)
        self.clock = tl.Clock(1200.0)
        self.rt.loop.clock = self.clock
        self.detector = self.http.hosts["detector:9100"]
        self.loki = self.http.hosts["loki:3100"]
        self.loki.routes["/loki/api/v1/query"] = lambda p, m: ok(vector_payload([]))
        self.loki.routes["/loki/api/v1/query_range"] = lambda p, m: ok(streams_payload([]))
        self.set_incident(tl.inc())
        self.f10_series()

    def tearDown(self):
        self.rt.collector.close()
        self.rt.store.close()
        self.tmp.cleanup()

    def set_incident(self, *items):
        self.detector.routes["/internal/v1/incidents"] = lambda p, m: ok({"incidents": list(items)})

    def f10_series(self):
        self.prom.on('count(container_memory_working_set_bytes{service="postgres"}) or vector(0)',
                     series=[({}, [(t, 1 if t < tc.ts(1000) else 0) for t in tc.steps(580, 1200)])])
        for service in ("orders", "inventory", "payments"):
            self.prom.on(f'dependency_requests_total{{job="{service}"', series=[({"dependency": "postgres"}, tc.rising(0, 0.4, switch=1005))])
        self.prom.on("status=~", 'service="gateway"', series=[({}, tc.rising(0, 40, switch=1010))])

    def bundle(self):
        return self.rt.ctx.reader.latest_bundle("INC-0001")


class EndToEndTests(Stack):
    def test_postgres_outage(self):
        # the poller sees postgres running, then exited; the exited container carries its own finish time
        self.rt.poller.poll_once(at(1195))
        self.runtime_fake.inspect[tds.cid(5)] = tds.inspect_payload("postgres", status="exited", finished=fmt_ts(at(1000)))
        self.rt.poller.poll_once(at(1200))
        self.rt.loop.run_cycle()
        bundle = self.bundle()
        self.assertEqual(bundle["trigger"], "opened")
        self.assertEqual([s["state"] for s in bundle["sources"].values()], ["ok"] * 5)
        top = bundle["suspects"][0]
        self.assertEqual(top["service"], "postgres")
        self.assertIn("container missing (no memory series)", top["reasons"])
        self.assertIn("container state is exited", top["reasons"])
        self.assertIn("named primary_service by the detector", top["reasons"])
        self.assertEqual(bundle["services"]["postgres"]["container"]["state"], "exited")
        changes = bundle["services"]["postgres"]["changes"]
        self.assertEqual([(c["kind"], c["ts"]) for c in changes], [("stopped", fmt_ts(at(1000)))])  # its own time, not the poll time
        self.assertEqual(bundle["graph"]["shared_dependencies"]["postgres"], ["inventory", "orders", "payments"])
        self.assertEqual(bundle["data_gaps"], [])
        self.assertLess(bundle["build_ms"], 5000)

    def test_nothing_secret_and_no_marker_in_the_stored_bundle(self):
        self.runtime_fake.entries.append(tds.listing_entry("orders", 90, names=["/chaos-F01-proc"], created=1_900_000_000))
        self.runtime_fake.inspect[tds.cid(90)] = tds.inspect_payload("orders", status="exited")
        self.rt.poller.poll_once(at(1200))
        self.rt.loop.run_cycle()
        text = json.dumps(self.bundle())
        self.assertNotIn(tds.SECRET, text)
        self.assertNotIn("chaos", text.lower())
        self.assertNotIn("POSTGRES_PASSWORD", text)
        self.assertLess(len(text.encode("utf-8")), 64 * 1024)

    def test_api_serves_the_stored_bundle_and_the_status(self):
        self.rt.poller.poll_once(at(1200))
        self.rt.loop.run_cycle()
        self.rt.ctx.mono = self.clock.mono
        server = api.make_server(self.rt.ctx, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def get(path):
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}{path}", timeout=5) as r:
                    return r.status, json.loads(r.read())

            self.assertEqual(get("/internal/v1/evidence/INC-0001"), (200, self.bundle()))
            status, body = get("/internal/v1/status")
            self.assertEqual(body["sources"]["docker"]["state"], "ok")
            self.assertEqual(body["sources"]["prometheus"]["state"], "ok")
            self.assertEqual((body["bundles_total"], body["cycles"]), (1, 1))
            self.assertEqual(get("/ready")[0], 200)
            self.assertEqual(get("/internal/v1/bundles/EVD-00001")[1]["incident_id"], "INC-0001")
            self.assertEqual(get("/internal/v1/evidence/INC-0001/bundles")[1]["bundles"][0]["trigger"], "opened")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_prometheus_down_builds_nothing_and_resumes_when_it_returns(self):
        self.http.hosts["prometheus:9090"].ready_status = 503
        self.rt.loop.run_cycle()
        self.assertEqual(self.rt.ctx.reader.counts()["total"], 0)
        self.assertEqual(self.rt.loop.state.snapshot().sources["prometheus"].state, "no_data")
        self.http.hosts["prometheus:9090"].ready_status = 200
        self.clock.advance(5)
        self.rt.loop.run_cycle()
        self.assertEqual(self.bundle()["trigger"], "opened")

    def test_loki_down_gives_a_bundle_with_log_gaps(self):
        self.loki.ready_status = 503
        self.rt.loop.run_cycle()
        bundle = self.bundle()
        self.assertEqual(bundle["sources"]["loki"]["state"], "no_data")
        self.assertTrue(all(s["logs"]["state"] == "no_data" for s in bundle["services"].values()))
        self.assertEqual(bundle["suspects"][0]["service"], "postgres")

    def test_a_refused_runtime_socket_gives_a_bundle_without_containers(self):
        self.runtime_fake.fail = "PermissionError: [Errno 13] Permission denied"
        self.rt.poller.poll_once(at(1200))
        self.rt.loop.run_cycle()
        bundle = self.bundle()
        self.assertEqual(bundle["sources"]["docker"]["state"], "no_data")
        self.assertIn("Permission denied", bundle["sources"]["docker"]["detail"])
        self.assertTrue(all(s["container"] is None for s in bundle["services"].values()))
        self.assertEqual(bundle["suspects"][0]["service"], "postgres")  # Prometheus alone is enough

    def test_one_failed_prometheus_signal_is_a_gap_not_a_failed_bundle(self):
        prom_http = self.http.hosts["prometheus:9090"]
        inner = prom_http.routes["/api/v1/query_range"]
        prom_http.routes["/api/v1/query_range"] = lambda params, method: (
            (500, b'{"status":"error","error":"boom"}') if "histogram_quantile" in params["query"] and 'service="orders"' in params["query"] else inner(params, method))
        self.rt.loop.run_cycle()
        gaps = [g for g in self.bundle()["data_gaps"] if g["service"] == "orders"]
        self.assertEqual([g["signal"] for g in gaps], ["latency_p95"])  # the dependency query is selected by job, not service, so it still works
        self.assertEqual(self.bundle()["suspects"][0]["service"], "postgres")


class RuntimeOffTests(Stack):
    docker_mode = "off"

    def test_runtime_source_off_still_gives_a_working_bundle(self):
        self.rt.poller.poll_once(at(1200))
        self.rt.loop.run_cycle()
        bundle = self.bundle()
        self.assertEqual(bundle["sources"]["docker"]["state"], "off")
        self.assertIn("EVIDENCE_DOCKER_SOURCE=off", bundle["sources"]["docker"]["detail"])
        self.assertEqual(self.runtime_fake.calls, [])
        self.assertTrue(all(s["container"] is None and s["changes"] == [] for s in bundle["services"].values()))
        self.assertEqual(bundle["suspects"][0]["service"], "postgres")
        self.assertTrue(any(g["source"] == "docker" for g in bundle["data_gaps"]))


if __name__ == "__main__":
    unittest.main()
