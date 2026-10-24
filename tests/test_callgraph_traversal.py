import re
import unittest
from unittest.mock import patch

from codegraphhunter.web.callgraph_query_backend import CallGraphQueryBackend, CallGraphQueryPlan, CallGraphQueryError
from codegraphhunter.web.callgraph_traversal import traverse_callgraph
from codegraphhunter.web.database_client import DatabaseQueryTimeout
from tests.test_callgraph_query_backend import FakeLLMClient
from tests.evidence_assertions import assert_evidence_paths


class GraphBolt:
    def __init__(self, nodes=None, edges=()):
        self.nodes = nodes or {}
        self.edges = list(edges)
        self.calls = []

    def run(self, cypher, params=None, timeout_s=30):
        self.calls.append(dict(cypher=cypher, params=params, timeout_s=timeout_s))
        limit = int(re.search(r"LIMIT (\d+)", cypher)[1])
        offset = int(re.search(r"SKIP (\d+)", cypher)[1])
        if "function_name" in params:
            matching = sorted(node for node, name in self.nodes.items() if params["function_name"] in name)
            ids = matching[offset:offset + limit]
            return {"records": [{"node_id": node, "node_name": self.nodes[node]} for node in ids]}
        incoming = "<-" in cypher
        rows = []
        for index, edge in enumerate(self.edges):
            source, target = edge[:2]
            properties = edge[2] if len(edge) > 2 else {"csid": index, "kind": 1}
            if (target if incoming else source) != params["frontier_id"]:
                continue
            node = source if incoming else target
            row = {"node_id": node, "node_name": self.nodes[node]}
            row.update({f"edge_{name}": properties.get(name) for name in
                        ("csid", "kind", "direct_call_set", "indirect_call_set")})
            if row not in rows:
                rows.append(row)
        rows.sort(key=lambda row: (row["node_id"], row.get("edge_csid") or 0))
        return {"records": rows[offset:offset + limit]}


