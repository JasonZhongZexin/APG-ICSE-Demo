import re
import unittest
from unittest.mock import Mock, patch

from codegraphhunter.web.database_client import DatabaseQueryTimeout
from codegraphhunter.web.icfg_query_backend import ICFGQueryBackend, ICFGQueryError
from codegraphhunter.web.pag_query_backend import PAGQueryBackend, PAGQueryError
from tests.test_icfg_query_backend import FakeLLMClient
from tests import test_icfg_query_backend as icfg_fixtures
from tests import test_pag_query_backend as pag_fixtures
from tests.evidence_assertions import assert_evidence_paths


class TypedGraphBolt:
    """In-memory graph interpreting the generated labeled, one-hop read batches."""

    def __init__(self, nodes=None, edges=(), functions=()):
        self.nodes = nodes or {}
        self.edges = list(edges)
        self.functions = list(functions)
        self.calls = []

    def project(self, query, variable, values):
        return {alias: values.get(prop) for prop, alias in re.findall(
            rf"\b{variable}\.([a-z_]+) AS ([a-z_]+)", query)}

    def run(self, cypher, params=None, timeout_s=30):
        self.calls.append(dict(cypher=cypher, params=params, timeout_s=timeout_s))
        if "function.all_args_node_ids" in cypher:
            return {"records": self.functions}
        if cypher.startswith("MATCH (seed:") and "SKIP" in cypher:
            label = re.search(r"\(seed:(\w+)\)", cypher)[1]
            offset, limit = (int(re.search(rf"{clause} (\d+)", cypher)[1]) for clause in ("SKIP", "LIMIT"))
            rows = []
            for (node_label, node_id), values in sorted(self.nodes.items()):
                values = dict(values, id=node_id)
                if label != node_label:
                    continue
                if any((values.get(prop) != params[param]) for prop, param in re.findall(r"seed\.(\w+) = \$(\w+)", cypher)):
                    continue
                if any(params[param] not in str(values.get(prop, "")) for prop, param in re.findall(r"seed\.(\w+) CONTAINS \$(\w+)", cypher)):
                    continue
                if "argument_ids" in params and node_id not in params["argument_ids"]:
                    continue
                rows.append(self.project(cypher, "seed", dict(values, id=node_id)))
            return {"records": rows[offset:offset + limit]}
        rows = []
        for branch in cypher.split("\nUNION ALL\n"):
            front, front_label = re.search(r"\((frontier\d+):(\w+) \{id: \$frontier_id\d+\}\)", branch).groups()
            node, node_label = re.search(r"\((node\d+):(\w+)\)", branch).groups()
            flow, edge_label = re.search(r"\[(flow\d+):(\w+)\]", branch).groups()
            index = int(re.search(r"(\d+) AS branch_id", branch)[1])
            limit = int(re.search(r"LIMIT (\d+)", branch)[1])
            offset = int(re.search(r"SKIP (\d+)", branch)[1])
            incoming = "<-" in branch
            frontier_id = params[f"frontier_id{index}"]
            branch_rows = []
            for edge_index, item in enumerate(self.edges):
                left, right, edge = item[:3]
                edge_properties = item[3] if len(item) > 3 else {"kind": 1, "edge_id": edge_index}
                source, target = (right, left) if incoming else (left, right)
                if (edge == edge_label and source[0] == front_label and target[0] == node_label
                        and source[1] == frontier_id):
                    row = self.project(branch, node, dict(self.nodes[target], id=target[1]))
                    row.update(self.project(branch, flow, edge_properties))
                    row.update(node_label=node_label, branch_id=index)
                    if row not in branch_rows:
                        branch_rows.append(row)
            branch_rows.sort(key=lambda row: (row["node_id"], row.get("edge_edge_id") or 0))
            rows.extend(branch_rows[offset:offset + limit])
        return {"records": rows}


