import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codegraphhunter.web.capability_query_backend import CapabilityQueryBackend, empty_plan
from codegraphhunter.web.query_local_summary import MAX_INPUT_CHARS, QueryLocalSummary, _dump, summary_total_timeout
from tests import test_query_capabilities as fixtures


class SummaryClient:
    def __init__(self, plan, narrative=None, error=None):
        self.plan, self.narrative, self.error = plan, narrative, error
        self.responses = self
        self.calls, self.options = [], []

    def with_options(self, **options):
        self.options.append(options)
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["text"]["format"]["name"] == "capability_query_plan":
            return SimpleNamespace(output_text=json.dumps(self.plan))
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=json.dumps(self.narrative))


def narrative(ref="record-1"):
    return dict(overview="The returned evidence identifies matching code and graph context.",
                findings=[dict(text="The referenced record describes the retrieved evidence.", evidence_refs=[ref])],
                limitations=["These observations do not establish runtime feasibility."])


class BatchSummaryClient(SummaryClient):
    def __init__(self):
        super().__init__(None)
        self.evidence_inputs, self.merge_inputs = [], []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        context = json.loads(kwargs["input"][-1]["content"])
        if "batch_summaries" in context:
            self.merge_inputs.append(context)
            refs = [ref for summary in context["batch_summaries"] for finding in summary["findings"] for ref in finding["evidence_refs"]]
            summary = narrative(refs[-1])
            summary["overview"] = "Combined query-wide observations: " + ", ".join(refs)
        else:
            self.evidence_inputs.append(context)
            refs = [row["ref"] for row in context["records"] + context["paths"]]
            summary = narrative(refs[-1])
        return SimpleNamespace(output_text=json.dumps(summary))


class QueryLocalSummaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CapabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tmp.cleanup)

    def backend(self, plan, *, content=None, error=None, model="gpt-4o"):
        client = SummaryClient(plan, narrative() if content is None else content, error)
        env = dict(self.fixture.env, CODEGRAPHHUNTER_LLM_MODEL=model, CODEGRAPHHUNTER_QUERY_SUMMARY_TIMEOUT_S="7")
        return CapabilityQueryBackend(env, llm_client=client, clients=self.fixture.backend.clients), client

    def test_nonempty_query_gets_a_separate_summary_call_using_the_selected_model(self):
        plan = empty_plan("CG.CALL_CHAIN", function_name="parse", target_function="sink", depth=5, limit=100)
        backend, client = self.backend(plan, content=narrative("path-1"), model="gpt-5.4")
        progress = []
        result = backend.execute("Call paths from parse to sink", max_depth=5, max_entities=100, on_progress=progress.append)
        self.assertTrue(result["ok"])
        summary = result["local_summary"]
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["provider"], "openai")
        self.assertEqual(summary["model"], "gpt-5.4")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([call["model"] for call in client.calls], ["gpt-5.4", "gpt-5.4"])
        self.assertEqual(client.options, [dict(timeout=7, max_retries=0)])
        self.assertEqual(client.calls[-1]["timeout"], 7)
        self.assertNotIn("temperature", client.calls[-1])
        evidence = json.loads(client.calls[-1]["input"][-1]["content"])
        self.assertEqual(evidence["user_request"], "Call paths from parse to sink")
        self.assertEqual(evidence["capability"]["id"], "CG.CALL_CHAIN")
        self.assertEqual(evidence["totals"]["records"], result["record_count"])
        self.assertEqual(sorted(path["hop_count"] for path in evidence["paths"]), [1, 2])
        early = next(event for event in progress if event["stage"] == "summarizing")
        self.assertEqual(early["result"]["local_summary"]["status"], "pending")
        self.assertEqual(early["result"]["records"], result["records"])
        self.assertEqual(early["result"]["evidence_graph"], result["evidence_graph"])
        self.assertEqual(early["timeout_s"], 7)

    def test_source_type_and_cross_graph_records_are_in_the_model_context(self):
        cases = [
            ("SRC.SOURCE_WINDOW", dict(file="case.cpp", line=2), "case.cpp"),
            ("TYPE.FIELD_INFO", dict(type_id=700, field_index=1), "field_type_id"),
            ("XG.SOURCE_TO_GRAPH_IDS", dict(function_name="parse"), "argument_node_ids"),
            ("CG.FANIN_FANOUT", dict(direction="in"), "fan_in"),
        ]
        for capability, values, expected in cases:
            with self.subTest(capability=capability):
                plan = empty_plan(capability, depth=5, limit=100, **values)
                backend, client = self.backend(plan)
                result = backend.execute("Analyze case.cpp at line 2, type 700 and parse", max_depth=5, max_entities=100)
                self.assertEqual(result["local_summary"]["status"], "completed")
                self.assertIn(expected, client.calls[-1]["input"][-1]["content"])

    def test_empty_results_do_not_call_the_summary_model(self):
        plan = empty_plan("CG.FUNCTION_RESOLVE", function_name="missing_function")
        backend, client = self.backend(plan)
        progress = []
        result = backend.execute("Find missing_function", on_progress=progress.append)
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(result["local_summary"]["status"], "skipped")
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(any(event["stage"] == "summarizing" for event in progress))

    def test_summary_failure_preserves_results_and_does_not_expose_error_secrets(self):
        plan = empty_plan("PAG.ARGUMENT_FLOW", function_name="parse", arg_no=0, depth=5, limit=100)
        for error in (TimeoutError("timeout"), RuntimeError("secret-must-not-appear")):
            with self.subTest(error=type(error).__name__):
                backend, client = self.backend(plan, error=error)
                result = backend.execute("Trace argument zero of parse", max_depth=5, max_entities=100)
                self.assertTrue(result["ok"])
                self.assertGreater(result["record_count"], 0)
                self.assertTrue(result["evidence_graph"]["paths"])
                self.assertEqual(result["local_summary"]["status"], "fallback")
                self.assertEqual(result["local_summary"]["provider"], "deterministic")
                self.assertEqual(result["local_summary"]["statistics"]["records"], result["record_count"])
                self.assertNotIn("secret-must-not-appear", json.dumps(result))

    def test_invented_references_or_invalid_model_output_fall_back_to_measured_facts(self):
        plan = empty_plan("CG.FUNCTION_RESOLVE", function_name="parse")
        for output in (narrative("path-999"), {"overview": "Invented output"}, dict(narrative(), findings=[dict(text="No reference", evidence_refs=[])])):
            with self.subTest(output=output):
                backend, client = self.backend(plan, content=output)
                result = backend.execute("Find parse")
                self.assertEqual(result["local_summary"]["status"], "fallback")
                self.assertIn("1 record", result["local_summary"]["overview"])

    def test_partial_results_keep_mandatory_limits_even_if_the_model_omits_them(self):
        plan = empty_plan("PAG.ARGUMENT_FLOW", function_name="parse", arg_no=0, limit=1)
        backend, client = self.backend(plan, content=dict(narrative(), limitations=[]))
        result = backend.execute("Trace argument zero of parse", max_entities=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["local_summary"]["status"], "completed")
        self.assertTrue(any("partial evidence" in note for note in result["local_summary"]["limitations"]))
        self.assertFalse(json.loads(client.calls[-1]["input"][-1]["content"])["query_complete"])

    def test_large_cross_graph_context_includes_every_record_path_and_hop_in_bounded_batches(self):
        count = 200
        nodes = [dict(label="ValVar", id=index, properties={"source_loc": "x" * 5000}) for index in range(count + 1)]
        edges = [dict(source={"label": "ValVar", "id": index}, target={"label": "ValVar", "id": index + 1}, label="CopyStmt", properties={}) for index in range(count)]
        records = [dict(graph=("PAG", "ICFG", "CallGraph")[index % 3], node_id=index, source="x" * 20000) for index in range(1000)]
        result = dict(self.fixture.run_plan("XG.FUNCTION_EVIDENCE_BUNDLE", function_name="parse"), records=records,
                      evidence_graph=dict(nodes=nodes, edges=edges, paths=[dict(id="path-1", record_index=0, hop_count=count,
                         nodes=[dict(label="ValVar", id=index) for index in range(count + 1)], edge_indices=list(range(count)))], complete=False))
        local = QueryLocalSummary("Summarize this function " + "x" * 7900, result, "gpt-4o")
        self.assertGreater(len(local.batches), 1)
        self.assertTrue(all(len(_dump(batch)) <= MAX_INPUT_CHARS for batch in local.batches))
        rows = [row for batch in local.batches for row in batch["records"]]
        self.assertEqual({row["ref"] for row in rows}, {f"record-{index + 1}" for index in range(1000)})
        self.assertEqual({item["data"]["graph"] for item in rows}, {"PAG", "ICFG", "CallGraph"})
        fragments = [fragment for batch in local.batches for fragment in batch["paths"]]
        self.assertEqual([hop["hop"] for fragment in fragments for hop in fragment["ordered_hops"]], list(range(1, count + 1)))
        self.assertTrue(all(fragment["hop_count"] == count for fragment in fragments))
        self.assertFalse(local.coverage["path_details_omitted"])
        self.assertEqual(local.stats["records"], 1000)
        self.assertEqual(local.coverage["records_included"], 1000)
        self.assertEqual(local.coverage["hops_included"], count)

    def test_multiple_batches_are_all_interpreted_and_combined_into_one_query_wide_summary(self):
        result = self.fixture.run_plan("CG.CALL_CHAIN", function_name="parse", target_function="sink")
        result["records"] = [dict(note=f"result-{index}", source="x" * 950) for index in range(120)]
        paths = result["evidence_graph"]["paths"]
        result["evidence_graph"]["paths"] = [dict(paths[index % 2], id=f"path-{index + 1}", record_index=index) for index in range(12)]
        local = QueryLocalSummary("Compare all matching paths and records", result, "gpt-4o")
        client, progress = BatchSummaryClient(), []
        pending = local.snapshot()
        summary = local.generate(client, 7, on_progress=progress.append)
        self.assertEqual(summary["status"], "completed")
        self.assertTrue(summary["coverage"]["complete"])
        self.assertEqual(summary["coverage"]["records_included"], 120)
        self.assertEqual(summary["coverage"]["paths_included"], 12)
        self.assertEqual(summary["coverage"]["batches_processed"], len(local.batches))
        self.assertEqual(len(client.evidence_inputs), len(local.batches))
        self.assertTrue(client.merge_inputs)
        self.assertEqual({row["ref"] for batch in client.evidence_inputs for row in batch["records"]}, {f"record-{index + 1}" for index in range(120)})
        self.assertEqual({row["ref"] for batch in client.evidence_inputs for row in batch["paths"]}, {f"path-{index + 1}" for index in range(12)})
        self.assertTrue(all(len(call["input"][-1]["content"]) <= MAX_INPUT_CHARS for call in client.calls))
        self.assertTrue(any("path-12" in finding["evidence_refs"] for finding in summary["findings"]))
        self.assertTrue(all(event["stage"] == "summarizing" for event in progress))
        self.assertEqual(pending["coverage"]["batches_processed"], 0, "Early stream snapshots must not mutate")

    def test_failure_midway_does_not_claim_a_complete_query_wide_model_summary(self):
        result = self.fixture.run_plan("CG.FUNCTION_RESOLVE", function_name="parse")
        result["records"] = [dict(source="x" * 950) for _ in range(100)]
        local, client = QueryLocalSummary("All records", result, "gpt-4o"), BatchSummaryClient()
        original = client.create
        def fail_after_first(**kwargs):
            if client.calls:
                raise RuntimeError("private-secret")
            return original(**kwargs)
        client.create = fail_after_first
        summary = local.generate(client, 7)
        self.assertEqual(summary["status"], "fallback")
        self.assertFalse(summary["coverage"]["complete"])
        self.assertEqual(summary["coverage"]["batches_processed"], 1)
        self.assertIn("100 records", summary["overview"])
        self.assertNotIn("private-secret", json.dumps(summary))

    def test_large_batch_summaries_are_merged_hierarchically_without_dropping_a_group(self):
        result = self.fixture.run_plan("CG.FUNCTION_RESOLVE", function_name="parse")
        result["records"] = [dict(index=index, **{f"value-{field}": "x" * 900 for field in range(4)}) for index in range(100)]
        local, client = QueryLocalSummary("Compare all returned results " + "x" * 7000, result, "gpt-4o"), BatchSummaryClient()
        original = client.create
        def verbose(**kwargs):
            response = original(**kwargs)
            content = json.loads(response.output_text)
            context = json.loads(kwargs["input"][-1]["content"])
            refs = [ref for summary in context.get("batch_summaries", []) for finding in summary["findings"] for ref in finding["evidence_refs"]]
            if refs:
                content["findings"][0]["evidence_refs"] = list(dict.fromkeys(refs))
            content["findings"][0]["text"] = "An evidence-backed observation. " * 90
            content["findings"].append(dict(text="A comparison of the supplied results. " * 80, evidence_refs=content["findings"][0]["evidence_refs"]))
            return SimpleNamespace(output_text=json.dumps(content))
        client.create = verbose
        summary = local.generate(client, 7)
        self.assertEqual(summary["status"], "completed")
        self.assertGreater(len(client.merge_inputs), 1)
        self.assertTrue(any(context["merge_scope"] == "intermediate" for context in client.merge_inputs))
        self.assertEqual(client.merge_inputs[-1]["merge_scope"], "query_wide")
        expected = {row["ref"] for context in client.evidence_inputs for row in context["records"][-1:]}
        self.assertEqual({ref for finding in summary["findings"] for ref in finding["evidence_refs"]}, expected)
        self.assertTrue(all(len(call["input"][-1]["content"]) <= MAX_INPUT_CHARS for call in client.calls))

    def test_summary_total_deadline_and_disconnection_stop_additional_model_calls(self):
        result = self.fixture.run_plan("CG.FUNCTION_RESOLVE", function_name="parse")
        local, client = QueryLocalSummary("Find parse", result, "gpt-4o"), BatchSummaryClient()
        with patch("codegraphhunter.web.query_local_summary.time.monotonic", side_effect=[0, 6]):
            summary = local.generate(client, 7, total_timeout_s=5)
        self.assertEqual(summary["status"], "fallback")
        self.assertEqual(client.calls, [])
        def disconnected(event):
            raise BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            local.generate(client, 7, on_progress=disconnected)
        self.assertEqual(client.calls, [])
        self.assertEqual(summary_total_timeout({}), 180)
        self.assertEqual(summary_total_timeout({"CODEGRAPHHUNTER_QUERY_SUMMARY_TOTAL_TIMEOUT_S": "9000"}), 600)

    def test_query_credentials_are_reused_without_putting_them_in_summary_prompts(self):
        plan = empty_plan("CG.FUNCTION_RESOLVE", function_name="parse")
        client = SummaryClient(plan, narrative())
        env = dict(self.fixture.env, OPENAI_API_KEY="session-test-key", TUGRAPH_BOLT_PASS="session-test-db-password")
        backend = CapabilityQueryBackend(env, clients=self.fixture.backend.clients)
        before = dict(os.environ)
        with patch("openai.OpenAI", return_value=client) as constructor:
            result = backend.execute("Find parse")
        constructor.assert_called_once_with(api_key="session-test-key")
        self.assertEqual(result["local_summary"]["status"], "completed")
        self.assertNotIn("session-test-key", json.dumps(client.calls))
        self.assertNotIn("session-test-db-password", json.dumps(client.calls))
        self.assertEqual(before, dict(os.environ))


if __name__ == "__main__":
    unittest.main()
