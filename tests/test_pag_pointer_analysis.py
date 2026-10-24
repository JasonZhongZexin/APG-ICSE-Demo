import json
import re
import tempfile
import unittest
from unittest.mock import patch

from codegraphhunter.web.capability_query_backend import CapabilityQueryBackend, CapabilityQueryError, empty_plan
from codegraphhunter.web.database_client import DatabaseQueryTimeout
from codegraphhunter.web.pag_pointer_analysis import AndersenSolver, UNKNOWN
from tests import test_query_capabilities as fixtures
from tests import test_query_local_summary as summary_fixtures


class PointerSolverTests(unittest.TestCase):
    def setUp(self):
        self.nodes, self.edges = {}, []

    def node(self, ident, label="ValVar", **properties):
        key = (label, ident)
        self.nodes[key] = dict(id=ident, **properties)
        return key

    def edge(self, label, source, target, **properties):
        self.edges.append(dict(label=label, source=source, target=target, properties=properties))

    def address(self, pointer, ident):
        obj = self.node(ident, "StackObjVar")
        self.edge("AddrStmt", obj, pointer)
        return obj

    def solve(self, **options):
        return AndersenSolver(self.nodes, self.edges, **options).solve()

    def test_address_and_copy_cycle_reach_a_fixed_point(self):
        a, b, c = [self.node(i) for i in (1, 2, 3)]
        obj = self.address(a, 100)
        self.edge("CopyStmt", a, b)
        self.edge("CopyStmt", b, c)
        self.edge("CopyStmt", c, a)
        solver = self.solve()
        self.assertTrue(solver.converged)
        self.assertEqual([solver.objects(key) for key in (a, b, c)], [{obj}] * 3)
        self.assertEqual(solver.fact_count, 3)

    def test_load_store_solve_memory_contents_instead_of_collecting_reachable_objects(self):
        pointer, value, loaded, result = [self.node(i) for i in (1, 2, 3, 4)]
        slot, data = self.address(pointer, 100), self.address(value, 200)
        self.edge("LoadStmt", pointer, loaded)
        self.edge("CopyStmt", loaded, result)
        self.edge("StoreStmt", value, pointer)
        solver = self.solve()
        self.assertEqual(solver.objects(slot), {data})
        self.assertEqual(solver.objects(loaded), {data})
        self.assertEqual(solver.objects(result), {data})
        self.assertNotIn(slot, solver.objects(result))

    def test_newly_discovered_indirect_stores_trigger_further_load_propagation(self):
        p, q, r, target, output = [self.node(i) for i in range(1, 6)]
        slot_a, slot_b, data = self.address(p, 100), self.address(q, 101), self.address(r, 102)
        self.edge("StoreStmt", q, p)
        self.edge("LoadStmt", p, target)
        self.edge("StoreStmt", r, target)
        self.edge("LoadStmt", q, output)
        solver = self.solve()
        self.assertEqual(solver.objects(slot_a), {slot_b})
        self.assertEqual(solver.objects(slot_b), {data})
        self.assertEqual(solver.objects(output), {data})

    def test_actual_formal_and_return_bindings_propagate_points_to_objects(self):
        actual = self.node(1)
        arg = self.node(2, "ArgValVar", arg_no=0, cg_node_id=1000)
        returned, caller = self.node(3, "RetValPN"), self.node(4)
        obj = self.address(actual, 100)
        self.edge("CallPE", actual, arg)
        self.edge("CopyStmt", arg, returned)
        self.edge("RetPE", returned, caller)
        solver = self.solve()
        self.assertEqual(solver.pts[arg], {obj})
        self.assertEqual(solver.pts[caller], {obj})

    def test_phi_and_select_use_all_exported_operands(self):
        a, b, result = [self.node(i) for i in (1, 2, 3)]
        first, second = self.address(a, 100), self.address(b, 101)
        self.edge("PhiStmt", a, result, op_var_node_ids="{1,2}")
        solver = self.solve()
        self.assertEqual(solver.pts[result], {first, second})

    def test_missing_phi_operands_and_undecoded_conversions_remain_unknown(self):
        a, phi, cast = [self.node(i) for i in (1, 2, 3)]
        obj = self.address(a, 100)
        self.edge("PhiStmt", a, phi)
        self.edge("CopyStmt", a, cast, copy_kind=999)
        solver = self.solve()
        self.assertEqual(solver.objects(phi), {obj})
        self.assertIn(UNKNOWN, solver.pts[phi])
        self.assertIn(UNKNOWN, solver.pts[cast])

    def test_gep_objects_collapse_to_the_same_base_for_field_insensitive_aliasing(self):
        a, b = self.node(1), self.node(2)
        base = self.node(100, "StackObjVar")
        field1 = self.node(101, "GepObjVar", base_obj_var_node_id=100, app_offset=1)
        field2 = self.node(102, "GepObjVar", base_obj_var_node_id=100, app_offset=2)
        self.edge("AddrStmt", field1, a)
        self.edge("AddrStmt", field2, b)
        solver = self.solve()
        self.assertEqual(solver.alias(a, b, constraints_complete=True), ("MayAlias", {base}))

    def test_may_alias_and_scoped_no_alias_are_separate_from_unknown(self):
        a, b, c = [self.node(i) for i in (1, 2, 3)]
        obj = self.address(a, 100)
        self.address(b, 101)
        self.edge("CopyStmt", a, c)
        solver = self.solve()
        self.assertEqual(solver.alias(a, c, constraints_complete=True), ("MayAlias", {obj}))
        self.assertEqual(solver.alias(a, b, constraints_complete=True)[0], "NoAlias")
        self.assertEqual(solver.alias(a, b, constraints_complete=False)[0], "Unknown")
        self.assertNotEqual(solver.alias(a, a, constraints_complete=True)[0], "MustAlias")

    def test_unknown_input_and_unknown_store_invalidate_negative_alias_conclusions(self):
        a, b, loaded = [self.node(i) for i in (1, 2, 3)]
        unknown = self.node(4, "ArgValVar")
        self.address(a, 100)
        self.address(b, 101)
        self.edge("StoreStmt", a, unknown)
        self.edge("LoadStmt", a, loaded)
        solver = self.solve()
        self.assertIn(UNKNOWN, solver.pts[unknown], "Store does not define its address operand")
        self.assertTrue(solver.unknown_memory)
        self.assertIn(UNKNOWN, solver.pts[loaded])
        self.assertEqual(solver.alias(loaded, b, constraints_complete=True)[0], "Unknown")

    def test_external_returns_and_memory_effects_are_conservative(self):
        self.node(1000, "FunObjVar", is_decl=True, is_uncalled=False)
        returned = self.node(1, "RetValPN", call_graph_node_id=1000)
        p, loaded = self.node(2), self.node(3)
        self.address(p, 100)
        self.edge("LoadStmt", p, loaded)
        solver = self.solve()
        self.assertIn(UNKNOWN, solver.pts[returned])
        self.assertIn(UNKNOWN, solver.pts[loaded])
        self.assertTrue(solver.unknown_memory)

    def test_unknown_objects_and_missing_base_metadata_are_not_fabricated_graph_objects(self):
        p, q = self.node(1), self.node(2)
        dummy = self.node(100, "DummyObjVar")
        field = self.node(101, "GepObjVar", base_obj_var_node_id=999)
        self.edge("AddrStmt", dummy, p)
        self.edge("AddrStmt", field, q)
        solver = self.solve()
        self.assertEqual(solver.objects(p), set())
        self.assertEqual(solver.objects(q), set())
        self.assertEqual(solver.alias(p, q, constraints_complete=True)[0], "Unknown")

    def test_budget_and_deadline_stop_without_reporting_a_fixed_point(self):
        p, q = self.node(1), self.node(2)
        self.address(p, 100)
        self.address(q, 101)
        solver = self.solve(max_facts=1)
        self.assertFalse(solver.converged)
        self.assertEqual(solver.stop_reason, "fact_budget")
        self.assertEqual(solver.alias(p, q, constraints_complete=True)[0], "Unknown")
        def expired():
            raise DatabaseQueryTimeout()
        solver = self.solve(check=expired)
        self.assertFalse(solver.converged)
        self.assertEqual(solver.stop_reason, "timeout")

    def test_bitset_memory_budget_stops_without_certifying_disjoint_sets(self):
        p, q = self.node(1), self.node(2)
        for ident in range(100, 120):
            self.address(p, ident)
        solver = self.solve(max_bitset_bytes=1)
        self.assertFalse(solver.converged)
        self.assertEqual(solver.stop_reason, "bitset_budget")
        self.assertEqual(solver.alias(p, q, constraints_complete=True)[0], "Unknown")


class PointerQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = fixtures.MemoryGraph()
        self.f = self.db.node("FunObjVar", 1000, val_name="parse")
        self.other = self.db.node("FunObjVar", 1001, val_name="sink")
        self.a = self.db.node("ArgValVar", 1, cg_node_id=1000, arg_no=0)
        self.b = self.db.node("ArgValVar", 2, cg_node_id=1000, arg_no=1)
        self.c = self.db.node("ArgValVar", 3, cg_node_id=1001, arg_no=0)
        self.obj1 = self.db.node("StackObjVar", 100, source_loc="case.cpp:10")
        self.obj2 = self.db.node("HeapObjVar", 101, source_loc="case.cpp:20")
        self.db.edge("AddrStmt", self.obj1, self.a)
        self.db.edge("AddrStmt", self.obj2, self.b)
        self.db.edge("CallPE", self.a, self.c)
        self.env = dict(CODEGRAPHHUNTER_SOURCE_ROOT=self.tmp.name)

    def backend(self, *, llm_client=None, **environ):
        return CapabilityQueryBackend(dict(self.env, **environ), llm_client=llm_client, clients={"PAG": self.db})

    def test_parameter_points_to_returns_object_identity_metadata_and_solver_statistics(self):
        result = self.backend().execute_plan(empty_plan("PAG.POINTS_TO", function_name="parse", arg_no=0))
        self.assertTrue(result["ok"])
        row = result["records"][0]
        self.assertEqual((row["pointer_node_id"], row["object_node_id"], row["object_label"]), (1, 100, "StackObjVar"))
        self.assertEqual(row["object_source_loc"], "case.cpp:10")
        self.assertTrue(row["set_complete"])
        analysis = result["analysis"]["pointer_analysis"]
        self.assertTrue(analysis["fixed_point_reached"])
        self.assertTrue(analysis["constraints_complete"])
        self.assertEqual(analysis["constraint_count"], 3)
        self.assertEqual(analysis["points_to_sets"][0]["function_name"], "parse")
        self.assertEqual(result["evidence_graph"]["edges"], [], "Derived points-to facts must not masquerade as stored PAG edges")
        self.assertEqual({node["id"] for node in result["evidence_graph"]["nodes"]}, {1, 100})
        self.assertTrue(all(re.search(r"LIMIT (1|1000|50000)$", query) for query, _ in self.db.calls))
        self.assertFalse(any(re.search(r"\b(?:CREATE|MERGE|DELETE|SET)\b", query) for query, _ in self.db.calls))

    def test_alias_resolves_both_arguments_of_the_same_function_and_different_functions(self):
        for target, number, expected in [("", 1, "NoAlias"), ("sink", 0, "MayAlias")]:
            with self.subTest(target=target):
                result = self.backend().execute_plan(empty_plan("PAG.PARAMETER_ALIAS", function_name="parse", arg_no=0,
                                                               target_function=target, target_arg_no=number))
                row = result["records"][0]
                self.assertEqual(row["alias_result"], expected)
                self.assertEqual(row["common_object_count"], 1 if expected == "MayAlias" else 0)
                self.assertEqual(result["analysis"]["conclusion"], expected)

    def test_explicit_node_ids_and_function_object_ids_work(self):
        result = self.backend().execute_plan(empty_plan("PAG.PARAMETER_ALIAS", node_id=1, target_node_id=3))
        self.assertEqual(result["records"][0]["alias_result"], "MayAlias")
        value = self.db.node("ValVar", 300)
        self.db.edge("AddrStmt", self.obj1, value)
        result = self.backend().execute_plan(empty_plan("PAG.POINTS_TO", node_id=300))
        self.assertEqual(result["records"][0]["object_node_id"], 100)

    def test_qualified_parameter_names_do_not_match_another_class_method(self):
        self.db.node("FunObjVar", 2000, val_name="_ZN7httplib7Request10set_headerEv")
        self.db.node("FunObjVar", 2001, val_name="_ZN7httplib8Response10set_headerEv")
        request_arg = self.db.node("ArgValVar", 2002, cg_node_id=2000, arg_no=1)
        response_arg = self.db.node("ArgValVar", 2003, cg_node_id=2001, arg_no=1)
        self.db.edge("AddrStmt", self.obj1, request_arg)
        self.db.edge("AddrStmt", self.obj2, response_arg)
        result = self.backend().execute_plan(empty_plan("PAG.PARAMETER_ALIAS", function_name="parse", arg_no=0,
            target_function="Request::set_header", target_arg_no=1))
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["records"][0]["right_node_id"], 2002)
        self.assertEqual(result["records"][0]["alias_result"], "MayAlias")

    def test_missing_constraint_identity_disables_negative_alias_conclusions(self):
        self.db.edges = [(label, source, target, dict(props, edge_id=None)) for label, source, target, props in self.db.edges]
        result = self.backend().execute_plan(empty_plan("PAG.PARAMETER_ALIAS", node_id=1, target_node_id=2))
        self.assertEqual(result["records"][0]["alias_result"], "Unknown")
        self.assertFalse(result["analysis"]["pointer_analysis"]["constraints_complete"])
        result = self.backend().execute_plan(empty_plan("PAG.PARAMETER_ALIAS", function_id=1000, arg_no=0,
                                                       target_function_id=1001, target_arg_no=0))
        self.assertEqual(result["records"][0]["alias_result"], "MayAlias")

    def test_display_cap_and_hop_limit_do_not_truncate_pag_scans_or_fixed_point_solving(self):
        self.db.edges.clear()
        for index in range(1105):
            obj = self.db.node("ObjVar", 2000 + index)
            self.db.node("FunObjVar", 5000 + index, val_name=f"unused_function_{index}")
            self.db.edge("AddrStmt", obj, self.a)
        self.db.edge("AddrStmt", obj, self.b)
        with patch("codegraphhunter.web.pag_pointer_analysis.SCAN_PAGE_SIZE", 1000):
            result = self.backend().execute_plan(empty_plan("PAG.PARAMETER_ALIAS", function_name="parse", arg_no=0,
                                                           target_arg_no=1, depth=1, limit=1))
        self.assertEqual(result["record_count"], 1)
        row = result["records"][0]
        self.assertEqual(row["alias_result"], "MayAlias")
        self.assertEqual(row["left_points_to_count"], 1105)
        self.assertEqual(row["common_objects"][0]["id"], 3104)
        self.assertEqual(result["analysis"]["pointer_analysis"]["constraint_count"], 1106)
        self.assertTrue(result["analysis"]["pointer_analysis"]["complete"])
        self.assertTrue(any("SKIP 1000" in query for query, _ in self.db.calls))
        self.assertTrue(any(params.get("after_id", -1) >= 5000 for _, params in self.db.calls))

    def test_points_to_display_truncation_preserves_full_set_counts(self):
        self.db.edge("AddrStmt", self.obj2, self.a)
        result = self.backend().execute_plan(empty_plan("PAG.POINTS_TO", node_id=1, limit=1))
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["records"][0]["points_to_count"], 2)
        self.assertTrue(result["analysis"]["pointer_analysis"]["complete"])

    def test_gep_base_object_without_its_own_constraint_is_loaded_by_exported_id(self):
        self.db.edges.clear()
        field = self.db.node("GepObjVar", 150, base_obj_var_node_id=100, app_offset=8)
        self.db.edge("AddrStmt", field, self.a)
        result = self.backend().execute_plan(empty_plan("PAG.POINTS_TO", node_id=1))
        self.assertEqual(result["records"][0]["object_node_id"], 100)
        self.assertEqual(result["records"][0]["object_source_loc"], "case.cpp:10")
        self.assertTrue(result["records"][0]["set_complete"])

    def test_whole_graph_scans_defer_heavy_source_text_until_object_display(self):
        result = self.backend().execute_plan(empty_plan("PAG.POINTS_TO", node_id=1))
        scans = [(query, params) for query, params in self.db.calls if "after_id" in params]
        self.assertTrue(scans)
        self.assertTrue(all("source_loc" not in query for query, _ in scans))
        self.assertEqual(result["records"][0]["object_source_loc"], "case.cpp:10")

    def test_incomplete_constraint_or_fact_budgets_never_produce_no_alias(self):
        for setting in ("CODEGRAPHHUNTER_PTA_MAX_NODES", "CODEGRAPHHUNTER_PTA_MAX_EDGES", "CODEGRAPHHUNTER_PTA_MAX_FACTS"):
            with self.subTest(setting=setting):
                result = self.backend(**{setting: "1"}).execute_plan(empty_plan("PAG.PARAMETER_ALIAS", node_id=1, target_node_id=2))
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["records"][0]["alias_result"], "Unknown")
                self.assertFalse(result["analysis"]["pointer_analysis"]["complete"])

    def test_unbound_formal_parameter_returns_unknown_instead_of_an_empty_proven_set(self):
        self.db.edges = [edge for edge in self.db.edges if edge[2] != self.a]
        result = self.backend().execute_plan(empty_plan("PAG.POINTS_TO", node_id=1))
        self.assertEqual(result["records"][0]["result"], "Unknown")
        self.assertTrue(result["records"][0]["contains_unknown"])
        self.assertFalse(result["records"][0]["set_complete"])
        self.assertFalse(result["analysis"]["pointer_analysis"]["points_to_sets"][0]["complete"])

    def test_missing_indices_invalid_parameters_and_injection_are_rejected(self):
        plans = [empty_plan("PAG.POINTS_TO", function_name="parse"),
                 empty_plan("PAG.PARAMETER_ALIAS", function_name="parse", arg_no=0),
                 empty_plan("PAG.POINTS_TO", node_id=999),
                 empty_plan("PAG.POINTS_TO", node_id=100, node_label="StackObjVar"),
                 empty_plan("PAG.POINTS_TO", node_id=1, node_label="ArgValVar) DELETE node //")]
        for plan in plans:
            with self.subTest(plan=plan), self.assertRaises((CapabilityQueryError, ValueError)):
                self.backend().execute_plan(plan)

    def test_model_auto_selection_and_local_summary_receive_pointer_analysis_results(self):
        for capability in ("PAG.POINTS_TO", "PAG.PARAMETER_ALIAS"):
            with self.subTest(capability=capability):
                plan = empty_plan(capability, function_name="parse", arg_no=0, target_function="sink", target_arg_no=0)
                client = summary_fixtures.SummaryClient(plan, summary_fixtures.narrative())
                progress = []
                result = self.backend(llm_client=client).execute("Analyze argument 0 of parse and argument 0 of sink", scope="PAG", on_progress=progress.append)
                self.assertEqual(result["capability"]["id"], capability)
                choices = client.calls[0]["text"]["format"]["schema"]["properties"]["capability"]["enum"]
                self.assertIn("PAG.POINTS_TO", choices)
                self.assertIn("PAG.PARAMETER_ALIAS", choices)
                self.assertIn("target_arg_no", client.calls[0]["text"]["format"]["schema"]["required"])
                self.assertEqual(result["local_summary"]["status"], "completed")
                context = json.loads(client.calls[-1]["input"][-1]["content"])
                self.assertTrue(context["pointer_analysis"]["complete"])
                self.assertIn("andersen_style_field_insensitive", client.calls[-1]["input"][-1]["content"])
                self.assertTrue(any(event.get("capability", {}).get("id") == capability for event in progress))
                self.assertTrue(all(event.get("timeout_s", 1) > 0 for event in progress))

    def test_planner_does_not_treat_an_argument_index_as_a_pag_node_id(self):
        plan = empty_plan("PAG.POINTS_TO", node_id=0)
        client = summary_fixtures.SummaryClient(plan, summary_fixtures.narrative())
        with self.assertRaises(CapabilityQueryError):
            self.backend(llm_client=client).execute("Compute the points-to set of argument 0 of parse", scope="PAG")
        self.assertEqual(self.db.calls, [])
        plan = empty_plan("PAG.PARAMETER_ALIAS", node_id=1, target_node_id=3)
        client = summary_fixtures.SummaryClient(plan, summary_fixtures.narrative())
        result = self.backend(llm_client=client).execute("May PAG nodes 1 and 3 alias?", scope="PAG")
        self.assertEqual(result["records"][0]["alias_result"], "MayAlias")

    def test_bulk_reader_keeps_ordinary_read_limits_and_graph_scope(self):
        backend = self.backend()
        backend.execute_plan(empty_plan("PAG.POINTS_TO", node_id=1))
        graph = backend.graph
        self.assertEqual(graph.guard.policy["max_limit"], 1000)
        self.assertEqual(graph.scan_guard.policy["max_limit"], 50000)
        with self.assertRaises(ValueError):
            graph.read("PAG", "MATCH (node:ArgValVar) RETURN node.id AS node_id LIMIT 50000")
        with self.assertRaises(ValueError):
            graph.read("CallGraph", "MATCH (node:CallGraphNode) RETURN node.id AS node_id LIMIT 50000", bulk=True)

    def test_pointer_analysis_timeout_can_be_configured_without_changing_other_queries(self):
        backend = self.backend(CODEGRAPHHUNTER_CAPABILITY_TIMEOUT_S="8", CODEGRAPHHUNTER_PTA_TIMEOUT_S="90")
        backend.execute_plan(empty_plan("PAG.POINTS_TO", node_id=1))
        self.assertEqual(backend.graph.timeout, 90)
        backend.execute_plan(empty_plan("PAG.ARGUMENT_RESOLVE", node_id=1))
        self.assertEqual(backend.graph.timeout, 8)


if __name__ == "__main__":
    unittest.main()
