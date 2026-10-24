import json
import tempfile
import unittest
from pathlib import Path

from codegraphhunter.web.frozen_demo_backend import FrozenDemoBackend, FrozenDemoError


class FrozenDemoBackendTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.allowed = self.root / "frontend"
        self.latest_dir = self.allowed / "latest"
        self.latest_dir.mkdir(parents=True)
        self.result_path = self.latest_dir / "frontend_result.json"
        self.runner = self.root / "codegraphhunter-demo-v1.2.2"

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_runner(self, body):
        self.runner.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
        self.runner.chmod(0o755)

    def _backend(self):
        return FrozenDemoBackend(
            environ={"CODEGRAPHHUNTER_FROZEN_DEMO_TIMEOUT_S": "10"},
            runner_path=self.runner,
            result_path=self.result_path,
            artifact_roots=(self.allowed,),
        )

    def test_run_executes_only_configured_runner_and_returns_fresh_result(self):
        result_json = json.dumps(
            {
                "run_id": "20260828_050000",
                "demo_version": "v1.2.2",
                "status": "PASSED",
                "localized_function": "is_valid_path",
            }
        )
        self._write_runner(
            "printf '%s\\n' " + repr(result_json) + " > " + repr(str(self.result_path)) + "\n"
            "printf 'frozen runner completed\\n'\n"
        )

        payload = self._backend().run()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["demo_version"], "v1.2.2")
        self.assertEqual(payload["result"]["localized_function"], "is_valid_path")
        self.assertEqual(payload["execution"]["runner"], str(self.runner.resolve()))
        self.assertEqual(payload["execution"]["return_code"], 0)
        self.assertIn("frozen runner completed", payload["execution"]["output_tail"])

    def test_nonzero_runner_result_is_still_returned_when_json_is_fresh(self):
        result_json = json.dumps({"status": "FAILED", "demo_version": "v1.2.2"})
        self._write_runner(
            "printf '%s\\n' " + repr(result_json) + " > " + repr(str(self.result_path)) + "\n"
            "exit 7\n"
        )

        payload = self._backend().run()

        self.assertEqual(payload["result"]["status"], "FAILED")
        self.assertEqual(payload["execution"]["return_code"], 7)

    def test_missing_result_reports_last_eighty_pipeline_lines(self):
        pipeline_log = self.latest_dir / "pipeline.log"
        pipeline_log.write_text(
            "\n".join(f"pipeline line {number}" for number in range(1, 101)),
            encoding="utf-8",
        )
        self._write_runner("printf 'TuGraph Bolt connection failed\\n'\nexit 9\n")

        with self.assertRaises(FrozenDemoError) as caught:
            self._backend().run()

        self.assertEqual(caught.exception.code, "DEMO_RESULT_MISSING")
        self.assertEqual(caught.exception.details["runner_return_code"], 9)
        self.assertIn("pipeline line 100", caught.exception.details["log_tail"])
        self.assertNotIn("pipeline line 1\n", caught.exception.details["log_tail"])

    def test_stale_previous_result_is_not_reported_as_new_run(self):
        self.result_path.write_text(
            json.dumps({"run_id": "old", "status": "PASSED"}), encoding="utf-8"
        )
        self._write_runner("printf 'failed before writing result\\n'\nexit 0\n")

        with self.assertRaises(FrozenDemoError) as caught:
            self._backend().run()

        self.assertEqual(caught.exception.code, "DEMO_RESULT_STALE")

    def test_latest_reads_result_without_running_executable(self):
        self.result_path.write_text(
            json.dumps({"run_id": "latest", "status": "PASSED"}), encoding="utf-8"
        )

        payload = self._backend().latest()

        self.assertEqual(payload["result"]["run_id"], "latest")

    def test_artifact_reader_allows_text_inside_root_and_rejects_outside(self):
        diagnosis = self.allowed / "run-1" / "fault_diagnosis.txt"
        diagnosis.parent.mkdir()
        diagnosis.write_text("### Fault Diagnosis\nEmbedded NUL is not rejected.\n", encoding="utf-8")
        outside = self.root / "secret.txt"
        outside.write_text("not allowed", encoding="utf-8")
        backend = self._backend()

        artifact = backend.read_artifact(str(diagnosis))

        self.assertIn("Embedded NUL", artifact["content"])
        with self.assertRaises(FrozenDemoError) as caught:
            backend.read_artifact(str(outside))
        self.assertEqual(caught.exception.code, "ARTIFACT_PATH_REJECTED")

    def test_symlink_cannot_escape_allowlisted_root(self):
        outside = self.root / "outside.log"
        outside.write_text("private", encoding="utf-8")
        link = self.allowed / "linked.log"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks are not supported in this test environment")

        with self.assertRaises(FrozenDemoError) as caught:
            self._backend().read_artifact(str(link))

        self.assertEqual(caught.exception.code, "ARTIFACT_PATH_REJECTED")


if __name__ == "__main__":
    unittest.main()
