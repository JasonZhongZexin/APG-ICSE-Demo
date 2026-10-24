import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codegraphhunter.web import app
from codegraphhunter.web.capability_catalog import CAPABILITIES, EXPECTED_OUTPUTS, catalog
from codegraphhunter.web.capability_graph import CapabilityGraph
from codegraphhunter.web.capability_query_backend import CapabilityQueryBackend, CapabilityQueryError, empty_plan
from codegraphhunter.web.database_client import DatabaseQueryTimeout
from codegraphhunter.web.query_limits import QueryLimits
from codegraphhunter.web.source_queries import SourceQueries
from codegraphhunter.web.query_local_summary import QueryLocalSummary


class MemoryGraph:
    """Read-only fixture database; runs the real compiler, guards and traversal."""
    def __init__(self):
        self.nodes, self.edges, self.calls = {}, [], []

    def node(self, label, ident, **props):
        primary = "st_info_id" if label == "StInfo" else "id"
        self.nodes[(label, ident)] = {primary: ident, **props}
        return label, ident

    def edge(self, label, source, target, **props):
        self.edges.append((label, source, target, dict({"edge_id": len(self.edges)}, **props, uid=len(self.edges))))

    def run(self, cypher, *, params, timeout_s):
        self.calls.append((cypher, params))
        limit = int(re.search(r"LIMIT (\d+)", cypher).group(1))
        offset = re.search(r"SKIP (\d+)", cypher)
        offset = int(offset.group(1)) if offset else 0
        if "MATCH (source)-[flow:" in cypher:
            edge_label = re.search(r"flow:(\w+)", cypher).group(1)
            rows = []
            for edge, a, b, properties in self.edges:
                if edge == edge_label:
                    row = dict(source_id=a[1], source_label=a[0], target_id=b[1], target_label=b[0], edge_uid=properties["uid"])
                    row.update({alias: properties.get(prop) for prop, alias in re.findall(r"flow\.(\w+) AS (edge_\w+)", cypher)})
                    rows.append(row)
            return {"records": rows[offset:offset + limit]}
        if "source:CallGraphNode" in cypher:
            return {"records": [dict(source_id=a[1], source_name=self.nodes[a]["fun_name"],
                                     source_fun_obj_var_id=self.nodes[a]["fun_obj_var_id"], target_id=b[1],
                                     target_name=self.nodes[b]["fun_name"], target_fun_obj_var_id=self.nodes[b]["fun_obj_var_id"],
                                     edge_csid=p.get("csid"), edge_kind=p.get("kind"), edge_uid=p["uid"])
                                for label, a, b, p in self.edges if label == "CallGraphEdge"][offset:offset + limit]}
        if "(seed:" in cypher:
            label = re.search(r"seed:(\w+)", cypher).group(1)
            edge_label = re.search(r"flow:(\w+)", cypher).group(1)
            incoming = "<-[flow:" in cypher
            rows = []
            for edge, a, b, p in self.edges:
                root, target = (b, a) if incoming else (a, b)
                if edge == edge_label and root == (label, params["seed_id"]):
                    row = dict(node_id=target[1], node_label=target[0], edge_uid=p["uid"])
                    row.update({alias: p.get(prop) for prop, alias in re.findall(r"flow\.(\w+) AS (edge_\w+)", cypher)})
                    rows.append(row)
            return {"records": sorted(rows, key=lambda r: (r["node_id"], r["edge_uid"]))[offset:offset + limit]}
        label = re.search(r"node:(\w+)", cypher).group(1)
        rows = []
        for key, values in self.nodes.items():
            if key[0] != label:
                continue
            if "after_id" in params and values.get("id", -1) <= params["after_id"]:
                continue
            filters = re.findall(r"node\.(\w+) (=|CONTAINS|IN) \$(filter_\d+)", cypher)
            filters += [(prop, "=", name) for prop, name in re.findall(r"\{(\w+): \$(filter_\d+)\}", cypher)]
            def match(prop, op, name):
                actual, expected = values.get(prop), params[name]
                return actual == expected if op == "=" else expected in str(actual or "") if op == "CONTAINS" else actual in expected
            if all(match(*condition) for condition in filters):
                rows.append({alias: values.get(prop) for prop, alias in re.findall(r"node\.(\w+) AS (node_\w+)", cypher)})
        rows.sort(key=lambda row: row.get("node_id", row.get("node_st_info_id", -1)))
        return {"records": rows[offset:offset + limit]}