class CallGraphTraversalTests(unittest.TestCase):
    def plan(self, depth=40, limit=100, intent="find_reachable_callees", name="root"):
        return CallGraphQueryPlan(intent, name, depth, limit, "Reachability test.")

    def run_plan(self, plan, graph):
        return traverse_callgraph(plan, lambda query, params, timeout: graph.run(query, params, timeout))

    def test_dense_cyclic_graph_stops_at_path_limit_with_cached_reads(self):
        graph = GraphBolt({0: "root", **{i: str(i) for i in range(1, 15)}},
                          [(a, b) for a in range(15) for b in range(15)])
        rows, reads, strategy, evidence = self.run_plan(self.plan(), graph)
        self.assertEqual(len(rows), 100)
        self.assertLessEqual(len(reads), len(graph.nodes) + 1)
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["stop_reason"], "result_limit")
        self.assertTrue(all("*" not in call["cypher"] for call in graph.calls))
        self.assertEqual(strategy["max_depth"], 40)

    def test_depth_is_respected_and_empty_depth_reaches_a_finite_fixed_point(self):
        edges = [(0, 1), (1, 2), (2, 3), (3, 1), (3, 4)]
        for depth, expected in ((1, {1}), (2, {1, 2}), (40, {1, 2, 3, 4}), (None, {1, 2, 3, 4})):
            graph = GraphBolt({0: "root", **{i: str(i) for i in range(1, 5)}}, edges)
            rows, _, _, _ = self.run_plan(self.plan(depth=depth), graph)
            self.assertEqual({row["callee_id"] for row in rows}, expected)
            self.assertEqual(len(rows), len(expected))

    def test_alternative_call_distances_and_incoming_direction_are_preserved(self):
        graph = GraphBolt({0: "root", 1: "a", 2: "b", 3: "c"}, [(1, 0), (2, 1), (3, 2), (3, 1)])
        rows, _, _, _ = self.run_plan(self.plan(intent="find_reachable_callers"), graph)
        self.assertEqual({(row["caller_id"], row["call_distance"]) for row in rows}, {(1, 1), (2, 2), (3, 2), (3, 3)})
        self.assertEqual({row["target_id"] for row in rows}, {0})

    def test_multiple_seed_matches_are_paged_until_results_are_found(self):
        graph = GraphBolt({0: "root", 1: "root_one", 2: "root_two", 3: "leaf"}, [(2, 3)])
        rows, _, _, _ = self.run_plan(self.plan(limit=1), graph)
        self.assertEqual(rows[0]["source_id"], 2)
        self.assertEqual(rows[0]["callee_id"], 3)
        self.assertTrue(any("SKIP 2" in call["cypher"] for call in graph.calls))

    def test_return_limit_is_applied_to_paths(self):
        graph = GraphBolt({0: "root", **{i: str(i) for i in range(1, 20)}}, [(0, i) for i in range(1, 20)])
        rows, _, _, _ = self.run_plan(self.plan(limit=3), graph)
        self.assertEqual(len(rows), 3)
        self.assertLessEqual(len(graph.calls), 5)

    def test_whole_traversal_has_one_deadline(self):
        graph = GraphBolt({0: "root", 1: "a"}, [(0, 1)])
        with patch("codegraphhunter.web.callgraph_traversal.time.monotonic", side_effect=[0, 0, 0, 31]):
            with self.assertRaises(DatabaseQueryTimeout):
                self.run_plan(self.plan(), graph)

    def test_backend_guards_every_read_and_reports_database_progress(self):
        graph = GraphBolt({0: "is_valid_path", 1: "caller", 2: "entry"}, [(1, 0), (2, 1), (1, 2)])
        llm = FakeLLMClient(dict(intent="find_reachable_callers", function_name="is_valid_path", depth=40, limit=100, rationale="Find callers."))
        backend = CallGraphQueryBackend(environ={}, llm_client=llm, bolt_client=graph)
        progress = []
        result = backend.execute("Find entry functions that can reach is_valid_path", max_depth=40, max_entities=100, on_progress=progress.append)
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["execution_strategy"]["max_depth"], 40)
        self.assertEqual(result["execution_strategy"]["max_hops_reached"], 2)
        self.assertTrue(all(item["guard"]["status"] == "ok" for item in result["execution_queries"]))
        self.assertEqual(progress[0]["stage"], "planning")
        self.assertEqual(progress[1]["stage"], "database")
        self.assertIn("*1..40]", progress[1]["cypher"])
        self.assertTrue(any("hop" in item["message"] for item in progress[2:]))

    def test_backend_database_timeouts_are_explicit(self):
        graph = GraphBolt()
        graph.run = lambda *args, **kwargs: (_ for _ in ()).throw(DatabaseQueryTimeout("Deadline reached."))
        llm = FakeLLMClient(dict(intent="find_reachable_callers", function_name="root", depth=40, limit=100, rationale="Find callers."))
        backend = CallGraphQueryBackend(environ={}, llm_client=llm, bolt_client=graph)
        with self.assertRaises(CallGraphQueryError) as caught:
            backend.execute("Find reachable callers.", max_depth=40, max_entities=100)
        self.assertEqual(caught.exception.code, "DATABASE_QUERY_TIMEOUT")
        self.assertEqual(caught.exception.http_status, 504)

    def test_complete_paths_use_real_hops_in_both_directions_with_cycles(self):
        for incoming in (False, True):
            edges = [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4), (4, 0)]
            if incoming:
                edges = [(target, source) for source, target in edges]
            graph = GraphBolt({0: "root", 1: "step a", 2: "step b", 3: "join", 4: "leaf"}, edges)
            plan = dict(intent="find_reachable_callers" if incoming else "find_reachable_callees",
                        function_name="root", depth=40, limit=100, rationale="Find full paths.")
            backend = CallGraphQueryBackend(environ={}, llm_client=FakeLLMClient(plan), bolt_client=graph)
            result = backend.execute("Find reachable functions", max_depth=40, max_entities=100)
            actual_edges = {(("CallGraphNode", a), ("CallGraphNode", b), "CallGraphEdge") for a, b in edges}
            assert_evidence_paths(self, result, actual_edges)
            self.assertEqual(len(result["evidence_graph"]["nodes"]), 5)
            pairs = {(edge["source"]["id"], edge["target"]["id"]) for edge in result["evidence_graph"]["edges"]}
            self.assertNotIn((3, 0) if incoming else (0, 3), pairs)
            self.assertEqual({node["properties"]["name"] for node in result["evidence_graph"]["nodes"]}, set(graph.nodes.values()))

    def test_shared_hops_merge_across_multiple_matched_seed_functions(self):
        graph = GraphBolt({0: "root a", 1: "root b", 2: "middle", 3: "leaf"}, [(0, 2), (1, 2), (2, 3)])
        plan = dict(intent="find_reachable_callees", function_name="root", depth=40, limit=100, rationale="Find full paths.")
        backend = CallGraphQueryBackend(environ={}, llm_client=FakeLLMClient(plan), bolt_client=graph)
        result = backend.execute("Find reachable functions", max_depth=40, max_entities=100)
        assert_evidence_paths(self, result, {(("CallGraphNode", a), ("CallGraphNode", b), "CallGraphEdge") for a, b in graph.edges})
        shared = next(edge for edge in result["evidence_graph"]["edges"] if edge["source"]["id"] == 2)
        self.assertEqual(shared["records"], [1, 3])
        self.assertEqual(len(result["evidence_graph"]["edges"]), 3)

    def test_same_endpoints_retain_all_different_lengths_and_call_sites(self):
        edges = [(0, 1, {"csid": 21, "kind": 1, "direct_call_set": "a"}),
                 (0, 1, {"csid": 22, "kind": 1, "direct_call_set": "b"}),
                 (1, 2), (0, 2), (2, 3), (1, 3)]
        graph = GraphBolt({0: "root", 1: "branch", 2: "join", 3: "leaf"}, edges)
        plan = dict(intent="find_reachable_callees", function_name="root", depth=40, limit=100, rationale="All paths.")
        backend = CallGraphQueryBackend(environ={}, llm_client=FakeLLMClient(plan), bolt_client=graph)
        result = backend.execute("Find reachable callees", max_depth=40, max_entities=100)
        assert_evidence_paths(self, result, {(("CallGraphNode", e[0]), ("CallGraphNode", e[1]), "CallGraphEdge") for e in edges})
        leaf = [row["call_distance"] for row in result["records"] if row["callee_id"] == 3]
        self.assertEqual(sorted(leaf), [2, 2, 2, 3, 3])
        first_hops = [e["properties"]["csid"] for e in result["evidence_graph"]["edges"] if e["target"]["id"] == 1]
        self.assertEqual(first_hops, [21, 22])
        self.assertTrue(result["evidence_graph"]["complete"])
        # Property names containing SET are accepted; executable SET/DELETE
        # clauses remain forbidden.
        for clause in ("SET node.fun_name = 'x'", "DELETE node", "DETACH DELETE node", "CREATE (other:CallGraphNode)"):
            checked = backend.query_guard.validate_for_execution(f"MATCH (node:CallGraphNode) {clause} RETURN node.id LIMIT 1")
            self.assertEqual(checked["reason"], "WRITE_OPERATION_FORBIDDEN")

    def test_database_deadline_keeps_completed_paths_and_marks_them_partial(self):
        graph = GraphBolt({0: "root", 1: "leaf"}, [(0, 1)])
        original = graph.run
        def read(query, params, timeout):
            if len(graph.calls) >= 2:
                raise DatabaseQueryTimeout("Deadline reached.")
            return original(query, params, timeout)
        rows, _, strategy, evidence = traverse_callgraph(self.plan(), read)
        self.assertEqual(len(rows), 1)
        self.assertFalse(evidence["complete"])
        self.assertEqual(strategy["stop_reason"], "timeout")
