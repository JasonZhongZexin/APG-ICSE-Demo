"""Verify selected APR models reach SDK requests without a paid API call."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codegraphhunter.web.apr_models import model_environment


@unittest.skipUnless(importlib.util.find_spec("openai"), "OpenAI SDK is optional")
class AprModelCompatibilityTests(unittest.TestCase):
    def test_child_sdk_requests_use_selected_model_and_supported_parameters(self):
        script = """
import json
import httpx
from openai import OpenAI
captured = []
def handle(request):
    captured.append(json.loads(request.content))
    return httpx.Response(200, json={"id": "resp_test", "object": "response", "output": []})
client = OpenAI(api_key="fixture-key", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
client.responses.create(model="configured-model", input="test", temperature=0, max_output_tokens=700)
print(json.dumps(captured[0]))
"""
        parent = dict(os.environ)
        parent.pop("CGH_APR_MODEL", None)
        for model in (None, "gpt-4o", "gpt-6-luna", "gpt-5.4", "gpt-5.4-pro"):
            with self.subTest(model=model):
                environ = parent if model is None else model_environment(parent, model)
                result = subprocess.run([sys.executable, "-c", script], env=environ, text=True, capture_output=True, check=True)
                request = json.loads(result.stdout)
                self.assertEqual(request["model"], model or "configured-model")
                if model is None or model == "gpt-4o":
                    self.assertEqual(request["temperature"], 0)
                    self.assertEqual(request["max_output_tokens"], 700)
                    self.assertNotIn("reasoning", request)
                else:
                    self.assertNotIn("temperature", request)
                    self.assertGreaterEqual(request["max_output_tokens"], 8192)
                    if model.endswith("-pro"):
                        self.assertNotIn("reasoning", request)
                    else:
                        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertEqual(parent.get("CGH_APR_MODEL"), None)

    def test_audit_records_response_ids_usage_and_failures_without_secrets_or_prompts(self):
        script = '''
import json, os
from pathlib import Path
import httpx
from openai import OpenAI
def handle(request):
    if os.environ.get("SIMULATE_FAILURE"):
        return httpx.Response(401, json={"error":{"message":"private-api-key private-prompt", "type":"invalid_request_error"}}, headers={"x-request-id":"req_error"})
    return httpx.Response(200, json={"id":"resp_fixture", "object":"response", "status":"completed", "model":"gpt-4o-2024-08-06", "output":[], "usage":{"input_tokens":12,"output_tokens":4,"total_tokens":16}}, headers={"x-request-id":"req_fixture"})
client = OpenAI(api_key="private-api-key", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
try:
    client.responses.create(model="configured", input="private-prompt", temperature=0)
except Exception:
    pass
print(Path(os.environ["CGH_APR_LLM_AUDIT"]).read_text())
'''
        with tempfile.TemporaryDirectory() as directory:
            for failed in (False, True):
                audit = Path(directory) / ("failed.jsonl" if failed else "success.jsonl")
                env = model_environment(os.environ, "gpt-4o")
                env["CGH_APR_LLM_AUDIT"] = str(audit)
                env["SIMULATE_FAILURE"] = "1" if failed else ""
                subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True)
                data = audit.read_text()
                self.assertNotIn("private-api-key", data)
                self.assertNotIn("private-prompt", data)
                events = [json.loads(line) for line in data.splitlines()]
                self.assertEqual(events[0]["status"], "started")
                self.assertEqual(events[1]["requested_model"], "gpt-4o")
                if failed:
                    self.assertEqual(events[1]["status"], "failed")
                    self.assertEqual(events[1]["http_status"], 401)
                    self.assertEqual(events[1]["request_id"], "req_error")
                else:
                    self.assertEqual(events[1]["status"], "completed")
                    self.assertEqual(events[1]["request_id"], "req_fixture")
                    self.assertEqual(events[1]["response_id"], "resp_fixture")
                    self.assertEqual(events[1]["input_tokens"], 12)


if __name__ == "__main__":
    unittest.main()
