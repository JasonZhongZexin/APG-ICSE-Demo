import json
import io
import os
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

from codegraphhunter.web import app
from codegraphhunter.web.apr_models import GPT_MODELS, MODEL_VARIABLES
from codegraphhunter.web.frozen_demo_backend import FrozenDemoBackend, FrozenDemoError
from codegraphhunter.web.icse_demo_settings import IcseDemoSettings


class IcseDemoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.result = self.root / "frontend_result.json"
        self.snapshot = self.root / "child_environment.json"
        self.log = self.root / "pipeline.log"
        self.runner = self.root / "runner"
        self.environ = {
            "OPENAI_API_KEY": "server-api-key-for-test",
            "TUGRAPH_BOLT_URI": "bolt://server.example:7687",
            "TUGRAPH_BOLT_USER": "server-user",
            "TUGRAPH_BOLT_PASS": "server-password-for-test",
            "TEST_RESULT": str(self.result),
            "TEST_SNAPSHOT": str(self.snapshot),
            "TEST_LOG": str(self.log),
            "CODEGRAPHHUNTER_FROZEN_DEMO_TIMEOUT_S": "10",
        }
        self.configuration = {
            "api_key": "client-api-key-for-test",
            "graphdb_uri": "bolt://client.example:17687",
            "graphdb_user": "client-user",
            "graphdb_password": "client-password-for-test",
        }
        self.runner.write_text(
            f"#!{sys.executable}\n"
            "import json, os, time\n"
            "from pathlib import Path\n"
            "from urllib.parse import quote\n"
            "snapshot = {key: os.environ[key] for key in ('OPENAI_API_KEY', 'TUGRAPH_BOLT_URI', 'TUGRAPH_BOLT_USER', 'TUGRAPH_BOLT_PASS')}\n"
            "Path(os.environ['TEST_SNAPSHOT']).write_text(json.dumps(snapshot))\n"
            "echo = snapshot['OPENAI_API_KEY'] + ' ' + snapshot['TUGRAPH_BOLT_PASS']\n"
            "Path(os.environ['TEST_LOG']).write_text(echo + ' ' + quote(snapshot['TUGRAPH_BOLT_PASS'], safe=''))\n"
            "Path(os.environ['TEST_RESULT']).write_text(json.dumps({'run_id': str(time.time_ns()), 'status': 'PASSED', 'credential_echo': echo}))\n"
            "print(echo)\n",
            encoding="utf-8",
        )
        self.runner.chmod(0o755)
        self.backend = FrozenDemoBackend(
            environ=self.environ,
            runner_path=self.runner,
            apr_runner_path=self.runner,
            result_path=self.result,
            artifact_roots=(self.root,),
        )
        self.settings = IcseDemoSettings()
        self.store_patch = patch.object(app, "_ICSE_DEMO_SETTINGS", self.settings)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def tearDown(self):
        self.directory.cleanup()

    def test_submitted_credentials_reach_child_without_changing_server_configuration(self):
        before = os.environ.copy()
        payload = self.backend.run(configuration=self.configuration)
        actual = json.loads(self.snapshot.read_text())
        self.assertEqual(actual["OPENAI_API_KEY"], self.configuration["api_key"])
        self.assertEqual(actual["TUGRAPH_BOLT_URI"], self.configuration["graphdb_uri"])
        self.assertEqual(actual["TUGRAPH_BOLT_USER"], self.configuration["graphdb_user"])
        self.assertEqual(actual["TUGRAPH_BOLT_PASS"], self.configuration["graphdb_password"])
        self.assertTrue(payload["ok"])
        self.assertEqual(os.environ, before)
        self.assertEqual(self.environ["OPENAI_API_KEY"], "server-api-key-for-test")

        self.backend.run()
        original = json.loads(self.snapshot.read_text())
        self.assertEqual(original["OPENAI_API_KEY"], self.environ["OPENAI_API_KEY"])
        self.assertEqual(original["TUGRAPH_BOLT_PASS"], self.environ["TUGRAPH_BOLT_PASS"])
        self.assertEqual(original["TUGRAPH_BOLT_URI"], self.environ["TUGRAPH_BOLT_URI"])

    def test_empty_fields_reuse_server_secrets_and_defaults(self):
        self.backend.run(configuration={key: "" for key in self.configuration})
        actual = json.loads(self.snapshot.read_text())
        for key in actual:
            self.assertEqual(actual[key], self.environ[key])

    def test_two_custom_runs_do_not_reuse_previous_overrides(self):
        self.backend.run(configuration=self.configuration)
        self.backend.run(configuration={"api_key": "second-api-key-for-test"})
        actual = json.loads(self.snapshot.read_text())
        self.assertEqual(actual["OPENAI_API_KEY"], "second-api-key-for-test")
        self.assertEqual(actual["TUGRAPH_BOLT_PASS"], self.environ["TUGRAPH_BOLT_PASS"])
        self.assertEqual(actual["TUGRAPH_BOLT_USER"], self.environ["TUGRAPH_BOLT_USER"])

    def test_selected_model_overrides_all_stages_for_one_run_only(self):
        model_snapshot = self.root / "model_environment.json"
        self.environ["TEST_MODEL_SNAPSHOT"] = str(model_snapshot)
        for variable in MODEL_VARIABLES:
            self.environ[variable] = "configured-model-for-test"
        self.runner.write_text(self.runner.read_text() +
            "Path(os.environ['TEST_MODEL_SNAPSHOT']).write_text(json.dumps({key: os.environ.get(key) for key in " + repr(MODEL_VARIABLES) + "}))\n")
        before = os.environ.copy()
        for model in ("gpt-6-luna", "gpt-4o"):
            payload = self.backend.run(configuration=self.configuration, model=model)
            self.assertEqual(payload["execution"]["model"], model)
            self.assertEqual(json.loads(model_snapshot.read_text()), dict.fromkeys(MODEL_VARIABLES, model))
        self.backend.run()
        self.assertEqual(json.loads(model_snapshot.read_text()), dict.fromkeys(MODEL_VARIABLES, "configured-model-for-test"))
        self.assertEqual(os.environ, before)

    def test_invalid_model_is_rejected_before_starting_runner(self):
        with patch("codegraphhunter.web.frozen_demo_backend.subprocess.run") as run:
            for model in ("", "arbitrary-model", "gpt-4.1-nano", 42, []):
                with self.subTest(model=model), self.assertRaises(FrozenDemoError) as caught:
                    self.backend.run(configuration=self.configuration, model=model)
                self.assertEqual(caught.exception.code, "INVALID_MODEL")
            run.assert_not_called()

    def test_password_spaces_and_special_characters_are_preserved(self):
        password = '  quote" spaces $`(test)&  '
        self.backend.run(configuration=dict(self.configuration, graphdb_password=password))
        self.assertEqual(json.loads(self.snapshot.read_text())["TUGRAPH_BOLT_PASS"], password)

    def test_secrets_are_redacted_in_result_output_latest_and_artifacts(self):
        configuration = dict(self.configuration, graphdb_password='credential/with"special&chars')
        payload = self.backend.run(configuration=configuration)
        for exposed in (payload, self.backend.latest(), self.backend.read_artifact(str(self.log))):
            serialized = json.dumps(exposed)
            self.assertNotIn(configuration["api_key"], serialized)
            self.assertNotIn(configuration["graphdb_password"], serialized)
            self.assertNotIn(quote(configuration["graphdb_password"], safe=""), serialized)
            self.assertIn("[REDACTED]", serialized)

    def test_failure_log_is_redacted(self):
        self.runner.write_text(
            f"#!{sys.executable}\nimport os\nprint(os.environ['OPENAI_API_KEY'], os.environ['TUGRAPH_BOLT_PASS'])\nraise SystemExit(9)\n",
            encoding="utf-8",
        )
        with self.assertRaises(FrozenDemoError) as caught:
            self.backend.run(configuration=self.configuration)
        self.assertEqual(caught.exception.code, "DEMO_RESULT_MISSING")
        serialized = json.dumps(caught.exception.as_payload())
        self.assertNotIn(self.configuration["api_key"], serialized)
        self.assertNotIn(self.configuration["graphdb_password"], serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_configuration_defaults_expose_presence_without_secret_values(self):
        self.environ["TUGRAPH_BOLT_URI"] = "bolt://user:embedded-password@host.example:7687"
        defaults = self.backend.configuration_defaults()["configuration"]
        self.assertEqual(defaults["graphdb_uri"], "bolt://host.example:7687")
        self.assertTrue(defaults["api_key_configured"])
        self.assertTrue(defaults["graphdb_password_configured"])
        serialized = json.dumps(defaults)
        for secret in ("embedded-password", self.environ["OPENAI_API_KEY"], self.environ["TUGRAPH_BOLT_PASS"]):
            self.assertNotIn(secret, serialized)

    def test_invalid_configuration_is_rejected_before_starting_runner(self):
        invalid = [[], "key", {"OPENAI_API_KEY": "injected"}, {"runner_path": "/bin/sh"},
                   {"api_key": 10}, {"api_key": "key\nvalue"}, {"graphdb_password": None},
                   {"graphdb_uri": "http://host:7687"}, {"graphdb_uri": "bolt://user:secret@host:7687"},
                   {"graphdb_uri": "bolt://host:99999"}, {"graphdb_uri": "bolt://host/path"},
                   {"graphdb_uri": "bolt://host?password=value"}, {"graphdb_user": "x" * 257}]
        with patch("codegraphhunter.web.frozen_demo_backend.subprocess.run") as run:
            for configuration in invalid:
                with self.subTest(configuration=configuration), self.assertRaises(FrozenDemoError):
                    self.backend.run(configuration=configuration)
            run.assert_not_called()

    def test_missing_server_and_submitted_credentials_are_rejected(self):
        for variable in ("OPENAI_API_KEY", "TUGRAPH_BOLT_PASS"):
            saved = self.environ.pop(variable)
            with self.subTest(variable=variable), self.assertRaises(FrozenDemoError):
                self.backend.run(configuration={})
            self.environ[variable] = saved

    def test_original_and_configured_runs_share_concurrency_lock(self):
        self.backend._run_lock.acquire()
        try:
            for configuration in (None, self.configuration):
                with self.assertRaises(FrozenDemoError) as caught:
                    self.backend.run(configuration=configuration)
                self.assertEqual(caught.exception.http_status, 409)
        finally:
            self.backend._run_lock.release()

    def _handler(self, path, data=None, cookie=""):
        handler = object.__new__(app.RequestHandler)
        handler.path = path
        handler.headers = {"Cookie": cookie}
        handler._json = Mock()
        handler._read_json = Mock(return_value=data)
        handler._serve_static = Mock()
        return handler

    def _save_settings(self, configuration=None, cookie=""):
        handler = self._handler(
            "/api/codegraphhunter/icse-demo/config",
            {"configuration": self.configuration if configuration is None else configuration},
            cookie,
        )
        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend):
            handler.do_POST()
        return handler

    @staticmethod
    def _response_cookie(handler):
        return handler._json.call_args.kwargs["headers"]["Set-Cookie"].split(";", 1)[0]

    def test_icse_page_and_config_routes_use_existing_demo_and_safe_defaults(self):
        for path in ("/icse-demo", "/icse-demo/"):
            handler = self._handler(path)
            handler.do_GET()
            handler._serve_static.assert_called_once_with("icse-demo.html")
        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend):
            handler = self._handler("/api/codegraphhunter/icse-demo/config")
            handler.do_GET()
        status, payload = handler._json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(payload["configuration"]["api_key_configured"])
        self.assertNotIn(self.environ["OPENAI_API_KEY"], json.dumps(payload))

    def test_icse_run_route_executes_child_with_submitted_configuration(self):
        handler = self._handler("/api/codegraphhunter/icse-demo/run", {"configuration": self.configuration})
        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend):
            handler.do_POST()
        status, payload = handler._json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(json.loads(self.snapshot.read_text())["OPENAI_API_KEY"], self.configuration["api_key"])

    def test_model_routes_default_to_4o_and_forward_explicit_selection(self):
        handler = self._handler("/api/codegraphhunter/icse-demo/models")
        handler.do_GET()
        status, catalog = handler._json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(catalog["default_model"], "gpt-4o")
        identifiers = [model["id"] for model in catalog["models"]]
        self.assertIn("gpt-4o", identifiers)
        self.assertIn("gpt-6-luna", identifiers)
        self.assertNotIn("gpt-4.1-nano", identifiers)
        for body, expected in (({}, "gpt-4o"), ({"model": "gpt-6-luna"}, "gpt-6-luna")):
            handler = self._handler("/api/codegraphhunter/icse-demo/run", body)
            with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(self.backend, "run", return_value={"ok": True}) as run:
                handler.do_POST()
            self.assertEqual(handler._json.call_args.args[0], 200)
            run.assert_called_once_with(configuration={}, model=expected)
        for model in (None, {}, 42, "unknown-model"):
            handler = self._handler("/api/codegraphhunter/icse-demo/run", {"model": model})
            with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend):
                handler.do_POST()
            self.assertEqual(handler._json.call_args.args[0], 400)
        self.assertFalse(self.snapshot.exists())

    def test_icse_route_rejects_null_config_and_unknown_top_level_fields(self):
        for data in ({"configuration": None}, {"configuration": {}, "environ": {}}, {"configuration": []}):
            with self.subTest(data=data):
                handler = self._handler("/api/codegraphhunter/icse-demo/run", data)
                with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend):
                    handler.do_POST()
                self.assertEqual(handler._json.call_args.args[0], 400)
        self.assertFalse(self.snapshot.exists())

    def test_settings_page_is_separate_and_demo_has_no_credential_form(self):
        for path in ("/icse-demo/settings", "/icse-demo/settings/"):
            handler = self._handler(path)
            handler.do_GET()
            handler._serve_static.assert_called_once_with("icse-settings.html")
        static = Path(app.STATIC_DIR)
        self.assertNotIn('id="experimentApiKey"', (static / "demo-v122.html").read_text())
        self.assertIn('href="/icse-demo/settings"', (static / "demo-v122.html").read_text())

    def test_saved_settings_survive_navigation_and_are_used_without_resubmitting_secrets(self):
        saved = self._save_settings()
        self.assertEqual(saved._json.call_args.args[0], 200)
        cookie = self._response_cookie(saved)
        attributes = saved._json.call_args.kwargs["headers"]["Set-Cookie"]
        self.assertIn("HttpOnly", attributes)
        self.assertIn("SameSite=Strict", attributes)
        self.assertNotIn(self.configuration["api_key"], cookie)

        handler = self._handler("/api/codegraphhunter/icse-demo/config", cookie=cookie)
        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend):
            handler.do_GET()
            payload = handler._json.call_args.args[1]
            self.assertTrue(payload["settings_saved"])
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["configuration"]["graphdb_uri"], self.configuration["graphdb_uri"])
            self.assertNotIn(self.configuration["api_key"], json.dumps(payload))
            self.assertNotIn(self.configuration["graphdb_password"], json.dumps(payload))

            handler = self._handler("/api/codegraphhunter/icse-demo/run", {}, cookie)
            handler.do_POST()
        self.assertEqual(handler._json.call_args.args[0], 200)
        self.assertEqual(json.loads(self.snapshot.read_text())["OPENAI_API_KEY"], self.configuration["api_key"])

    def test_empty_secrets_keep_saved_values_and_invalid_save_keeps_previous_settings(self):
        cookie = self._response_cookie(self._save_settings())
        updated = self._save_settings({"api_key": "", "graphdb_password": "", "graphdb_user": "edited-user"}, cookie)
        self.assertEqual(updated._json.call_args.args[0], 200)
        invalid = self._save_settings({"graphdb_uri": "https://invalid.example"}, cookie)
        self.assertEqual(invalid._json.call_args.args[0], 400)
        run = self._handler("/api/codegraphhunter/icse-demo/run", {}, cookie)
        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend):
            run.do_POST()
        actual = json.loads(self.snapshot.read_text())
        self.assertEqual(actual["OPENAI_API_KEY"], self.configuration["api_key"])
        self.assertEqual(actual["TUGRAPH_BOLT_PASS"], self.configuration["graphdb_password"])
        self.assertEqual(actual["TUGRAPH_BOLT_USER"], "edited-user")
        self.assertEqual(actual["TUGRAPH_BOLT_URI"], self.configuration["graphdb_uri"])

    def test_saved_settings_are_isolated_between_browsers_and_original_demo(self):
        cookie = self._response_cookie(self._save_settings())
        other = self._handler("/api/codegraphhunter/icse-demo/config")
        original = self._handler("/api/codegraphhunter/demo/run", cookie=cookie)
        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend):
            other.do_GET()
            self.assertFalse(other._json.call_args.args[1]["settings_saved"])
            self.assertEqual(other._json.call_args.args[1]["configuration"]["graphdb_user"], "server-user")
            original.do_POST()
        self.assertEqual(json.loads(self.snapshot.read_text())["OPENAI_API_KEY"], self.environ["OPENAI_API_KEY"])

    def test_icse_queries_use_saved_credentials_and_close_per_request_clients(self):
        cookie = self._response_cookie(self._save_settings())
        for graph, backend_name in (("icfg", "ICFGQueryBackend"), ("pag", "PAGQueryBackend"), ("callgraph", "CallGraphQueryBackend")):
            with self.subTest(graph=graph):
                handler = self._handler(f"/api/codegraphhunter/icse-demo/query/{graph}", {"query": "Find evidence"}, cookie)
                with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, backend_name) as constructor:
                    constructor.return_value.execute.return_value = {"ok": True, "records": []}
                    handler.do_POST()
                self.assertEqual(handler._json.call_args.args[0], 200)
                environ = constructor.call_args.kwargs["environ"]
                self.assertEqual(environ["OPENAI_API_KEY"], self.configuration["api_key"])
                self.assertEqual(environ["TUGRAPH_BOLT_URI"], self.configuration["graphdb_uri"])
                self.assertEqual(environ["TUGRAPH_BOLT_PASS"], self.configuration["graphdb_password"])
                self.assertEqual(environ["CODEGRAPHHUNTER_LLM_MODEL"], "gpt-4o")
                constructor.return_value.execute.assert_called_once_with("Find evidence")
                constructor.return_value.close.assert_called_once_with()

    def test_query_models_are_request_scoped_and_invalid_models_never_reach_clients(self):
        before = self.environ.copy()
        for graph, backend_name in (("icfg", "ICFGQueryBackend"), ("pag", "PAGQueryBackend"), ("callgraph", "CallGraphQueryBackend")):
            for model in ("gpt-6-luna", "gpt-4o", "gpt-5.4"):
                handler = self._handler(f"/api/codegraphhunter/icse-demo/query/{graph}", {"query": "Find evidence", "model": model})
                with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, backend_name) as constructor:
                    constructor.return_value.execute.return_value = {"ok": True}
                    handler.do_POST()
                self.assertEqual(handler._json.call_args.args[0], 200)
                self.assertEqual(constructor.call_args.kwargs["environ"]["CODEGRAPHHUNTER_LLM_MODEL"], model)
                constructor.return_value.close.assert_called_once_with()
            for model in (None, [], 1, "unlisted-model"):
                handler = self._handler(f"/api/codegraphhunter/icse-demo/query/{graph}", {"query": "Find evidence", "model": model})
                with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, backend_name) as constructor:
                    handler.do_POST()
                self.assertEqual(handler._json.call_args.args[0], 400)
                self.assertEqual(handler._json.call_args.args[1]["code"], "INVALID_MODEL")
                constructor.assert_not_called()
        self.assertEqual(self.environ, before)

    def test_both_query_workspaces_forward_explicit_numeric_limits_for_all_graphs(self):
        for graph, backend_name in (("icfg", "ICFGQueryBackend"), ("pag", "PAGQueryBackend"), ("callgraph", "CallGraphQueryBackend")):
            for apr in (True, False):
                path = f"/api/codegraphhunter/icse-demo/query/{graph}" if apr else f"/api/query/{graph}"
                target = backend_name if apr else f"get_{graph}_query_backend"
                handler = self._handler(path, {"query": "Find evidence", "max_depth": 100, "max_entities": 77})
                with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, target) as factory:
                    factory.return_value.execute.return_value = {"ok": True}
                    handler.do_POST()
                self.assertEqual(handler._json.call_args.args[0], 200)
                factory.return_value.execute.assert_called_once_with("Find evidence", max_depth=100, max_entities=77)

    def test_both_query_workspaces_accept_blank_limits_for_all_graphs(self):
        for graph, backend_name in (("icfg", "ICFGQueryBackend"), ("pag", "PAGQueryBackend"), ("callgraph", "CallGraphQueryBackend")):
            for apr in (True, False):
                path = f"/api/codegraphhunter/icse-demo/query/{graph}" if apr else f"/api/query/{graph}"
                target = backend_name if apr else f"get_{graph}_query_backend"
                for options in ({}, {"max_depth": None}, {"max_entities": None}, {"max_depth": None, "max_entities": None}):
                    with self.subTest(graph=graph, apr=apr, options=options):
                        handler = self._handler(path, dict({"query": "Find evidence"}, **options))
                        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, target) as factory:
                            factory.return_value.execute.return_value = {"ok": True}
                            handler.do_POST()
                        self.assertEqual(handler._json.call_args.args[0], 200)
                        factory.return_value.execute.assert_called_once_with("Find evidence", **options)

    def test_graph_query_stream_delivers_validated_cypher_before_results(self):
        for graph, backend_name in (("icfg", "ICFGQueryBackend"), ("pag", "PAGQueryBackend"), ("callgraph", "CallGraphQueryBackend")):
            for apr in (True, False):
                path = f"/api/codegraphhunter/icse-demo/query/{graph}" if apr else f"/api/query/{graph}"
                target = backend_name if apr else f"get_{graph}_query_backend"
                handler = self._handler(path, {"query": "Find evidence", "max_depth": 40, "max_entities": 100})
                handler.headers["Accept"] = "application/x-ndjson"
                handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
                handler.wfile = io.BytesIO()

                def execute(question, on_progress, **options):
                    on_progress({"stage": "planning", "message": "Planning"})
                    on_progress({"stage": "database", "cypher": "MATCH (n) RETURN n LIMIT 100", "params": {"secret": self.environ["OPENAI_API_KEY"]}, "timeout_s": 30})
                    # Progress must already be flushed while the query is still in flight.
                    self.assertEqual(len(handler.wfile.getvalue().splitlines()), 2)
                    return {"ok": True, "records": []}

                with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, target) as factory:
                    factory.return_value.execute.side_effect = execute
                    handler.do_POST()
                frames = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
                self.assertEqual([frame["type"] for frame in frames], ["progress", "progress", "result"])
                self.assertTrue(frames[-1]["payload"]["ok"])
                self.assertNotIn(self.environ["OPENAI_API_KEY"], handler.wfile.getvalue().decode())
                handler._json.assert_not_called()
                if apr:
                    factory.return_value.close.assert_called_once_with()

    def test_graph_query_stream_returns_timeout_after_validated_progress(self):
        from codegraphhunter.web.callgraph_query_backend import CallGraphQueryError
        from codegraphhunter.web.icfg_query_backend import ICFGQueryError
        from codegraphhunter.web.pag_query_backend import PAGQueryError
        for graph, backend_name, error_type in (
            ("icfg", "ICFGQueryBackend", ICFGQueryError), ("pag", "PAGQueryBackend", PAGQueryError),
            ("callgraph", "CallGraphQueryBackend", CallGraphQueryError),
        ):
            for apr in (True, False):
                path = f"/api/codegraphhunter/icse-demo/query/{graph}" if apr else f"/api/query/{graph}"
                target = backend_name if apr else f"get_{graph}_query_backend"
                handler = self._handler(path, {"query": "Find evidence"})
                handler.headers["Accept"] = "application/x-ndjson"
                handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
                handler.wfile = io.BytesIO()

                def execute(question, on_progress, **options):
                    on_progress({"stage": "database", "cypher": "validated query", "timeout_s": 30})
                    raise error_type("DATABASE_QUERY_TIMEOUT", "Database query timed out.", 504)

                with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, target) as factory:
                    factory.return_value.execute.side_effect = execute
                    handler.do_POST()
                frames = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
                self.assertEqual(frames[0]["stage"], "database")
                self.assertEqual(frames[-1]["type"], "error")
                self.assertEqual(frames[-1]["payload"]["code"], "DATABASE_QUERY_TIMEOUT")
                if apr:
                    factory.return_value.close.assert_called_once_with()

    def test_query_routes_reject_invalid_limits_before_planning_for_all_graphs(self):
        for graph, backend_name in (("icfg", "ICFGQueryBackend"), ("pag", "PAGQueryBackend"), ("callgraph", "CallGraphQueryBackend")):
            for apr in (True, False):
                path = f"/api/codegraphhunter/icse-demo/query/{graph}" if apr else f"/api/query/{graph}"
                target = backend_name if apr else f"get_{graph}_query_backend"
                for field, value in (("max_depth", 0), ("max_depth", -1), ("max_depth", True), ("max_depth", "3"), ("max_depth", 2.5), ("max_entities", -1), ("max_entities", 1001), ("max_entities", "")):
                    with self.subTest(graph=graph, apr=apr, field=field, value=value):
                        handler = self._handler(path, {"query": "Find evidence", field: value})
                        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, target) as factory:
                            handler.do_POST()
                        self.assertEqual(handler._json.call_args.args[0], 400)
                        self.assertEqual(handler._json.call_args.args[1]["code"], "INVALID_REQUEST")
                        self.assertIn(field, handler._json.call_args.args[1]["error"])
                        factory.return_value.execute.assert_not_called()
                        if apr:
                            factory.assert_not_called()

    def test_model_options_are_delivered_with_pages_without_a_separate_api_request(self):
        class Options(HTMLParser):
            def __init__(self):
                super().__init__()
                self.models = []
                self.selected = []
                self.in_model_select = False

            def handle_starttag(self, tag, attrs):
                if tag == "select":
                    self.in_model_select = dict(attrs).get("id") in {"runModel", "queryModel"}
                if tag == "option" and self.in_model_select:
                    attrs = dict(attrs)
                    self.models.append(attrs["value"])
                    if "selected" in attrs:
                        self.selected.append(attrs["value"])

            def handle_endtag(self, tag):
                if tag == "select":
                    self.in_model_select = False

        for filename in ("icse-demo.html", "icse-query.html"):
            handler = self._handler("/icse-demo")
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.wfile = io.BytesIO()
            app.RequestHandler._serve_static(handler, filename)
            page = handler.wfile.getvalue()
            parser = Options()
            parser.feed(page.decode())
            self.assertEqual(parser.models, [model for model, group in GPT_MODELS if group == "GPT-4"] +
                             [model for model, group in GPT_MODELS if group != "GPT-4"])
            self.assertEqual(parser.selected, ["gpt-4o"])
            self.assertNotIn(b"APR_MODEL_OPTIONS", page)
            handler.send_header.assert_any_call("Content-Length", str(len(page)))

    def test_pag_original_route_and_apr_failure_cleanup(self):
        original = self._handler("/api/query/pag", {"query": "Find arguments."})
        with patch.object(app, "get_pag_query_backend") as backend:
            backend.return_value.execute.return_value = {"ok": True, "records": []}
            original.do_POST()
        self.assertEqual(original._json.call_args.args[0], 200)
        backend.return_value.execute.assert_called_once_with("Find arguments.")

        from codegraphhunter.web.pag_query_backend import PAGQueryError
        cookie = self._response_cookie(self._save_settings())
        handler = self._handler("/api/codegraphhunter/icse-demo/query/pag", {"query": "Find arguments.", "model": "gpt-4o"}, cookie)
        with patch.object(app, "_FROZEN_DEMO_BACKEND", self.backend), patch.object(app, "PAGQueryBackend") as backend:
            backend.return_value.execute.side_effect = PAGQueryError("DATABASE_QUERY_FAILED", "Query failed.", 502,
                {"reason": self.configuration["graphdb_password"]})
            handler.do_POST()
        self.assertEqual(handler._json.call_args.args[0], 502)
        self.assertNotIn(self.configuration["graphdb_password"], json.dumps(handler._json.call_args.args[1]))
        backend.return_value.close.assert_called_once_with()

    def test_icse_query_routes_and_text_match_original_pages(self):
        for graph in ("icfg", "pag", "callgraph"):
            for suffix in ("", "/"):
                handler = self._handler(f"/icse-demo/query/{graph}{suffix}")
                handler.do_GET()
                handler._serve_static.assert_called_once_with("icse-query.html")
            original = self._handler(f"/query/{graph}")
            original.do_GET()
            original._serve_static.assert_called_once_with("query.html")

        class PageText(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []

            def handle_data(self, data):
                if data.strip():
                    self.text.append(data.strip())

        original, icse = PageText(), PageText()
        # APR has its own header and navigation; retain the query workspace.
        original.feed((app.STATIC_DIR / "query.html").read_text().split('<section class="query-hero">', 1)[1])
        icse.feed((app.STATIC_DIR / "icse-query.html").read_text().split('<section class="query-hero">', 1)[1])
        self.assertEqual([text for text in icse.text if text not in ("GPT model", "gpt-4o (default)")], original.text)


class IcseSessionTests(unittest.TestCase):
    def test_unknown_and_expired_tokens_cannot_recover_saved_credentials(self):
        settings = IcseDemoSettings(ttl_s=10)
        with patch("codegraphhunter.web.icse_demo_settings.time.monotonic", return_value=10):
            token, _ = settings.resolve("untrusted-token")
            self.assertNotEqual(token, "untrusted-token")
            settings.save(token, {"api_key": "saved-test-key"})
            _, loaded = settings.resolve(token)
            self.assertEqual(loaded["api_key"], "saved-test-key")
            loaded["api_key"] = "modified-copy"
            self.assertEqual(settings.resolve(token)[1]["api_key"], "saved-test-key")
        with patch("codegraphhunter.web.icse_demo_settings.time.monotonic", return_value=21):
            replacement, loaded = settings.resolve(token)
        self.assertNotEqual(token, replacement)
        self.assertEqual(loaded, {})


if __name__ == "__main__":
    unittest.main()
