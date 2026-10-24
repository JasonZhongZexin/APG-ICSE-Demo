import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codegraphhunter.web.pag_query_backend import (
    ALLOWED_INTENTS, PAG_EDGES, PAG_NODES, MAX_DEPTH, MAX_LIMIT,
    PAGQueryBackend, PAGQueryError,
)
from tests.test_icfg_query_backend import FakeBoltClient, FakeLLMClient


class PAGQueryBackendTests(unittest.TestCase):
    @staticmethod
    def plan(**overrides):
        return dict({
            "intent": "find_successors", "seed_label": "ArgValVar", "result_label": "ValVar",
            "edge_label": "StoreStmt", "node_id": 99131, "cg_node_id": -1, "arg_no": -1,
            "icfg_node_id": -1, "source_location": "", "value_name": "", "function_name": "", "depth": 1,
            "limit": 25, "rationale": "Find values receiving the argument.",
        }, **overrides)

    def backend(self, plan=None, records=None, environ=None):
        llm = FakeLLMClient(self.plan() if plan is None else plan)
        bolt = FakeBoltClient(records)
        backend = PAGQueryBackend(environ={} if environ is None else environ, llm_client=llm, bolt_client=bolt)
        return backend, llm, bolt

    def test_all_intents_are_guarded_before_they_reach_pag_database(self):
        for intent in ALLOWED_INTENTS:
            with self.subTest(intent=intent):
                backend, llm, bolt = self.backend(self.plan(intent=intent, depth=MAX_DEPTH))
                result = backend.execute("Find PAG evidence.", max_depth=MAX_DEPTH, max_entities=25)
                self.assertEqual(result["graph"], "PAG")
                self.assertEqual(result["database"], "PAG")
                self.assertEqual(result["guard"]["status"], "ok")
                self.assertEqual(result["model"], "gpt-4o")
                self.assertEqual(bolt.calls[0]["cypher"], result["execution_queries"][0]["cypher"] if result.get("execution_strategy") else result["cypher"])
                self.assertEqual(bolt.calls[0]["params"], {"node_id": 99131})
                self.assertIn("LIMIT 25", result["cypher"])
                self.assertNotIn("CallGraphNode", result["cypher"])
                if "reachable" in intent:
                    self.assertIn("[:StoreStmt*1..4]", result["cypher"])
                elif intent != "find_nodes":
                    self.assertIn("flow.edge_id AS flow_id", result["cypher"])
                if "predecessors" in intent:
                    self.assertIn("(result:ValVar)-[", result["cypher"])
                    self.assertIn("->(seed:ArgValVar)", result["cypher"])

    def test_all_concrete_labels_compile_using_only_their_own_properties(self):
        backend, _, _ = self.backend()
        for label in PAG_NODES:
            plan = backend._validate_plan(self.plan(intent="find_nodes", seed_label=label))
            cypher, _ = backend._compile_plan(plan)
            guard = backend.query_guard.validate_for_execution(cypher, source="system")
            self.assertEqual(guard["status"], "ok", (label, guard))
        for edge in PAG_EDGES:
            for intent in ("find_successors", "find_reachable_successors"):
                plan = backend._validate_plan(self.plan(edge_label=edge, intent=intent, depth=2))
                cypher, _ = backend._compile_plan(plan)
                guard = backend.query_guard.validate_for_execution(cypher, source="system")
                self.assertEqual(guard["status"], "ok", (edge, guard))

    def test_argument_and_return_filters_use_the_stored_function_id_property(self):
        for label, prop in (("ArgValVar", "cg_node_id"), ("RetValPN", "call_graph_node_id"), ("VarArgValPN", "call_graph_node_id")):
            backend, _, bolt = self.backend(self.plan(intent="find_nodes", node_id=-1, seed_label=label, cg_node_id=29066))
            result = backend.execute("Find function arguments or returns.")
            self.assertIn(f"seed.{prop} = $cg_node_id", result["cypher"])
            self.assertEqual(bolt.calls[0]["params"], {"cg_node_id": 29066})
        backend, _, _ = self.backend(self.plan(intent="find_nodes", node_id=-1, cg_node_id=29066, arg_no=0))
        result = backend.execute("Find argument zero of function 29066.")
        self.assertIn("seed.arg_no = $arg_no", result["cypher"])
        self.assertEqual(result["params"]["arg_no"], 0)

    def test_text_filters_are_bound_parameters_and_schema_grounding_is_sent_to_model(self):
        hostile = "is_valid_path' DELETE node //"
        backend, llm, _ = self.backend(self.plan(intent="find_nodes", seed_label="FunObjVar", node_id=-1, value_name=hostile))
        result = backend.execute("Find a function object.")
        self.assertEqual(result["params"]["value_name"], hostile)
        self.assertNotIn(hostile, result["cypher"])
        self.assertIn("seed.val_name CONTAINS $value_name", result["cypher"])
        self.assertIn("seed.all_args_node_ids AS seed_all_args_node_ids", result["cypher"])
        request = llm.responses.calls[0]
        self.assertEqual(request["text"]["format"]["name"], "pag_query_plan")
        self.assertTrue(request["text"]["format"]["strict"])
        context = json.loads(request["input"][1]["content"])["schema_context"]
        self.assertEqual(context["graph"], "PAG")
        self.assertNotIn("val_name", context["node_properties"]["ArgValVar"])

    def test_source_and_icfg_filters_are_parameterized(self):
        source = '"ln": 127'
        backend, _, _ = self.backend(self.plan(intent="find_nodes", seed_label="ValVar", node_id=-1, source_location=source, icfg_node_id=83372))
        result = backend.execute("Find values near line 127.")
        self.assertEqual(result["params"], {"icfg_node_id": 83372, "source_location": source})
        self.assertIn("seed.source_loc CONTAINS $source_location", result["cypher"])

    def test_named_function_arguments_are_resolved_from_database_metadata(self):
        backend, _, bolt = self.backend(self.plan(node_id=-1, function_name="is_valid_path", arg_no=0))
        bolt.run = Mock(side_effect=[
            {"records": [{"function_id": 29066, "argument_ids": "99131"}]},
            {"records": [{"seed_id": 99131, "result_id": 99134}]},
        ])
        result = backend.execute("Trace argument zero of is_valid_path through StoreStmt to ValVar.")
        self.assertEqual(bolt.run.call_count, 2)
        self.assertEqual(bolt.run.call_args_list[0].kwargs["params"], {"function_name": "is_valid_path"})
        self.assertEqual(bolt.run.call_args_list[1].kwargs["params"], {"arg_no": 0, "argument_ids": [99131]})
        self.assertIn("seed.id IN $argument_ids", result["cypher"])
        self.assertEqual(result["records"][0]["seed_id"], 99131)
        self.assertEqual(result["lookup_queries"][0]["guard"]["status"], "ok")

    def test_function_metadata_formats_and_bounds_are_checked(self):
        for metadata in ("99131", "99131, 99132", "99131 99132", "[99131,99132]", [99131, 99132]):
            expected = [99131] if metadata == "99131" else [99131, 99132]
            self.assertEqual(PAGQueryBackend._argument_ids([{"argument_ids": metadata}]), expected)
        for metadata in ("DELETE n", "[true]", {"node": 1}, list(range(65))):
            with self.subTest(metadata=metadata), self.assertRaises(PAGQueryError):
                PAGQueryBackend._argument_ids([{"argument_ids": metadata}])
        self.assertEqual(PAGQueryBackend._argument_ids([]), [])

    def test_function_lookup_and_evidence_query_share_one_database_deadline(self):
        backend, _, bolt = self.backend(self.plan(node_id=-1, function_name="is_valid_path"))
        bolt.run = Mock(return_value={"records": [{"function_id": 29066, "argument_ids": "99131"}]})
        with patch("codegraphhunter.web.pag_query_backend.time.monotonic", side_effect=[0, 0, 31]):
            with self.assertRaises(PAGQueryError) as caught:
                backend.execute("Trace a named function's argument.")
        self.assertEqual(caught.exception.code, "DATABASE_QUERY_TIMEOUT")
        self.assertEqual(bolt.run.call_count, 1)

    def test_function_lookup_guard_rejection_happens_before_any_database_call(self):
        backend, _, bolt = self.backend(self.plan(node_id=-1, function_name="is_valid_path"))
        with patch.object(type(backend.query_guard), "validate_for_execution", side_effect=[{"status": "ok"}, {"status": "error"}]), self.assertRaises(PAGQueryError) as caught:
            backend.execute("Trace a named function's argument.")
        self.assertEqual(caught.exception.code, "GUARD_REJECTED")
        self.assertEqual(bolt.calls, [])

    def test_invalid_plans_never_touch_database(self):
        invalid = [
            [], "MATCH (n) DELETE n", self.plan(cypher="DELETE n"),
            self.plan(seed_label="CallGraphNode"), self.plan(edge_label="CallGraphEdge"),
            self.plan(intent="write"), self.plan(result_label="ValVar:FunObjVar"),
            self.plan(node_id=True), self.plan(node_id=2147483648), self.plan(cg_node_id="29066"),
            self.plan(depth=MAX_DEPTH + 1), self.plan(limit=MAX_LIMIT + 1), self.plan(limit=True),
            self.plan(node_id=-1), self.plan(arg_no=0, seed_label="ValVar"),
            self.plan(value_name="name", seed_label="ArgValVar"),
            self.plan(cg_node_id=1, seed_label="ValVar"),
            self.plan(icfg_node_id=1, seed_label="ObjVar"),
            self.plan(source_location="line\n127"), self.plan(value_name="x" * 301),
        ]
        for plan in invalid:
            with self.subTest(plan=plan):
                backend, llm, bolt = self.backend(plan)
                llm.responses.follow_limits = False
                with self.assertRaises(PAGQueryError) as caught:
                    backend.execute("Find evidence.")
                self.assertEqual(caught.exception.code, "INVALID_MODEL_OUTPUT")
                self.assertEqual(bolt.calls, [])

    def test_guard_rejection_never_executes_query(self):
        backend, _, bolt = self.backend()
        with patch.object(type(backend.query_guard), "validate_for_execution", return_value={"status": "error", "reason": "test-rejection"}), self.assertRaises(PAGQueryError) as caught:
            backend.execute("Find evidence.")
        self.assertEqual(caught.exception.code, "GUARD_REJECTED")
        self.assertEqual(bolt.calls, [])

    def test_cross_graph_compiler_output_is_rejected(self):
        backend, _, bolt = self.backend()
        backend._compile_plan = Mock(return_value=("MATCH (n:CallGraphNode) RETURN n.id LIMIT 1", {}))
        with self.assertRaises(PAGQueryError) as caught:
            backend.execute("Find evidence.")
        self.assertEqual(caught.exception.code, "QUERY_SCOPE_REJECTED")
        self.assertEqual(bolt.calls, [])

    def test_model_selection_and_reasoning_parameters_reach_sdk(self):
        for model in ("gpt-4o", "gpt-6-luna", "gpt-5.4-pro"):
            backend, llm, _ = self.backend(environ={"CODEGRAPHHUNTER_LLM_MODEL": model})
            result = backend.execute("Find evidence.")
            self.assertEqual(result["model"], model)
            request = llm.responses.calls[0]
            self.assertEqual(request["model"], model)
            if model != "gpt-4o":
                self.assertNotIn("temperature", request)
                self.assertGreaterEqual(request["max_output_tokens"], 8192)

    def test_results_are_bounded_and_empty_results_stay_empty(self):
        records = [{"seed_id": number} for number in range(100)]
        backend, _, _ = self.backend(self.plan(limit=2), records)
        result = backend.execute("Find evidence.", max_entities=2)
        self.assertEqual(result["records"], records[:2])
        self.assertEqual(result["record_count"], 2)
        empty, _, _ = self.backend(records=[])
        self.assertEqual(empty.execute("Find evidence.")["records"], [])

    def test_request_and_database_failures_are_reported(self):
        backend, llm, bolt = self.backend()
        for question in (None, "", " " * 4, "x" * 2001):
            with self.assertRaises(PAGQueryError) as caught:
                backend.execute(question)
            self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertEqual(llm.responses.calls, [])
        bolt.run = Mock(side_effect=RuntimeError("test failure"))
        with self.assertRaises(PAGQueryError) as caught:
            backend.execute("Find evidence.")
        self.assertEqual(caught.exception.code, "DATABASE_QUERY_FAILED")

    def test_configuration_errors_and_per_request_client_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaises(PAGQueryError) as caught:
                PAGQueryBackend(environ={}, schema_path=missing)
            self.assertEqual(caught.exception.code, "CONFIGURATION_ERROR")
        backend, llm, bolt = self.backend()
        llm.close = Mock()
        bolt.close = Mock()
        backend.close()
        backend.close()
        llm.close.assert_called_once_with()
        bolt.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
