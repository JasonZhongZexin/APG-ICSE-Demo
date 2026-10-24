import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codegraphhunter.repair.llm_patch_generator import LLMPatchGenerator, extract_unified_diff
from codegraphhunter.web.apr_runner import controller_command, llm_report, publish_result, run
from codegraphhunter.web.frozen_demo_backend import FrozenDemoBackend

PATCH = "--- a/example.cc\n+++ b/example.cc\n@@ -1 +1 @@\n-return true;\n+return false;\n"


class RequiredModelPatchTests(unittest.TestCase):
    def test_long_source_context_keeps_the_localized_function_instead_of_only_the_file_prefix(self):
        prefix = "// unrelated header\n" * 200
        function = "inline bool check() {\n  size_t i = 0;\n  while (i < 3) { ++i; }\n  return true;\n}\n"
        source = prefix + function + "// unrelated footer\n" * 200
        generator = LLMPatchGenerator(max_source_chars=400, require_llm=True)
        prompt = generator._user_prompt("example.cc", source, {}, [], fault_diagnosis="Patch source: `example.cc:201`")
        excerpt = prompt.split("<<<SOURCE_FILE_START>>>\n")[1].split("<<<SOURCE_FILE_END>>>")[0]
        self.assertIn(function, excerpt)
        self.assertIn("Exact current source excerpt follows, starting at file line", prompt)
        self.assertLess(len(excerpt), 450)

    def test_gpt_hunk_count_errors_are_corrected_without_changing_source_edits(self):
        source = "\ninline bool check() {\n  size_t level = 0;\n  for (;;) {\n    switch (level) {\n"
        malformed = "--- a/example.cc\n+++ b/example.cc\n@@ -1,6 +1,10 @@\n \n inline bool check() {\n+  return false;\n+\n   size_t level = 0;\n   for (;;) {\n     switch (level) {\n"
        normalized = extract_unified_diff(malformed)
        self.assertIn("@@ -1,5 +1,7 @@", normalized)
        self.assertEqual(normalized.splitlines()[3:], malformed.splitlines()[3:])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.cc").write_text(source)
            patch_file = root / "generated.diff"
            patch_file.write_text(normalized + "\n")
            check = subprocess.run(["git", "apply", "--check", str(patch_file)], cwd=root, capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_hunk_recount_handles_multiple_files_and_missing_final_newlines(self):
        model_diff = "--- a/a.cc\n+++ b/a.cc\n@@ -1,9 +1,9 @@\n-old\n+new\n\\ No newline at end of file\n--- a/b.cc\n+++ b/b.cc\n@@ -2,8 +2,8 @@ function\n same\n+added\n"
        fixed = extract_unified_diff(model_diff)
        self.assertIn("@@ -1,1 +1,1 @@", fixed)
        self.assertIn("@@ -2,1 +2,2 @@ function", fixed)
        self.assertIn("\\ No newline at end of file", fixed)

    def generate(self, generator):
        return generator.generate_patch("example.cc", "return true;\n", {}, [], fault_diagnosis="")

    def test_required_mode_skips_the_compiler_and_calls_the_selected_model(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text=PATCH)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-key"}), patch("openai.OpenAI", return_value=client), \
                patch("codegraphhunter.repair.llm_patch_generator._generate_patch_from_required_sketch") as compiler:
            result = self.generate(LLMPatchGenerator(model="gpt-4o", require_llm=True))
        compiler.assert_not_called()
        client.responses.create.assert_called_once()
        self.assertEqual(client.responses.create.call_args.kwargs["model"], "gpt-4o")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "GENERATED")
        self.assertEqual(result["model"], "gpt-4o")

    def test_api_failure_does_not_fall_back_to_a_compiled_patch(self):
        client = Mock()
        client.responses.create.side_effect = RuntimeError("Simulated API failure")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-key"}), patch("openai.OpenAI", return_value=client), \
                patch("codegraphhunter.repair.llm_patch_generator._generate_patch_from_required_sketch") as compiler:
            result = self.generate(LLMPatchGenerator(require_llm=True))
        compiler.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("Simulated API failure", result["error"])

    def test_legacy_default_still_compiles_required_sketches(self):
        with patch("codegraphhunter.repair.llm_patch_generator._generate_patch_from_required_sketch",
                   return_value={"ok": True, "patch": PATCH, "model": "structured-edit-compiler"}), \
                patch("openai.OpenAI") as client:
            result = self.generate(LLMPatchGenerator())
        self.assertEqual(result["model"], "structured-edit-compiler")
        client.assert_not_called()