def fixture():
    db = MemoryGraph()
    c1 = db.node("CallGraphNode", 1, fun_name="parse", fun_obj_var_id=101)
    c2 = db.node("CallGraphNode", 2, fun_name="validate", fun_obj_var_id=102)
    c3 = db.node("CallGraphNode", 3, fun_name="sink", fun_obj_var_id=103)
    c4 = db.node("CallGraphNode", 4, fun_name="main", fun_obj_var_id=104)
    db.edge("CallGraphEdge", c4, c1, csid=14)
    db.edge("CallGraphEdge", c1, c2, csid=12)
    db.edge("CallGraphEdge", c1, c3, csid=13)
    db.edge("CallGraphEdge", c2, c3, csid=23)
    db.edge("CallGraphEdge", c2, c2, csid=22)
    db.edge("CallGraphEdge", c3, c1, csid=31)
    for ident, name in [(101, "parse"), (102, "validate"), (103, "sink"), (105, "Base::run"), (106, "Derived::run")]:
        db.node("FunObjVar", ident, val_name=name, svf_type_id=700)
    arg = db.node("ArgValVar", 201, cg_node_id=101, arg_no=0, svf_type_id=700, icfg_node_id=302)
    val = db.node("ValVar", 202, icfg_node_id=303, svf_type_id=700)
    other = db.node("ValVar", 203, svf_type_id=700)
    sink = db.node("ArgValVar", 204, cg_node_id=103, arg_no=0, svf_type_id=700)
    obj = db.node("ObjVar", 205, svf_type_id=700)
    ret = db.node("RetValPN", 206, call_graph_node_id=101, svf_type_id=700)
    gep = db.node("GepValVar", 207, ap_fld_idx=2, svf_type_id=700)
    db.edge("StoreStmt", arg, val, icfg_node_id=303)
    db.edge("LoadStmt", val, other, icfg_node_id=303)
    db.edge("CallPE", other, sink, call_icfg_node_id=303, icfg_node_id=303)
    db.edge("AddrStmt", obj, val)
    db.edge("CopyStmt", val, other)
    db.edge("CopyStmt", ret, other)
    db.edge("GepStmt", val, gep)
    entry = db.node("FunEntryICFGNode", 301, fun_obj_var_id=101)
    branch = db.node("IntraICFGNode", 302, fun_obj_var_id=101, is_return=False, source_loc='{"ln": 2}')
    call = db.node("CallICFGNode", 303, fun_obj_var_id=101, called_fun_obj_var_id=103,
                   is_vir_call_inst=True, virtual_fun_idx=0, chnodes_ids="501,502", fun_name_of_v_call="run")
    ret = db.node("IntraICFGNode", 304, fun_obj_var_id=101, is_return=True)
    db.node("IntraICFGNode", 305, fun_obj_var_id=101, is_return=False)
    sink_entry = db.node("FunEntryICFGNode", 306, fun_obj_var_id=103)
    db.edge("IntraCFGEdge", entry, branch)
    db.edge("IntraCFGEdge", branch, call, branch_cond_val=1, condition_var_id=201)
    db.edge("IntraCFGEdge", branch, ret, branch_cond_val=0, condition_var_id=201)
    db.edge("IntraCFGEdge", call, ret)
    db.edge("IntraCFGEdge", call, branch)
    db.edge("CallCFGEdge", call, sink_entry)
    base = db.node("CHNode", 501, class_name="Base", virtual_function_vectors="{105}")
    derived = db.node("CHNode", 502, class_name="Derived", virtual_function_vectors="{106}")
    db.edge("CHEdge", derived, base, edge_type=1)
    db.node("SVFStructType", 700, struct_name="Request", fields_id_vec="701,702", stinfo_node_id=800)
    db.node("StInfo", 800, fld_idx_vec="0,8", fld_idx_2_type_map="0:701,1:702")
    return db


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "case.cpp").write_text("int parse(int path) {\n  if (!path) return 0;\n  return sink(path);\n}\nint validate(int x) { return x; }\nint sink(int x) { return x; }\n")
        self.db = fixture()
        self.env = {"CODEGRAPHHUNTER_SOURCE_ROOT": str(self.root)}
        self.backend = CapabilityQueryBackend(self.env, clients={name: self.db for name in ("PAG", "ICFG", "CallGraph", "CHG", "TypeGraph")})

    def run_plan(self, capability, **params):
        return self.backend.execute_plan(empty_plan(capability, depth=5, limit=100, **params))

    def test_help_catalog_covers_every_capability_with_english_examples_and_outputs(self):
        self.assertEqual(set(EXPECTED_OUTPUTS), set(CAPABILITIES))
        payload = catalog()
        self.assertEqual(payload["count"], 45)
        for item in payload["capabilities"]:
            with self.subTest(capability=item["id"]):
                self.assertTrue(item["examples"])
                self.assertIn(item["example"], item["examples"])
                self.assertTrue(item["expected_output"].strip())
                self.assertGreater(len(item["expected_output"].split()), 8)
                for text in [item["title"], item["expected_output"], *item["examples"]]:
                    self.assertIsNone(re.search(r"[\u3400-\u9fff]", text))

    def test_all_named_contracts_have_executing_handlers_with_real_evidence(self):
        plans = {
            "SRC.FUNCTION_SEARCH": dict(function_name="parse"),
            "SRC.SOURCE_WINDOW": dict(file="case.cpp", line=2),
            "SRC.CANDIDATE_LOCALIZATION": dict(problem_statement="parse path"),
            "SRC.PATCH_LOCATION": dict(node_id=1),
            "SRC.KEYWORD_USAGE": dict(keyword="sink"),
            "CG.FUNCTION_RESOLVE": dict(function_name="parse"),
            "CG.CALLER_CALLEE_NEIGHBORHOOD": dict(function_name="parse"),
            "CG.CALL_CHAIN": dict(function_name="parse", target_function="sink"),
            "CG.REACHABILITY": dict(node_id=1, target_node_id=3),
            "CG.RECURSION_SELF": {}, "CG.CYCLE_DETECTION": {},
            "CG.FANIN_FANOUT": dict(direction="in"),
            "CG.ENTRY_TO_TARGET_CONTEXT": dict(function_name="sink"),
            "ICFG.SEED_RESOLVE": dict(function_name="parse"),
            "ICFG.LOCAL_CONTROL_FLOW": dict(function_name="parse"),
            "ICFG.BRANCH_CONTEXT": dict(function_name="parse"),
            "ICFG.EARLY_RETURN_GUARD": dict(function_name="parse"),
            "ICFG.CALLSITE_CONTEXT": dict(callsite_id=303),
            "ICFG.INTERPROC_PATH": dict(node_id=301, target_node_id=306),
            "ICFG.LOOP_APPROX": dict(function_name="parse"),
            "ICFG.UNREACHABLE_APPROX": dict(function_name="parse"),
            "PAG.FUNOBJ_RESOLVE": dict(function_name="parse"),
            "PAG.ARGUMENT_RESOLVE": dict(function_name="parse", arg_no=0),
            "PAG.POINTS_TO": dict(function_name="parse", arg_no=0),
            "PAG.PARAMETER_ALIAS": dict(function_name="parse", arg_no=0, target_function="sink", target_arg_no=0),
            "PAG.ARGUMENT_FLOW": dict(function_name="parse", arg_no=0),
            "PAG.LOAD_STORE_USES": dict(node_id=202),
            "PAG.DEF_USE_FLOW": dict(node_id=201),
            "PAG.SOURCE_SINK_FLOW": dict(node_id=201, target_function="sink"),
            "PAG.RETURN_FLOW": dict(function_name="parse"),
            "PAG.CALL_PARAM_FLOW": dict(function_name="sink", arg_no=0, callsite_id=303),
            "PAG.FIELD_ACCESS_FLOW": dict(node_id=202),
            "PAG.ALIAS_APPROX": dict(node_id=202, target_node_id=203),
            "CHG.CLASS_HIERARCHY": dict(class_name="Base"),
            "CHG.VIRTUAL_DISPATCH": dict(callsite_id=303),
            "CHG.OVERRIDE_RELATION": dict(class_name="Base", method_name="run"),
            "TYPE.NODE_TYPE_LOOKUP": dict(node_id=202),
            "TYPE.FIELD_INFO": dict(type_id=700, field_index=1),
            "XG.SOURCE_TO_GRAPH_IDS": dict(function_name="parse"),
            "XG.FUNCTION_EVIDENCE_BUNDLE": dict(function_name="parse"),
            "XG.BUG_LOCALIZATION": dict(problem_statement="parse path"),
            "XG.CHECK_BEFORE_USE": dict(node_id=202),
            "XG.SANITIZE_TO_SINK": dict(function_name="parse", arg_no=0, target_function="sink", sanitizer="sink"),
            "XG.API_MISUSE_CONTEXT": dict(function_name="sink"),
            "XG.REPAIR_LOCALIZATION_CONTEXT": dict(problem_statement="parse path"),
        }
        self.assertEqual(set(plans), set(CAPABILITIES))
        for capability, params in plans.items():
            with self.subTest(capability=capability):
                result = self.run_plan(capability, **params)
                self.assertTrue(result["ok"])
                self.assertGreater(result["record_count"], 0)
                self.assertEqual(result["capability"]["id"], capability)
                self.assertEqual(result["guard"]["status"], "ok")
                self.assertLessEqual(result["record_count"], 100)
                local = QueryLocalSummary(result["capability"]["example"], result, "gpt-4o")
                self.assertEqual(local.stats["records"], result["record_count"])
                self.assertTrue(local.input["records"])
                evidence = result["evidence_graph"]
                for path in evidence["paths"]:
                    self.assertEqual(path["hop_count"], len(path["edge_indices"]))
                    self.assertEqual(len(path["nodes"]), path["hop_count"] + 1)
                    for a, b, index in zip(path["nodes"], path["nodes"][1:], path["edge_indices"]):
                        edge = evidence["edges"][index]
                        self.assertEqual(edge["source"], a)
                        self.assertEqual(edge["target"], b)

    def test_alternative_paths_and_cycles_keep_actual_hops(self):
        result = self.run_plan("CG.CALL_CHAIN", node_id=1, target_node_id=3)
        self.assertEqual(sorted(p["hop_count"] for p in result["evidence_graph"]["paths"]), [1, 2])
        result = self.run_plan("CG.CYCLE_DETECTION", node_id=1)
        self.assertEqual(result["evidence_graph"]["enumeration"], "cycle_witnesses")
        self.assertTrue(all(p["nodes"][0] == p["nodes"][-1] for p in result["evidence_graph"]["paths"]))

    def test_exported_funobj_ids_join_arguments_returns_and_mappings(self):
        result = self.run_plan("XG.SOURCE_TO_GRAPH_IDS", function_name="parse")
        row = result["records"][0]
        self.assertEqual((row["callgraph_node_id"], row["fun_obj_var_id"]), (1, 101))
        self.assertEqual(row["argument_node_ids"], [201])
        self.assertEqual(row["icfg_entry_ids"], [301])
        self.assertTrue(self.run_plan("PAG.RETURN_FLOW", function_name="parse")["records"])

    def test_alias_and_validation_queries_never_claim_semantic_proof(self):
        alias = self.run_plan("PAG.ALIAS_APPROX", node_id=202, target_node_id=204)
        self.assertEqual(alias["analysis"]["conclusion"], "unknown")
        context = self.run_plan("XG.CHECK_BEFORE_USE", node_id=202)
        guards = [r for r in context["records"] if r.get("relation") == "branch_can_reach_use"]
        self.assertTrue(guards)
        self.assertTrue(all(r["check_dominates_use"] == "unknown" for r in guards))
        sanitize = self.run_plan("XG.SANITIZE_TO_SINK", function_name="parse", target_function="sink", sanitizer="sink")
        graphs = {r.get("graph") for r in sanitize["records"]}
        self.assertTrue({"PAG", "ICFG", "CallGraph"}.issubset(graphs))
        self.assertTrue(all(r["sanitized"] == "unknown" for r in sanitize["records"] if "sanitized" in r))

    def test_type_fields_preserve_exported_metadata(self):
        result = self.run_plan("TYPE.FIELD_INFO", node_id=202, field_index=1)
        self.assertTrue(any(r.get("field_type_id") == 702 for r in result["records"]))
        self.assertTrue(any(r.get("fld_idx_2_type_map") == "0:701,1:702" for r in result["records"]))

    def test_original_source_locations_map_to_function_ids_and_patch_candidates(self):
        result = self.run_plan("XG.SOURCE_TO_GRAPH_IDS", file="case.cpp", line=2)
        self.assertEqual(result["records"][0]["fun_obj_var_id"], 101)
        result = self.run_plan("SRC.PATCH_LOCATION", file="case.cpp", line=2)
        self.assertEqual(result["records"][0]["function"], "parse")

    def test_cross_graph_small_result_budget_preserves_each_scope(self):
        result = self.backend.execute_plan(empty_plan("XG.API_MISUSE_CONTEXT", function_name="sink", depth=3, limit=5))
        self.assertEqual({r.get("graph") for r in result["records"]}, {"ICFG", "PAG", "CallGraph"})

    def test_parallel_calls_are_counted_separately_from_distinct_callees(self):
        self.db.edge("CallGraphEdge", ("CallGraphNode", 1), ("CallGraphNode", 3), csid=123)
        result = self.run_plan("CG.FANIN_FANOUT", function_name="parse")
        self.assertEqual(result["records"][0]["fan_out"], 2)
        self.assertEqual(result["records"][0]["outgoing_call_sites"], 3)

    def test_class_hierarchy_prioritizes_real_edges_over_isolated_nodes(self):
        result = self.backend.execute_plan(empty_plan("CHG.CLASS_HIERARCHY", class_name="Base", limit=1))
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["evidence_graph"]["paths"][0]["hop_count"], 1)

    def test_callee_argument_mapping_does_not_reuse_the_source_function_id(self):
        result = self.run_plan("PAG.CALL_PARAM_FLOW", function_id=101, target_function="sink", arg_no=0)
        self.assertTrue(result["records"])
        self.assertEqual(result["evidence_graph"]["paths"][0]["nodes"][-1], {"label": "ArgValVar", "id": 204})
        excluded = self.run_plan("PAG.CALL_PARAM_FLOW", function_id=105, target_function="sink", arg_no=0)
        self.assertEqual(excluded["record_count"], 0)

    def test_both_flow_directions_keep_physical_edge_orientation(self):
        result = self.run_plan("PAG.DEF_USE_FLOW", node_id=202, direction="both", edge_labels=["StoreStmt", "LoadStmt"])
        paths = result["evidence_graph"]["paths"]
        self.assertTrue(any(path["nodes"][0]["id"] == 201 and path["nodes"][-1]["id"] == 202 for path in paths))
        self.assertTrue(any(path["nodes"][0]["id"] == 202 and path["nodes"][-1]["id"] == 203 for path in paths))

    def test_capability_progress_stream_forwards_catalog_selection_and_final_result(self):
        handler = object.__new__(app.RequestHandler)
        handler.path, handler.headers = "/api/query/capabilities", {"Accept": "application/x-ndjson"}
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        handler._read_json = Mock(return_value=dict(query="Find parse", capability="CG.FUNCTION_RESOLVE", scope="CallGraph", max_depth=40, max_entities=100))
        def execute(question, **options):
            self.assertEqual(question, "Find parse")
            self.assertEqual(options["capability"], "CG.FUNCTION_RESOLVE")
            self.assertEqual(options["scope"], "CallGraph")
            self.assertEqual(options["max_depth"], 40)
            options["on_progress"](dict(stage="database", message="Reading CallGraph", cypher="MATCH (n:CallGraphNode) RETURN n.id LIMIT 1", timeout_s=30))
            return {"ok": True, "records": []}
        with patch.object(CapabilityQueryBackend, "execute", side_effect=execute), patch.object(CapabilityQueryBackend, "close") as close:
            handler.do_POST()
        events = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
        self.assertEqual([event["type"] for event in events], ["progress", "result"])
        self.assertEqual(events[0]["stage"], "database")
        close.assert_called_once_with()

    def test_partial_results_and_missing_graph_are_explicit(self):
        result = self.backend.execute_plan(empty_plan("PAG.ARGUMENT_FLOW", node_id=201, limit=1))
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["evidence_graph"]["complete"])
        with patch.object(self.db, "run", side_effect=DatabaseQueryTimeout("timeout")):
            result = self.run_plan("CG.FUNCTION_RESOLVE", function_name="parse")
        self.assertEqual(result["status"], "partial")
        with patch.object(self.db, "run", side_effect=RuntimeError("No such graph: SVFType")):
            result = self.run_plan("TYPE.FIELD_INFO", type_id=700)
        self.assertEqual(result["status"], "requires_graph")

    def test_path_and_template_injection_and_missing_targets_are_rejected(self):
        for params in [dict(capability="SRC.SOURCE_WINDOW", file="../outside.cpp", line=1),
                       dict(capability="CG.CALL_CHAIN", function_name="parse"),
                       dict(capability="PAG.ARGUMENT_FLOW", node_id=201, edge_labels=["StoreStmt]->() DELETE node //"]),
                       dict(capability="ICFG.SEED_RESOLVE", node_id=301, node_label="FunObjVar")]:
            with self.subTest(params=params), self.assertRaises(CapabilityQueryError):
                self.backend.execute_plan(empty_plan(**params))
        malicious = "parse' OR true DELETE node"
        result = self.run_plan("CG.FUNCTION_RESOLVE", function_name=malicious)
        self.assertEqual(result["record_count"], 0)
        self.assertNotIn(malicious, result["execution_queries"][0]["cypher"])

    def test_incomplete_neighbor_cache_is_not_reused(self):
        g = CapabilityGraph(self.env, QueryLimits(), clients={"CallGraph": self.db})
        root = g.node("CallGraph", 1, "CallGraphNode")[0]
        iterator = g.neighbors("CallGraph", root, ["CallGraphEdge"])
        next(iterator)
        iterator.close()
        self.assertEqual(len(list(g.neighbors("CallGraph", root, ["CallGraphEdge"]))), 2)

    def test_planner_uses_selected_model_contract_and_exact_limits(self):
        plan = empty_plan("CG.CALL_CHAIN", depth=40, limit=100, node_id=1, target_node_id=3)
        fake = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=SimpleNamespace(output_text=json.dumps(plan)))))
        self.backend.llm_client = fake
        self.backend.model = "gpt-4o"
        result = self.backend.execute("Paths from CallGraph 1 to 3", capability="CG.CALL_CHAIN", max_depth=40, max_entities=100)
        self.assertTrue(result["ok"])
        options = fake.responses.create.call_args_list[0].kwargs
        self.assertEqual(options["model"], "gpt-4o")
        schema = options["text"]["format"]["schema"]["properties"]
        self.assertEqual(schema["depth"]["enum"], [40])
        self.assertEqual(schema["limit"]["enum"], [100])
        fake.responses.create.return_value.output_text = json.dumps(dict(plan, node_id=999))
        with self.assertRaises(CapabilityQueryError):
            self.backend.execute("Paths from CallGraph 1 to 3", max_depth=40, max_entities=100)

    def test_auto_selection_is_made_by_the_model_within_each_page_scope(self):
        cases = [
            ("CallGraph", "CG.FUNCTION_RESOLVE", "Resolve parse", dict(function_name="parse")),
            ("CallGraph", "CG.CALL_CHAIN", "Find call paths from parse to sink", dict(function_name="parse", target_function="sink")),
            ("ICFG", "ICFG.SEED_RESOLVE", "Find the entry of parse", dict(function_name="parse")),
            ("ICFG", "ICFG.BRANCH_CONTEXT", "Show branches before node 304", dict(node_id=304)),
            ("PAG", "PAG.ARGUMENT_RESOLVE", "Resolve argument zero of parse", dict(function_name="parse", arg_no=0)),
            ("PAG", "PAG.ARGUMENT_FLOW", "Trace argument zero through parse", dict(function_name="parse", arg_no=0)),
            ("", "SRC.KEYWORD_USAGE", "Find uses of sink in source", dict(keyword="sink")),
            ("", "XG.SOURCE_TO_GRAPH_IDS", "Map parse across the graphs", dict(function_name="parse")),
        ]
        for scope, selected, question, values in cases:
            with self.subTest(scope=scope, capability=selected):
                plan = empty_plan(selected, depth=3, limit=20, **values)
                create = Mock(return_value=SimpleNamespace(output_text=json.dumps(plan)))
                self.backend.llm_client = SimpleNamespace(responses=SimpleNamespace(create=create))
                progress = []
                result = self.backend.execute(question, scope=scope, max_depth=3, max_entities=20, on_progress=progress.append)
                self.assertEqual(result["capability"]["id"], selected)
                self.assertGreater(result["record_count"], 0)
                request = create.call_args_list[0].kwargs
                available = request["text"]["format"]["schema"]["properties"]["capability"]["enum"]
                expected = {item["id"] for item in CAPABILITIES.values() if not scope or item["group"] == scope}
                self.assertEqual(set(available), expected)
                self.assertGreater(len(available), 1)
                context = json.loads(request["input"][-1]["content"])
                self.assertEqual(context["user_request"], question)
                self.assertEqual({item["id"] for item in context["catalog"]}, expected)
                self.assertEqual(progress[0]["stage"], "schema")
                stages = [event["stage"] for event in progress]
                self.assertLess(stages.index("schema"), stages.index("matching"))
                self.assertLess(stages.index("matching"), stages.index("compiling"))
                self.assertLess(stages.index("compiling"), stages.index("applying"))
                if "database" in stages:
                    self.assertLess(stages.index("applying"), stages.index("database"))
                selected_event = next(event for event in progress if event.get("capability"))
                self.assertEqual(selected_event["capability"]["id"], selected)
                self.assertTrue(selected_event["capability"]["expected_output"])
                self.assertEqual(selected_event["model"], self.backend.model)
                database_event = next((index for index, event in enumerate(progress) if event["stage"] == "database"), len(progress))
                self.assertLess(progress.index(selected_event), database_event)

    def test_auto_selection_rejects_a_model_choice_outside_the_current_graph(self):
        plan = empty_plan("PAG.FUNOBJ_RESOLVE", function_name="parse")
        create = Mock(return_value=SimpleNamespace(output_text=json.dumps(plan)))
        self.backend.llm_client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with self.assertRaises(CapabilityQueryError) as error:
            self.backend.execute("Find parse", scope="CallGraph")
        self.assertEqual(error.exception.code, "INVALID_MODEL_OUTPUT")
        self.assertEqual(self.db.calls, [])

    def test_new_pages_catalog_and_apr_session_configuration(self):
        def handler(path, data=None):
            h = object.__new__(app.RequestHandler)
            h.path, h.headers, h.wfile = path, {}, io.BytesIO()
            h._json, h._serve_static, h.send_error = Mock(), Mock(), Mock()
            h._read_json = Mock(return_value=data)
            return h
        for path, expected in [("/query/capabilities", "query.html"), ("/icse-demo/query/capabilities", "icse-query.html")]:
            h = handler(path); h.do_GET(); h._serve_static.assert_called_once_with(expected)
        h = handler("/api/codegraphhunter/capabilities"); h.do_GET()
        self.assertEqual(h._json.call_args.args[1]["count"], 45)
        h = handler("/api/codegraphhunter/icse-demo/query/capabilities", dict(query="parse", capability="SRC.FUNCTION_SEARCH", model="gpt-4o", max_depth=40, max_entities=100))
        h._icse_session = Mock(return_value=("sid", {"api_key": "session-key", "graphdb_password": "session-pass"}, {}))
        # Patch __init__ (not the class): isinstance checks must still use its type.
        with patch.object(app._FROZEN_DEMO_BACKEND, "execution_environment", return_value={"OPENAI_API_KEY": "session-key", "TUGRAPH_BOLT_PASS": "session-pass"}), \
             patch.object(CapabilityQueryBackend, "execute", return_value={"ok": True}) as execute, \
             patch.object(CapabilityQueryBackend, "close"):
            h.do_POST()
        self.assertEqual(h._json.call_args.args[0], 200)
        execute.assert_called_once_with("parse", capability="SRC.FUNCTION_SEARCH", scope="", max_depth=40, max_entities=100)


if __name__ == "__main__":
    unittest.main()
