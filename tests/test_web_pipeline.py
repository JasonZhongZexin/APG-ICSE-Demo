import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codegraphhunter.web.app import JobManager, PipelineJob
from codegraphhunter.web.pipeline import (
    PipelinePlan,
    PipelineStage,
    PipelineConfigurationError,
    build_demo_pipeline_plan,
    build_pipeline_plan,
    infer_repo_root,
)
from codegraphhunter.localization.agentic_fault_localizer import AgenticFaultLocalizer


class WebPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / ".git").mkdir()
        (self.root / "artifacts").mkdir()
        self.bc = self.root / "artifacts" / "program.bc"
        self.bc.write_bytes(b"BC")
        self.problem = self.root / "problem_statement.txt"
        self.problem.write_text("Observed behavior differs from expected output.", encoding="utf-8")
        self.graphdb = self.root / "graphdb-wpa"
        self.graphdb.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.graphdb.chmod(0o755)
        self.demo_script = self.root / "cpphttplib-demo.sh"
        self.demo_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.demo_script.chmod(0o755)
        self.demo_output = self.root / "demo-output"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_infers_nearest_git_repo(self):
        self.assertEqual(infer_repo_root(self.bc, {}), self.root)

    def test_builds_two_stage_plan_with_only_two_input_paths(self):
        plan = build_pipeline_plan(
            str(self.bc),
            str(self.problem),
            {
                "GRAPHDB_WPA": str(self.graphdb),
                "TUGRAPH_BOLT_PASS": "test-only",
                "CODEGRAPHHUNTER_WEB_MAX_QUERIES": "7",
            },
        )
        self.assertEqual(plan.repo_root, self.root)
        self.assertEqual([stage.key for stage in plan.stages], ["graph", "localize"])
        self.assertEqual(plan.stages[0].command[-1], str(self.bc))
        controller = plan.stages[1].command
        self.assertIn("--use-graph-localizer", controller)
        self.assertIn("--patch-problem-file", controller)
        self.assertIn(str(self.problem), controller)
        self.assertIn("7", controller)

    def test_rejects_non_bitcode_input(self):
        wrong = self.root / "program.ll"
        wrong.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(PipelineConfigurationError, "not a .bc"):
            build_pipeline_plan(
                str(wrong),
                str(self.problem),
                {"GRAPHDB_WPA": str(self.graphdb), "TUGRAPH_BOLT_PASS": "test-only"},
            )

    def test_requires_tugraph_password_before_run(self):
        with self.assertRaisesRegex(PipelineConfigurationError, "TUGRAPH_BOLT_PASS"):
            build_pipeline_plan(
                str(self.bc),
                str(self.problem),
                {"GRAPHDB_WPA": str(self.graphdb)},
            )

    def test_demo_plan_runs_fixed_script_and_declares_allowlisted_outputs(self):
        plan = build_demo_pipeline_plan(
            "ignored-by-fixed-demo",
            "ignored-by-fixed-demo",
            {
                "TUGRAPH_BOLT_PASS": "test-only",
                "TUGRAPH_BOLT_DB": "existing",
                "CODEGRAPHHUNTER_DEMO_SCRIPT": str(self.demo_script),
                "CODEGRAPHHUNTER_DEMO_CASE": str(self.root),
                "CODEGRAPHHUNTER_DEMO_PROBLEM": str(self.problem),
                "CODEGRAPHHUNTER_DEMO_OUTPUT": str(self.demo_output),
            },
        )
        self.assertEqual(plan.mode, "demo")
        self.assertIsNone(plan.bc_path)
        self.assertEqual(plan.repo_root, self.root)
        self.assertEqual(plan.database, "existing")
        self.assertEqual([stage.key for stage in plan.stages], ["cpphttplib_demo"])
        command = plan.stages[0].command
        self.assertEqual(command, (str(self.demo_script),))
        self.assertEqual(
            [path.name for path in plan.external_artifacts],
            [
                "final_patch.diff",
                "pipeline_key.log",
                "pipeline_full.log",
                "pre_patch_test.log",
                "post_patch_test.log",
            ],
        )
        self.assertTrue(all(path.parent == self.demo_output for path in plan.external_artifacts))
        self.assertEqual(plan.external_artifact_roots, (self.demo_output / "debug",))

    def test_demo_plan_requires_executable_script(self):
        self.demo_script.chmod(0o644)
        with self.assertRaisesRegex(PipelineConfigurationError, "not executable"):
            build_demo_pipeline_plan(
                "",
                "",
                {
                    "TUGRAPH_BOLT_PASS": "test-only",
                    "CODEGRAPHHUNTER_DEMO_SCRIPT": str(self.demo_script),
                    "CODEGRAPHHUNTER_DEMO_CASE": str(self.root),
                    "CODEGRAPHHUNTER_DEMO_PROBLEM": str(self.problem),
                },
            )

    def test_graph_query_workspace_covers_all_demo_pages(self):
        project_root = Path(__file__).resolve().parents[1]
        static_root = project_root / "codegraphhunter" / "web" / "static"
        demo_html = (static_root / "demo-v122.html").read_text(encoding="utf-8")
        demo_js = (static_root / "demo-v122.js").read_text(encoding="utf-8")
        demo_css = (static_root / "demo-v122.css").read_text(encoding="utf-8")
        frozen_backend = (project_root / "codegraphhunter" / "web" / "frozen_demo_backend.py").read_text(encoding="utf-8")
        query_html = (static_root / "query.html").read_text(encoding="utf-8")
        query_js = (static_root / "query.js").read_text(encoding="utf-8")
        styles_css = (static_root / "styles.css").read_text(encoding="utf-8")
        app_py = (project_root / "codegraphhunter" / "web" / "app.py").read_text(
            encoding="utf-8"
        )

        for graph in ("icfg", "pag", "callgraph"):
            route = f"/query/{graph}"
            self.assertIn(route, demo_html)
            self.assertIn(route, query_html)
            self.assertIn(route, app_py)
            self.assertIn(f"{graph}:", query_js)

        self.assertIn("Natural-language request", query_html)
        self.assertIn("GENERATED CYPHER", query_html)
        self.assertIn("LIVE RESULT", query_html)
        self.assertNotIn("MOCK RESULT", query_html)
        self.assertIn("GraphQueryRequest.run(request.url, request.body", query_js)
        self.assertIn('["callgraph", "icfg", "pag", "capabilities"].includes(graphKey)', query_js)
        self.assertIn('QueryCapabilities.request(graphKey', query_js)
        self.assertIn("runConstrainedQuery(question, limits)", query_js)
        self.assertIn("BOUND PARAMETERS", query_html)
        self.assertIn("payload.params", query_js)
        self.assertIn("Query execution failed", query_js)
        self.assertIn("grid-template-columns: minmax(0, calc(100% - 308px))", styles_css)
        self.assertIn(".schema-panel { position: relative;", styles_css)
        self.assertIn("white-space: normal; overflow-wrap: anywhere;", styles_css)
        self.assertIn("CodeGraphHunter_demo_v1_2_2/bin/codegraphhunter-demo-v1.2.2", demo_html)
        self.assertIn("Run CodeGraphHunter Demo", demo_html)
        self.assertIn("Multi-Graph Retrieval", demo_html)
        self.assertIn("structured repair sketch", demo_html)
        self.assertIn("/api/codegraphhunter/demo/run", demo_js)
        self.assertNotIn('requestJson("/api/codegraphhunter/demo/latest")', demo_js)
        self.assertIn('id="demoResults" class="hidden"', demo_html)
        self.assertIn("/api/codegraphhunter/demo/artifact", demo_js)
        self.assertNotIn("/api/demo/jobs", demo_js)
        self.assertIn('self._serve_static("demo-v122.html")', app_py)
        self.assertIn("/api/codegraphhunter/demo/run", app_py)
        self.assertIn("frontend_result.json", frozen_backend)
        self.assertIn("/tmp/codegraphhunter_frontend_demo", frozen_backend)
        self.assertIn("/tmp/codegraphhunter_patch_demo", frozen_backend)
        self.assertIn(".v122-timeline", demo_css)

    def test_source_root_override_supports_external_bitcode(self):
        external = self.root.parent / f"external-{os.getpid()}.bc"
        try:
            external.write_bytes(b"BC")
            self.assertEqual(
                infer_repo_root(external, {"CODEGRAPHHUNTER_SOURCE_ROOT": str(self.root)}),
                self.root,
            )
        finally:
            external.unlink(missing_ok=True)

    def test_localizer_can_select_source_file_without_file_hint(self):
        source = self.root / "server.c"
        source.write_text(
            "void handleCount(client *c) {\n"
            "    value = lookupKey(c);\n"
            "    if (value == 0) addReply(c, shared.null);\n"
            "    return;\n"
            "}\n",
            encoding="utf-8",
        )
        localizer = AgenticFaultLocalizer(
            repo_root=str(self.root),
            problem_statement="COUNT on a missing key should return a null array.",
            patch_source_file="",
            bolt_router=None,
        )
        result = localizer.localize()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["selected_location"]["source_file"], "server.c")
        self.assertEqual(result["selected_location"]["function"], "handleCount")

    def test_job_runner_streams_output_and_extracts_summary(self):
        output = (
            "print('[GRAPH LOCALIZER] function=handleCount file=server.c arg_no=0');"
            "print('last_retrieval_stop_reason = OBJECTIVE_MET')"
        )
        plan = PipelinePlan(
            bc_path=self.bc,
            problem_path=self.problem,
            repo_root=self.root,
            stages=(
                PipelineStage("fake", "Test stage", (sys.executable, "-c", output)),
            ),
        )
        job = PipelineJob(plan, artifact_root=self.root / "web-artifacts")
        JobManager()._run(job)
        snapshot = job.snapshot()
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["selected_function"], "handleCount")
        self.assertEqual(snapshot["selected_file"], "server.c")
        self.assertEqual(snapshot["stop_reason"], "OBJECTIVE_MET")
        self.assertEqual(len(snapshot["artifacts"]), 1)
        self.assertEqual(snapshot["artifacts"][0]["kind"], "Stage summary")
        artifact = job.read_artifact(snapshot["artifacts"][0]["path"])
        self.assertIn('"stage_label": "Test stage"', artifact["content"])

    def test_job_runner_collects_declared_demo_outputs_and_intermediate_summaries(self):
        self.demo_output.mkdir()
        expected = {
            "final_patch.diff": "--- a/file\n+++ b/file\n-old\n+new\n",
            "pipeline_key.log": "[GRAPH LOCALIZER] function=is_valid_path file=httplib.h\n",
            "pipeline_full.log": "complete pipeline trace\n",
            "pre_patch_test.log": "reproduction failed as expected\n",
            "post_patch_test.log": "validation passed\n",
        }
        for name, content in expected.items():
            (self.demo_output / name).write_text(content, encoding="utf-8")
        undeclared = self.demo_output / "test_embedded_nul_path.sh"
        undeclared.write_text("#!/bin/sh\n", encoding="utf-8")
        debug_root = self.demo_output / "debug"
        local_dir = debug_root / "attempt_001" / "local_summaries"
        local_dir.mkdir(parents=True)
        (local_dir / "step_0001_PAG_summary.json").write_text(
            '{"summary_id":"LS-0001"}\n', encoding="utf-8"
        )
        (debug_root / "attempt_001" / "global_summary.json").write_text(
            '{"global_summary_id":"GS-0001"}\n', encoding="utf-8"
        )
        (debug_root / "graph_fault_localization.json").write_text(
            '{"selected_location":{"function":"is_valid_path"}}\n', encoding="utf-8"
        )
        (debug_root / "ignored.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (debug_root / "outside.log").symlink_to(self.problem)
        plan = PipelinePlan(
            bc_path=None,
            problem_path=self.problem,
            repo_root=self.root,
            stages=(PipelineStage("fake-demo", "Fake demo", (sys.executable, "-c", "print('ok')")),),
            mode="demo",
            external_artifacts=tuple(self.demo_output / name for name in expected),
            external_artifact_roots=(debug_root,),
        )
        job = PipelineJob(plan, artifact_root=self.root / "web-demo-artifacts")

        JobManager()._run(job)

        artifacts = job.snapshot()["artifacts"]
        names = {artifact["name"] for artifact in artifacts}
        self.assertTrue(set(expected).issubset(names))
        self.assertNotIn(undeclared.name, names)
        self.assertNotIn("ignored.sh", names)
        self.assertNotIn("outside.log", names)
        paths = {artifact["path"] for artifact in artifacts}
        self.assertIn(
            "demo_intermediates/debug/attempt_001/local_summaries/step_0001_PAG_summary.json",
            paths,
        )
        self.assertIn(
            "demo_intermediates/debug/attempt_001/global_summary.json",
            paths,
        )
        self.assertIn(
            "demo_intermediates/debug/graph_fault_localization.json",
            paths,
        )
        kinds = {artifact["name"]: artifact["kind"] for artifact in artifacts}
        self.assertEqual(kinds["final_patch.diff"], "Patch diff")
        self.assertEqual(kinds["pipeline_key.log"], "Key pipeline log")
        self.assertEqual(kinds["pipeline_full.log"], "Full pipeline log")
        self.assertEqual(kinds["pre_patch_test.log"], "Pre-patch test")
        self.assertEqual(kinds["post_patch_test.log"], "Post-patch test")
        self.assertEqual(kinds["step_0001_PAG_summary.json"], "Local summary")
        self.assertEqual(kinds["global_summary.json"], "Global summary")
        self.assertEqual(kinds["graph_fault_localization.json"], "Fault localization")
        diff_path = next(item["path"] for item in artifacts if item["name"] == "final_patch.diff")
        self.assertEqual(job.read_artifact(diff_path)["content"], expected["final_patch.diff"])

    def test_artifact_reader_rejects_path_traversal(self):
        plan = PipelinePlan(
            bc_path=self.bc,
            problem_path=self.problem,
            repo_root=self.root,
            stages=(),
        )
        job = PipelineJob(plan, artifact_root=self.root / "web-artifacts")
        with self.assertRaisesRegex(ValueError, "escapes"):
            job.read_artifact("../../problem_statement.txt")

    def test_controller_persists_each_local_and_global_summary(self):
        debug_dir = self.root / "controller-artifacts"
        env = os.environ.copy()
        env["CODEGRAPHHUNTER_PATCH_DEBUG_DIR"] = str(debug_dir)
        command = [
            sys.executable,
            "-m",
            "codegraphhunter.controller.attempt_controller_cli",
            "--schema",
            "codegraphhunter/resources/normalized_schema.v1.json",
            "--caps",
            "codegraphhunter/datasource/tugraph_cypher_capabilities.v1.json",
            "--max-iterations",
            "1",
            "--max-queries-total",
            "1",
            "--max-replans",
            "0",
        ]
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        local_files = list((debug_dir / "attempt_001" / "local_summaries").glob("*.json"))
        self.assertEqual(len(local_files), 1)
        local_summary = json.loads(local_files[0].read_text(encoding="utf-8"))
        self.assertEqual(local_summary["summary_id"], "LS-0001")
        global_path = debug_dir / "attempt_001" / "global_summary.json"
        self.assertTrue(global_path.is_file())
        global_summary = json.loads(global_path.read_text(encoding="utf-8"))
        self.assertEqual(global_summary["global_summary_id"], "GS-0001")


if __name__ == "__main__":
    unittest.main()