class AprRunnerTests(unittest.TestCase):
    def test_end_to_end_artifacts_require_recorded_calls_and_keep_source_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = root / "case with spaces"
            (case / ".codegraphhunter").mkdir(parents=True)
            (case / "httplib.h").write_text("original source")
            (case / ".codegraphhunter/problem_statement.txt").write_text("fixture bug")

            def controller(command, *, cwd, env, stdout, stderr, timeout, check):
                out = Path(env["CGH_APR_LLM_AUDIT"]).parent
                patch_file = out / "workdirs/patched/codegraphhunter_patch.diff"
                patch_file.parent.mkdir(parents=True)
                patch_file.write_text(PATCH)
                (out / "debug/attempt_001").mkdir()
                (out / "debug/attempt_001/global_summary.json").write_text(json.dumps({"stop_reason": "OBJECTIVE_MET"}))
                (out / "debug/fault_diagnosis.txt").write_text("fixture diagnosis")
                (out / "debug/graph_fault_localization.json").write_text(json.dumps({"selected_location": {
                    "function": "is_valid_path", "source_file": "httplib.h", "start_line": 20,
                    "primary_arg_node_id": 99131, "primary_icfg_node_id": 83372}}))
                events = []
                for stage in ("information_planner", "local_summary", "patch"):
                    events.extend([{"stage": stage, "status": "started"}, {"stage": stage, "status": "completed", "response_id": "resp_fixture"}])
                Path(env["CGH_APR_LLM_AUDIT"]).write_text("\n".join(json.dumps(event) for event in events))
                stdout.write("=== PATCH ATTEMPT RESULT ===\n" + json.dumps({"ok": True,
                    "generation": {"model": "gpt-4o", "status": "GENERATED"},
                    "validation": {"status": "PASSED", "patch_file": str(patch_file), "workdir": str(patch_file.parent)}}))
                stdout.flush()
                return SimpleNamespace(returncode=0)

            with patch("codegraphhunter.web.apr_runner.subprocess.run", side_effect=controller):
                status = run({"CGH_APR_MODEL": "gpt-4o", "CGH_DEMO_CASE": str(case), "CGH_DEMO_OUT_ROOT": str(root / "output")})
            result = json.loads((root / "output/latest/frontend_result.json").read_text())
            self.assertEqual(status, 0)
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(result["llm"]["status"], "COMPLETED")
            self.assertEqual(result["llm"]["completed_calls"], 3)
            self.assertEqual(result["localized_function"], "is_valid_path")
            self.assertEqual(result["graph_ids"]["primary_arg_node_id"], 99131)
            self.assertEqual(result["graph_ids"]["primary_icfg_node_id"], 83372)
            self.assertEqual(result["patch"]["status"], "GENERATED")
            self.assertTrue(Path(result["artifacts"]["llm_calls"]).exists())
            self.assertEqual((case / "httplib.h").read_text(), "original source")

    def test_real_apr_command_enables_all_model_stages_without_credentials_in_arguments(self):
        command = controller_command(Path("/tmp/case with spaces"), Path("/tmp/run with spaces"), "gpt-4o")
        for flag in ("--use-llm-info-planner", "--use-llm-summary", "--use-llm-patch", "--require-llm-patch"):
            self.assertIn(flag, command)
        for flag in ("--info-planner-model", "--llm-summary-model", "--patch-model"):
            self.assertEqual(command[command.index(flag) + 1], "gpt-4o")
        self.assertNotIn("--bolt-pass", command)
        self.assertIn("/tmp/case with spaces", command)

    def test_default_apr_runner_is_separate_from_the_original_demo_runner(self):
        backend = FrozenDemoBackend(environ={})
        self.assertNotEqual(backend.apr_runner_path, backend.runner_path)
        self.assertEqual(backend.apr_runner_path.name, "apr_runner.py")
        self.assertEqual(backend.configuration_defaults()["runtime"]["runner"], str(backend.apr_runner_path))

    def test_reports_only_actual_completed_calls_and_labels_failed_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "llm.jsonl"
            self.assertEqual(llm_report(audit, "gpt-4o")["status"], "NOT_CALLED")
            events = [{"stage": "patch", "status": "started"}, {"stage": "patch", "status": "completed", "response_id": "resp_test"},
                      {"stage": "information_planner", "status": "started"}, {"stage": "information_planner", "status": "failed"}]
            audit.write_text("\n".join(json.dumps(event) for event in events))
            report = llm_report(audit, "gpt-4o")
            self.assertEqual(report["attempted_calls"], 2)
            self.assertEqual(report["completed_calls"], 1)
            self.assertEqual(report["status"], "PARTIAL")
            self.assertEqual(report["stages"]["patch"]["completed"], 1)

    def test_validation_success_without_an_actual_model_call_cannot_pass_apr(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            result = {"ok": True, "generation": {"status": "GENERATED", "model": "gpt-4o"}, "validation": {"status": "PASSED"}}
            (out / "pipeline.log").write_text("=== PATCH ATTEMPT RESULT ===\n" + json.dumps(result))
            published = publish_result(out, "gpt-4o", 0)
            self.assertEqual(published["status"], "FAILED")
            self.assertEqual(published["llm"]["status"], "NOT_CALLED")
            self.assertEqual(published["patch"]["validation"], "PASSED")


if __name__ == "__main__":
    unittest.main()