class TypedGraphTraversalTests(unittest.TestCase):
    def cases(self):
        yield ICFGQueryBackend, ICFGQueryError, icfg_fixtures.ICFGQueryBackendTests._plan, "IntraICFGNode", "CallICFGNode", "IntraCFGEdge"
        yield PAGQueryBackend, PAGQueryError, pag_fixtures.PAGQueryBackendTests.plan, "ValVar", "ObjVar", "CopyStmt"

    def backend(self, case, nodes, edges, **overrides):
        cls, _, plan_factory, result, _, edge = case
        plan = plan_factory(intent="find_reachable_successors", seed_label=result, result_label=result,
                            edge_label=edge, node_id=0, **overrides)
        bolt = TypedGraphBolt(nodes, edges)
        return cls(environ={}, llm_client=FakeLLMClient(plan), bolt_client=bolt), bolt

    def test_dense_cycles_stop_at_path_limit_and_only_one_hop_reads_reach_database(self):
        for case in self.cases():
            label, edge = case[3], case[5]
            nodes = {(label, i): {} for i in range(10)}
            edges = [((label, a), (label, b), edge) for a in range(10) for b in range(10)]
            backend, bolt = self.backend(case, nodes, edges)
            progress = []
            result = backend.execute("Find reachable nodes", max_depth=40, max_entities=100, on_progress=progress.append)
            self.assertEqual(result["record_count"], 100)
            self.assertFalse(result["evidence_graph"]["complete"])
            label_count = len(backend.schema["graphs"][result["graph"]]["node_labels"])
            self.assertLessEqual(len(bolt.calls), 1 + len(nodes) * label_count)
            self.assertTrue(all("*" not in call["cypher"] for call in bolt.calls))
            self.assertTrue(all(not re.search(r"frontier\d+\.id IN", call["cypher"]) for call in bolt.calls))
            self.assertTrue(all(re.search(r"frontier\d+:\w+ \{id: \$frontier_id\d+\}", call["cypher"])
                                for call in bolt.calls[1:]))
            self.assertTrue(all(item["guard"]["status"] == "ok" for item in result["execution_queries"]))
            self.assertEqual(result["execution_strategy"]["max_depth"], 40)
            self.assertLessEqual(result["execution_strategy"]["max_hops_reached"], 10)
            assert_evidence_paths(self, result, set(edges))
            self.assertEqual(progress[0]["stage"], "planning")
            self.assertIn("*1..40]", progress[1]["cypher"])
            self.assertTrue(any("hop" in p["message"] for p in progress[2:]))

    def test_mixed_labels_colliding_ids_shortest_distances_depth_and_direction(self):
        for case in self.cases():
            label, other, edge = case[3:]
            # id=1 exists under both labels; both must be expanded.
            nodes = {(label, 0): {}, (label, 1): {}, (other, 1): {}, (label, 2): {}, (label, 3): {}}
            edges = [((label, 0), (other, 1), edge), ((other, 1), (label, 1), edge),
                     ((label, 1), (label, 2), edge), ((label, 2), (other, 1), edge),
                     ((label, 0), (label, 3), edge), ((label, 3), (label, 2), edge)]
            for incoming in (False, True):
                actual_edges = [(b, a, e) for a, b, e in edges] if incoming else edges
                for depth, expected in ((1, {3: 1}), (2, {1: 2, 2: 2, 3: 1}), (40, {1: 2, 2: 2, 3: 1}), (None, {1: 2, 2: 2, 3: 1})):
                    backend, _ = self.backend(case, nodes, actual_edges)
                    backend._llm_client.responses.output["intent"] = "find_reachable_predecessors" if incoming else "find_reachable_successors"
                    result = backend.execute("Find reachable nodes", max_depth=depth)
                    distances = {}
                    for row in result["records"]:
                        distances[row["result_id"]] = min(distances.get(row["result_id"], 1000), row["path_length"])
                    self.assertEqual(distances, expected)
                    if depth is None or depth >= 40:
                        self.assertEqual({(r["result_id"], r["path_length"]) for r in result["records"]},
                                         {(3, 1), (1, 2), (2, 2), (2, 3), (1, 4)})
                    assert_evidence_paths(self, result, set(actual_edges))

    def test_large_intermediate_frontiers_are_paged_without_applying_result_cap(self):
        for case in self.cases():
            label, other, edge = case[3:]
            nodes = {(label, 0): {}, (label, 9): {}, **{(other, i): {} for i in range(7)}}
            edges = [((label, 0), (other, i), edge) for i in range(7)] + [((other, 6), (label, 9), edge)]
            backend, bolt = self.backend(case, nodes, edges)
            with patch("codegraphhunter.web.typed_graph_traversal.NEIGHBOR_PAGE_SIZE", 3):
                result = backend.execute("Find reachable nodes", max_entities=1)
            self.assertEqual([r["result_id"] for r in result["records"]], [9])
            self.assertTrue(any("SKIP 6" in call["cypher"] for call in bolt.calls))
            self.assertEqual({(node["label"], node["id"]) for node in result["evidence_graph"]["nodes"]},
                             {(label, 0), (other, 6), (label, 9)})
            assert_evidence_paths(self, result, set(edges))

    def test_depth_above_twenty_and_unbounded_queries_reach_the_expected_layers(self):
        for case in self.cases():
            label, edge = case[3], case[5]
            nodes = {(label, i): {} for i in range(43)}
            edges = [((label, i), (label, i + 1), edge) for i in range(42)]
            backend, _ = self.backend(case, nodes, edges)
            policy = dict(backend.query_guard.policy)
            for depth, count in ((20, 20), (40, 40), (None, 42)):
                result = backend.execute("Find reachable nodes", max_depth=depth, max_entities=100)
                self.assertEqual(result["record_count"], count)
                self.assertEqual(result["records"][-1]["path_length"], count)
                self.assertEqual(len(result["evidence_graph"]["nodes"]), count + 1)
                self.assertEqual(len(result["evidence_graph"]["edges"]), count)
                assert_evidence_paths(self, result, set(edges))
                self.assertEqual(backend.query_guard.policy, policy)

    def test_alternative_paths_through_different_frontier_labels_are_all_retained(self):
        for case in self.cases():
            label, other, edge = case[3:]
            second = "RetICFGNode" if case[0] is ICFGQueryBackend else "ArgValVar"
            intermediates = [(other, 10), (second, 10)]
            targets = [(label, i) for i in range(1, 8)]
            nodes = {(label, 0): {}, **{key: {} for key in intermediates + targets}}
            edges = [((label, 0), key, edge) for key in intermediates]
            edges += [(source, target, edge) for source in intermediates for target in targets]
            backend, _ = self.backend(case, nodes, edges)
            with patch("codegraphhunter.web.typed_graph_traversal.NEIGHBOR_PAGE_SIZE", 3):
                result = backend.execute("Find reachable nodes", max_depth=2)
            self.assertEqual({r["result_id"] for r in result["records"]}, set(range(1, 8)))
            self.assertEqual(result["record_count"], 14)
            self.assertEqual({r["path_length"] for r in result["records"]}, {2})

    def test_return_filter_does_not_stop_expansion_of_non_return_nodes(self):
        case = next(self.cases())
        label, edge = case[3], case[5]
        backend, _ = self.backend(case, {(label, 0): {"is_return": False}, (label, 1): {"is_return": False}, (label, 2): {"is_return": True}},
                                  [((label, 0), (label, 1), edge), ((label, 1), (label, 2), edge)], returns_only=True)
        result = backend.execute("Find reachable return nodes")
        self.assertEqual([(r["result_id"], r["path_length"]) for r in result["records"]], [(2, 2)])
        self.assertTrue(result["records"][0]["result_is_return"])
        assert_evidence_paths(self, result, {((label, 0), (label, 1), edge), ((label, 1), (label, 2), edge)})

    def test_parallel_statement_edges_keep_their_properties_and_paths(self):
        case = list(self.cases())[1]
        label, edge = case[3], case[5]
        edges = [((label, 0), (label, 1), edge, {"edge_id": 71, "kind": 1}),
                 ((label, 0), (label, 1), edge, {"edge_id": 72, "kind": 1}),
                 ((label, 1), (label, 2), edge, {"edge_id": 73, "kind": 1})]
        backend, _ = self.backend(case, {(label, i): {} for i in range(3)}, edges)
        result = backend.execute("Find reachable nodes")
        self.assertEqual(result["record_count"], 4)
        self.assertEqual([r["path_length"] for r in result["records"] if r["result_id"] == 2], [2, 2])
        self.assertEqual({e["properties"]["edge_id"] for e in result["evidence_graph"]["edges"]}, {71, 72, 73})
        assert_evidence_paths(self, result, {tuple(item[:3]) for item in edges})

    def test_deadline_returns_completed_paths_as_partial_results(self):
        for case in self.cases():
            label, edge = case[3], case[5]
            backend, bolt = self.backend(case, {(label, 0): {}, (label, 1): {}}, [((label, 0), (label, 1), edge)])
            original = bolt.run
            def read(query, params=None, timeout_s=30):
                if len(bolt.calls) >= 2:
                    raise DatabaseQueryTimeout("Deadline reached.")
                return original(query, params, timeout_s)
            bolt.run = read
            result = backend.execute("Find reachable nodes")
            self.assertEqual(result["record_count"], 1)
            self.assertFalse(result["evidence_graph"]["complete"])
            self.assertEqual(result["execution_strategy"]["stop_reason"], "timeout")

    def test_seed_pagination_and_distinct_endpoint_pairs_obey_result_limit(self):
        for case in self.cases():
            label, edge = case[3], case[5]
            nodes = {(label, i): {"source_loc": "seed" if i < 3 else "result"} for i in range(4)}
            backend, bolt = self.backend(case, nodes, [((label, 2), (label, 3), edge)])
            backend._llm_client.responses.output.update(node_id=-1, source_location="seed")
            result = backend.execute("Find reachable nodes", max_entities=1)
            self.assertEqual([(r["seed_id"], r["result_id"]) for r in result["records"]], [(2, 3)])
            self.assertTrue(any("SKIP 2" in call["cypher"] for call in bolt.calls))

    def test_one_deadline_covers_whole_traversal_and_timeout_is_504(self):
        for case in self.cases():
            label = case[3]
            backend, bolt = self.backend(case, {(label, 0): {}}, [])
            with patch("codegraphhunter.web.typed_graph_traversal.time.monotonic", side_effect=[0, 0, 0, 31]):
                with self.assertRaises(case[1]) as caught:
                    backend.execute("Find reachable nodes")
            self.assertEqual(caught.exception.code, "DATABASE_QUERY_TIMEOUT")
            self.assertEqual(caught.exception.http_status, 504)
            self.assertLessEqual(len(bolt.calls), 1)

    def test_guard_rejection_in_a_batch_never_executes_that_read(self):
        for case in self.cases():
            label = case[3]
            backend, bolt = self.backend(case, {(label, 0): {}}, [])
            checked = Mock()
            checked.validate_for_execution.side_effect = [dict(status="ok"), dict(status="error", reason="READ_ONLY")]
            with patch("codegraphhunter.web.typed_graph_traversal.traversal_guard", return_value=checked):
                with self.assertRaises(case[1]) as caught:
                    backend.execute("Find reachable nodes")
            self.assertEqual(caught.exception.code, "GUARD_REJECTED")
            self.assertEqual(len(bolt.calls), 1)

    def test_pag_named_argument_lookup_precedes_reachability_and_shares_deadline(self):
        plan = pag_fixtures.PAGQueryBackendTests.plan(intent="find_reachable_successors", node_id=-1, function_name="is_valid_path")
        bolt = TypedGraphBolt({("ArgValVar", 99131): {}, ("ObjVar", 1): {}, ("ValVar", 99134): {}},
                              [(('ArgValVar', 99131), ('ObjVar', 1), 'StoreStmt'),
                               (('ObjVar', 1), ('ValVar', 99134), 'StoreStmt')],
                              [{"function_id": 29066, "argument_ids": "99131"}])
        backend = PAGQueryBackend(environ={}, llm_client=FakeLLMClient(plan), bolt_client=bolt)
        result = backend.execute("Find values reached from a named function argument", max_depth=40)
        self.assertEqual([(r["seed_id"], r["result_id"], r["path_length"]) for r in result["records"]], [(99131, 99134, 2)])
        self.assertEqual(result["params"]["argument_ids"], [99131])
        self.assertEqual(result["lookup_queries"][0]["guard"]["status"], "ok")
        self.assertTrue(all(call["timeout_s"] <= bolt.calls[0]["timeout_s"] for call in bolt.calls[1:]))


if __name__ == "__main__":
    unittest.main()
