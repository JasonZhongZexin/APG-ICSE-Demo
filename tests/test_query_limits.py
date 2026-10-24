import json
import unittest
from unittest.mock import Mock

from codegraphhunter.web.callgraph_query_backend import CallGraphQueryBackend, CallGraphQueryError
from codegraphhunter.web.icfg_query_backend import ICFGQueryBackend, ICFGQueryError
from codegraphhunter.web.pag_query_backend import PAGQueryBackend, PAGQueryError
from codegraphhunter.web.query_limits import MAX_QUERY_ENTITIES, query_limit_arguments
from tests.test_icfg_query_backend import FakeBoltClient, FakeLLMClient
from tests.test_callgraph_traversal import GraphBolt
from tests.test_typed_graph_traversal import TypedGraphBolt
from tests import test_icfg_query_backend as icfg_fixtures
from tests import test_pag_query_backend as pag_fixtures


class QueryLimitTests(unittest.TestCase):
    def backends(self, records=None):
        cases = (
            (ICFGQueryBackend, ICFGQueryError, icfg_fixtures.ICFGQueryBackendTests._plan(intent="find_reachable_successors", depth=4)),
            (PAGQueryBackend, PAGQueryError, pag_fixtures.PAGQueryBackendTests.plan(intent="find_reachable_successors", depth=4)),
            (CallGraphQueryBackend, CallGraphQueryError, {
                "intent": "find_reachable_callees", "function_name": "is_valid_path", "depth": 3,
                "limit": 25, "rationale": "Find reachable functions.",
            }),
        )
        for backend_type, error_type, plan in cases:
            llm, bolt = FakeLLMClient(plan), FakeBoltClient(records)
            if backend_type is CallGraphQueryBackend and records:
                ids = [row["id"] for row in records]
                bolt = GraphBolt({100000: "is_valid_path", **{node: str(node) for node in ids}}, [(100000, node) for node in ids])
            elif records:
                seed = (plan["seed_label"], plan["node_id"])
                targets = [(plan["result_label"], row["id"]) for row in records]
                bolt = TypedGraphBolt({seed: {}, **{target: {} for target in targets}},
                                      [(seed, target, plan["edge_label"]) for target in targets])
            yield backend_type(environ={}, llm_client=llm, bolt_client=bolt), error_type, llm, bolt

    def test_form_limits_reach_executed_cypher_and_record_count(self):
        records = [{"id": i} for i in range(150)]
        for backend, _, _, bolt in self.backends(records):
            with self.subTest(backend=type(backend).__name__):
                result = backend.execute("Find reachable nodes.", max_depth=9, max_entities=120)
                self.assertIn("*1..9]", result["cypher"])
                self.assertTrue(result["cypher"].endswith("LIMIT 120"))
                self.assertEqual(result["plan"]["depth"], 9)
                self.assertEqual(result["plan"]["limit"], 120)
                self.assertEqual(result["record_count"], 120)
                self.assertEqual(result["guard"]["status"], "ok")
                self.assertEqual(bolt.calls[0]["cypher"], result["execution_queries"][0]["cypher"] if result.get("execution_strategy") else result["cypher"])
                self.assertEqual(result["constraints"]["max_depth"], 9)
                self.assertEqual(result["constraints"]["max_limit"], 120)

    def test_prompt_schema_model_output_and_database_use_identical_limits(self):
        for backend, _, llm, bolt in self.backends():
            for depth, limit in ((20, 17), (100, 1000), (None, 1000), (None, 3)):
                with self.subTest(backend=type(backend).__name__, depth=depth, limit=limit):
                    result = backend.execute("Find nodes that can reach is_valid_path.", max_depth=depth, max_entities=limit)
                    request = llm.responses.calls[-1]
                    prompt = json.loads(request["input"][1]["content"])
                    policy = prompt["schema_context"]["policy"]
                    self.assertEqual(policy["max_depth"], depth)
                    self.assertEqual(policy["max_limit"], limit)
                    schema = request["text"]["format"]["schema"]
                    self.assertEqual(schema["properties"]["depth"]["enum"], [depth])
                    self.assertEqual(schema["properties"]["limit"]["enum"], [limit])
                    self.assertNotIn("maximum", schema["properties"]["depth"])
                    logged_output = json.loads(llm.responses.outputs[-1])
                    self.assertEqual(logged_output["depth"], result["plan"]["depth"])
                    self.assertEqual(logged_output["limit"], result["plan"]["limit"])
                    if result.get("execution_strategy"):
                        self.assertEqual(result["execution_strategy"]["max_depth"], depth)
                        self.assertTrue(all("*" not in call["cypher"] for call in bolt.calls))
                    else:
                        self.assertIn(f"*1..{'' if depth is None else depth}]", bolt.calls[-1]["cypher"])
                    self.assertTrue(bolt.calls[-1]["cypher"].endswith(f"LIMIT {limit}"))
                    self.assertIn("null max_depth means no hop limit", request["input"][0]["content"])
                    self.assertFalse(schema["additionalProperties"])

    def test_model_limit_mismatches_are_rejected_before_database_execution(self):
        for backend, error_type, llm, bolt in self.backends():
            llm.responses.follow_limits = False
            for depth, limit in ((3, 17), (20, 50), (3, 50), (None, 17), (True, 17), (20, True), (20, 17.0)):
                with self.subTest(backend=type(backend).__name__, depth=depth, limit=limit):
                    llm.responses.output.update(depth=depth, limit=limit)
                    with self.assertRaises(error_type) as caught:
                        backend.execute("Find reachable nodes.", max_depth=20, max_entities=17)
                    self.assertEqual(caught.exception.code, "INVALID_MODEL_OUTPUT")
                    self.assertEqual(bolt.calls, [])
            llm.responses.output.update(depth=3, limit=1000)
            with self.assertRaises(error_type):
                backend.execute("Find reachable nodes.")
            self.assertEqual(bolt.calls, [])

    def test_smaller_overrides_and_subsequent_requests_are_isolated(self):
        for backend, _, _, _ in self.backends([{"id": i} for i in range(30)]):
            with self.subTest(backend=type(backend).__name__):
                policy = dict(backend.query_guard.policy)
                default = backend.execute("Find reachable nodes.")
                small = backend.execute("Find reachable nodes.", max_depth=1, max_entities=2)
                self.assertIn("*1..1]", small["cypher"])
                self.assertTrue(small["cypher"].endswith("LIMIT 2"))
                self.assertEqual(small["record_count"], 2)
                restored = backend.execute("Find reachable nodes.")
                self.assertEqual(restored["cypher"], default["cypher"])
                self.assertEqual(backend.query_guard.policy, policy)

    def test_missing_or_null_limits_execute_unbounded_traversals_and_cap_results_at_1000(self):
        records = [{"id": i} for i in range(1005)]
        for backend, _, _, bolt in self.backends(records):
            for options in ({}, {"max_depth": None, "max_entities": None}):
                with self.subTest(backend=type(backend).__name__, options=options):
                    result = backend.execute("Find reachable nodes.", **options)
                    self.assertIn("*1..]", result["cypher"])
                    self.assertNotIn("None", result["cypher"])
                    self.assertTrue(result["cypher"].endswith("LIMIT 1000"))
                    self.assertIsNone(result["plan"]["depth"])
                    self.assertEqual(result["plan"]["limit"], 1000)
                    self.assertIsNone(result["constraints"]["max_depth"])
                    self.assertEqual(result["constraints"]["max_limit"], 1000)
                    self.assertEqual(result["record_count"], 1000)
                    depth_policy = result["guard"]["metadata"]["depth_policy"]
                    self.assertIsNone(depth_policy["effective_depth"])
                    self.assertIsNone(depth_policy["max_depth"])
                    self.assertFalse(depth_policy["clamped_to_max"])
                    self.assertEqual(bolt.calls[-1]["cypher"], result["execution_queries"][-1]["cypher"] if result.get("execution_strategy") else result["cypher"])

    def test_empty_depth_does_not_change_direct_relationship_queries(self):
        for backend, _, llm, _ in self.backends():
            llm.responses.output["intent"] = "find_callees" if isinstance(backend, CallGraphQueryBackend) else "find_successors"
            result = backend.execute("Find direct successors.")
            self.assertNotIn("*", result["cypher"])
            self.assertTrue(result["cypher"].endswith("LIMIT 1000"))

    def test_unbounded_guard_keeps_read_only_schema_and_result_limits(self):
        from codegraphhunter.web.query_limits import QueryLimits
        for backend, _, _, _ in self.backends():
            guard = QueryLimits().guard(backend.query_guard)
            for query in ("MATCH (n:CallGraphNode) DELETE n LIMIT 1", "MATCH (n:UnknownNode) RETURN n.id LIMIT 1",
                          "MATCH (n:CallGraphNode) RETURN n.id LIMIT 1001", "MATCH (n:CallGraphNode) RETURN n.id"):
                self.assertEqual(guard.validate_for_execution(query, source="system")["status"], "error")

    def test_individual_overrides_use_the_other_fields_default(self):
        for backend, _, _, _ in self.backends():
            with self.subTest(backend=type(backend).__name__):
                depth_only = backend.execute("Find reachable nodes.", max_depth=7)
                self.assertEqual(depth_only["plan"]["limit"], 1000)
                count_only = backend.execute("Find reachable nodes.", max_entities=3)
                self.assertIsNone(count_only["plan"]["depth"])
                self.assertIn("*1..]", count_only["cypher"])

    def test_depths_above_20_are_used_without_silent_clamping(self):
        for backend, _, _, _ in self.backends():
            for depth in (21, 50, 100, 1000):
                with self.subTest(backend=type(backend).__name__, depth=depth):
                    result = backend.execute("Find reachable nodes.", max_depth=depth, max_entities=MAX_QUERY_ENTITIES)
                    self.assertIn(f"*1..{depth}]", result["cypher"])
                    self.assertTrue(result["cypher"].endswith(f"LIMIT {MAX_QUERY_ENTITIES}"))
                    self.assertEqual(result["plan"]["depth"], depth)
                    self.assertEqual(result["guard"]["metadata"]["depth_policy"]["effective_depth"], depth)
                    self.assertFalse(result["guard"]["metadata"]["depth_policy"]["clamped_to_max"])

    def test_invalid_limits_never_reach_the_planner_or_database(self):
        for backend, error_type, llm, bolt in self.backends():
            for field in ("max_depth", "max_entities"):
                invalid = (0, -1, 1.5, 2.0, True, "2", [], {})
                if field == "max_entities":
                    invalid += (MAX_QUERY_ENTITIES + 1,)
                for value in invalid:
                    with self.subTest(backend=type(backend).__name__, field=field, value=value):
                        with self.assertRaises(error_type) as caught:
                            backend.execute("Find reachable nodes.", **{field: value})
                        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
                        self.assertEqual(caught.exception.http_status, 400)
            self.assertEqual(llm.responses.calls, [])
            self.assertEqual(bolt.calls, [])

    def test_http_limit_arguments_accept_null_and_preserve_absent_options(self):
        self.assertEqual(query_limit_arguments({"query": "Find nodes."}), {})
        self.assertEqual(query_limit_arguments({"max_depth": 6, "max_entities": 8}), {"max_depth": 6, "max_entities": 8})
        for field in ("max_depth", "max_entities"):
            self.assertEqual(query_limit_arguments({field: None}), {field: None})

    def test_direct_queries_stay_one_hop_when_maximum_depth_changes(self):
        for backend, _, llm, _ in self.backends():
            intent = "find_callees" if isinstance(backend, CallGraphQueryBackend) else "find_successors"
            llm.responses.output["intent"] = intent
            result = backend.execute("Find direct successors.", max_depth=8, max_entities=5)
            self.assertNotIn("*1..", result["cypher"])
            self.assertEqual(result["plan"]["intent"], intent)
            self.assertTrue(result["cypher"].endswith("LIMIT 5"))

    def test_pag_named_function_lookup_keeps_its_own_bounds(self):
        plan = pag_fixtures.PAGQueryBackendTests.plan(node_id=-1, function_name="is_valid_path", arg_no=0)
        bolt = FakeBoltClient()
        bolt.run = Mock(side_effect=[
            {"records": [{"function_id": 29066, "argument_ids": "99131"}]},
            {"records": [{"seed_id": 99131, "result_id": 99134}, {"seed_id": 99131, "result_id": 99135}]},
        ])
        backend = PAGQueryBackend(environ={}, llm_client=FakeLLMClient(plan), bolt_client=bolt)
        result = backend.execute("Find argument zero successors.", max_depth=2, max_entities=1)
        self.assertEqual(bolt.run.call_count, 2)
        self.assertTrue(bolt.run.call_args_list[0].args[0].endswith("LIMIT 11"))
        self.assertTrue(bolt.run.call_args_list[1].args[0].endswith("LIMIT 1"))
        self.assertEqual(result["record_count"], 1)


if __name__ == "__main__":
    unittest.main()
