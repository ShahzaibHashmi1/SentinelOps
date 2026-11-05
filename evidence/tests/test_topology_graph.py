import unittest

import _bootstrap  # noqa: F401
from _bootstrap import TOPOLOGY_PATH

from app.graph import DependencyGraph, GraphError
from app.topology import TopologyError, load_topology, parse_topology


class TopologyTests(unittest.TestCase):
    def test_real_file(self):
        t = load_topology(TOPOLOGY_PATH)
        self.assertEqual(t.services, ("gateway", "inventory", "orders", "payments", "postgres", "redis"))
        self.assertEqual(t.deps["orders"], ("inventory", "payments", "postgres"))
        self.assertEqual(t.tiers["gateway"], "app")
        self.assertEqual(t.tiers["postgres"], "infra")
        self.assertEqual(t.tiers["redis"], "infra")

    def test_tier_of_unknown_service(self):
        with self.assertRaises(TopologyError):
            load_topology(TOPOLOGY_PATH).tier("nope")

    def test_comments_blank_lines_crlf_and_bom(self):
        t = parse_topology("# c\r\n\r\na:\r\n  - b  # trailing\r\n")
        self.assertEqual(t.deps, {"a": ("b",)})

    def test_rejects_bad_input(self):
        bad = {
            "empty": "",
            "only comments": "# nothing\n",
            "dependency before caller": "  - a\n",
            "twice": "a:\n  - b\na:\n  - c\n",
            "self": "a:\n  - a\n",
            "duplicate dependency": "a:\n  - b\n  - b\n",
            "caller without dependency": "a:\n",
            "garbage": "a:\n  - b\nthis is not yaml\n",
            "inline list": "a: [b, c]\n",
            "uppercase": "A:\n  - b\n",
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(TopologyError):
                parse_topology(text)

    def test_missing_file(self):
        with self.assertRaises(TopologyError):
            load_topology("/no/such/topology.yaml")


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.g = DependencyGraph(load_topology(TOPOLOGY_PATH))

    def test_nodes_and_edges(self):
        self.assertEqual(self.g.nodes(), ["gateway", "inventory", "orders", "payments", "postgres", "redis"])
        self.assertIn(["orders", "postgres"], self.g.edges())
        self.assertEqual(len(self.g.edges()), 9)

    def test_direct_and_transitive(self):
        self.assertEqual(self.g.dependencies("orders"), ["inventory", "payments", "postgres"])
        self.assertEqual(self.g.dependents("postgres"), ["inventory", "orders", "payments"])
        self.assertEqual(self.g.descendants("gateway"), ["inventory", "orders", "payments", "postgres", "redis"])
        self.assertEqual(self.g.ancestors("postgres"), ["gateway", "inventory", "orders", "payments"])
        self.assertEqual(self.g.ancestors("gateway"), [])
        self.assertEqual(self.g.descendants("redis"), [])

    def test_depth(self):
        self.assertEqual(self.g.depth(), {"gateway": 3, "inventory": 1, "orders": 2, "payments": 1, "postgres": 0, "redis": 0})

    def test_shared_dependencies_direct_callers_only(self):
        self.assertEqual(self.g.shared_dependencies(["gateway", "orders"]), {"payments": ["gateway", "orders"]})
        self.assertEqual(self.g.shared_dependencies(["orders", "inventory", "payments"]),
                         {"postgres": ["inventory", "orders", "payments"], "redis": ["inventory", "payments"]})

    def test_shared_dependencies_none_and_min(self):
        self.assertEqual(self.g.shared_dependencies([]), {})
        self.assertEqual(self.g.shared_dependencies(["orders"]), {})
        self.assertEqual(self.g.shared_dependencies(["orders"], min_dependents=1)["postgres"], ["orders"])

    def test_unknown_service(self):
        for call in (self.g.dependencies, self.g.dependents, self.g.ancestors, self.g.descendants):
            with self.assertRaises(GraphError):
                call("nope")
        with self.assertRaises(GraphError):
            self.g.shared_dependencies(["nope"])

    def test_cycle_is_rejected(self):
        with self.assertRaises(GraphError) as ctx:
            DependencyGraph(parse_topology("a:\n  - b\nb:\n  - c\nc:\n  - a\n"))
        self.assertIn("cycle", str(ctx.exception))

    def test_section_shape(self):
        sec = self.g.section(["orders", "inventory"])
        self.assertEqual(list(sec), ["nodes", "edges", "depth", "shared_dependencies"])
        self.assertEqual(sec["shared_dependencies"], {"postgres": ["inventory", "orders"]})


if __name__ == "__main__":
    unittest.main()
