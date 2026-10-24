import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codegraphhunter.datasource.bolt_client import BoltClient
from codegraphhunter.web.database_client import DeadlineBoltClient, DatabaseQueryTimeout


class FakeWorkerClient(DeadlineBoltClient):
    script = "import time; time.sleep(30)"

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        self._process = subprocess.Popen([sys.executable, "-u", "-c", self.script], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.started_process = self._process


class DatabaseClientTests(unittest.TestCase):
    def test_bolt_timeout_is_transaction_metadata_and_preserves_query_parameters(self):
        with patch("codegraphhunter.datasource.bolt_client.GraphDatabase.driver") as factory:
            session = factory.return_value.session.return_value.__enter__.return_value
            session.run.return_value.data.return_value = [{"id": 7}]
            session.run.return_value.consume.return_value = SimpleNamespace(counters=None)
            client = BoltClient("bolt://test", "user", "secret")
            result = client.run("MATCH (n) RETURN n.id", {"id": 7, "timeout": 999}, timeout_s=0.5)
            query, parameters = session.run.call_args.args
            self.assertEqual(query.timeout, 0.5)
            self.assertEqual(str(query), "MATCH (n) RETURN n.id")
            self.assertEqual(parameters, {"id": 7, "timeout": 999})
            self.assertEqual(session.run.call_args.kwargs, {})
            self.assertEqual(result["records"], [{"id": 7}])

    def test_unresponsive_worker_is_stopped_at_client_deadline(self):
        client = FakeWorkerClient("bolt://test", "user", "secret")
        started = time.monotonic()
        with self.assertRaises(DatabaseQueryTimeout):
            client.run("MATCH (n) RETURN n.id", timeout_s=0.1)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(client.started_process.poll())
        self.assertIsNone(client._process)
        client.close()

    def test_successful_worker_is_reused_and_closed(self):
        client = FakeWorkerClient("bolt://test", "user", "secret")
        client.script = "import sys,json\nfor line in sys.stdin:\n p=json.loads(line); print(json.dumps({'ok':True,'result':{'records':[p['params']]}}),flush=True)"
        try:
            self.assertEqual(client.run("query", {"id": 1})["records"], [{"id": 1}])
            first = client.started_process
            self.assertEqual(client.run("query", {"id": 2})["records"], [{"id": 2}])
            self.assertIs(first, client.started_process)
        finally:
            client.close()
        self.assertIsNotNone(first.poll())

    def test_partial_worker_response_still_obeys_deadline(self):
        client = FakeWorkerClient("bolt://test", "user", "secret")
        client.script = "import sys,time; sys.stdout.write('{\"ok\":'); sys.stdout.flush(); time.sleep(30)"
        with self.assertRaises(DatabaseQueryTimeout):
            client.run("query", timeout_s=0.1)
        self.assertIsNotNone(client.started_process.poll())
        client.close()
