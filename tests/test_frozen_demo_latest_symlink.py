import json
import tempfile
import unittest
from pathlib import Path

from codegraphhunter.web.frozen_demo_backend import FrozenDemoBackend


class FrozenDemoLatestSymlinkTests(unittest.TestCase):
    def test_runner_can_replace_latest_symlink_after_backend_is_initialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend_root = root / "frontend"
            first_run = frontend_root / "run-1"
            second_run = frontend_root / "run-2"
            first_run.mkdir(parents=True)
            second_run.mkdir()
            (first_run / "frontend_result.json").write_text(
                json.dumps({"run_id": "run-1", "status": "PASSED"}),
                encoding="utf-8",
            )
            latest = frontend_root / "latest"
            try:
                latest.symlink_to(first_run, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are not supported in this test environment")

            runner = root / "codegraphhunter-demo-v1.2.2"
            new_result = json.dumps({"run_id": "run-2", "status": "PASSED"})
            runner.write_text(
                "#!/bin/sh\n"
                f"/bin/rm {latest!s}\n"
                f"/bin/ln -s {second_run!s} {latest!s}\n"
                f"printf '%s\\n' {new_result!r} > {latest / 'frontend_result.json'}\n",
                encoding="utf-8",
            )
            runner.chmod(0o755)
            backend = FrozenDemoBackend(
                environ={"CODEGRAPHHUNTER_FROZEN_DEMO_TIMEOUT_S": "10"},
                runner_path=runner,
                result_path=latest / "frontend_result.json",
                artifact_roots=(frontend_root,),
            )

            payload = backend.run()

            self.assertEqual(payload["result"]["run_id"], "run-2")
            self.assertEqual(backend.result_path, latest.absolute() / "frontend_result.json")


if __name__ == "__main__":
    unittest.main()
