"""Stable black-box integration for the frozen CodeGraphHunter v1.2.2 demo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from codegraphhunter.web.apr_models import model_environment


DEFAULT_RUNNER_PATH = Path(
    "/home/zexin/expirement/ICSE2027/CodeGraphHunter_demo_v1_2_2/"
    "bin/codegraphhunter-demo-v1.2.2"
)
DEFAULT_RESULT_PATH = Path(
    "/tmp/codegraphhunter_frontend_demo/latest/frontend_result.json"
)
DEFAULT_ARTIFACT_ROOTS = (
    Path("/tmp/codegraphhunter_frontend_demo"),
    Path("/tmp/codegraphhunter_patch_demo"),
)
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_LOG_TAIL_LINES = 80
ALLOWED_ARTIFACT_SUFFIXES = {".diff", ".json", ".jsonl", ".log", ".md", ".patch", ".txt"}
DEMO_CONFIGURATION_FIELDS = {
    "api_key": ("OPENAI_API_KEY", 4096),
    "graphdb_uri": ("TUGRAPH_BOLT_URI", 2048),
    "graphdb_user": ("TUGRAPH_BOLT_USER", 256),
    "graphdb_password": ("TUGRAPH_BOLT_PASS", 4096),
}


class FrozenDemoError(RuntimeError):
    """Expected frozen-demo failure safe to expose through the local API."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 400,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = int(http_status)
        self.details = details or {}

    def as_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "error": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def _tail_text(text: str, line_count: int = MAX_LOG_TAIL_LINES) -> str:
    return "\n".join(str(text or "").splitlines()[-line_count:])


class FrozenDemoBackend:
    """Execute the immutable v1.2.2 runner and expose only allowlisted artifacts."""

    def __init__(
        self,
        environ: Optional[Mapping[str, str]] = None,
        *,
        runner_path: Optional[Path] = None,
        apr_runner_path: Optional[Path] = None,
        result_path: Optional[Path] = None,
        artifact_roots: Optional[Sequence[Path]] = None,
    ) -> None:
        self.environ: Mapping[str, str] = environ if environ is not None else os.environ
        self.runner_path = Path(
            runner_path
            or self.environ.get(
                "CODEGRAPHHUNTER_FROZEN_DEMO_RUNNER", str(DEFAULT_RUNNER_PATH)
            )
        ).expanduser().resolve()
        self.apr_runner_path = Path(apr_runner_path or self.environ.get(
            "CODEGRAPHHUNTER_APR_RUNNER", str(Path(__file__).with_name("apr_runner.py"))
        )).expanduser().resolve()
        self.result_path = Path(
            result_path
            or self.environ.get(
                "CODEGRAPHHUNTER_FROZEN_DEMO_RESULT", str(DEFAULT_RESULT_PATH)
            )
        ).expanduser().absolute()
        configured_roots: Iterable[Path] = artifact_roots or DEFAULT_ARTIFACT_ROOTS
        self.artifact_roots: Tuple[Path, ...] = tuple(
            Path(root).expanduser().resolve() for root in configured_roots
        )
        self.timeout_s = self._environment_int(
            "CODEGRAPHHUNTER_FROZEN_DEMO_TIMEOUT_S", 1800, 1, 7200
        )
        self._run_lock = threading.Lock()
        self._secret_lock = threading.Lock()
        self._known_secrets: set[str] = set()
        self._remember_secrets(self.environ)

    def configuration_defaults(self, configuration: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        """Expose editable connection defaults and credential presence, never values."""
        environ = dict(self.environ)
        for field, value in (configuration or {}).items():
            if field in DEMO_CONFIGURATION_FIELDS and value:
                environ[DEMO_CONFIGURATION_FIELDS[field][0]] = value
        uri = str(environ.get("TUGRAPH_BOLT_URI", "")).strip() or "bolt://localhost:7687"
        try:
            parts = urlsplit(uri)
            # A server URI may include credentials; never send userinfo to the UI.
            uri = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        except ValueError:
            uri = "bolt://localhost:7687"
        return {
            "ok": True,
            "runtime": {"demo_version": "APR GPT", "runner": str(self.apr_runner_path)},
            "configuration": {
                "graphdb_uri": uri,
                "graphdb_user": str(environ.get("TUGRAPH_BOLT_USER", "")).strip() or "admin",
                "api_key_configured": bool(str(environ.get("OPENAI_API_KEY", "")).strip()),
                "graphdb_password_configured": bool(environ.get("TUGRAPH_BOLT_PASS", "")),
            },
        }

    def prepare_configuration(self, configuration: Any) -> Dict[str, str]:
        """Validate settings and freeze the four effective values for a session."""
        environ = self.execution_environment(configuration)
        return {field: environ[variable] for field, (variable, _) in DEMO_CONFIGURATION_FIELDS.items()}

    def execution_environment(self, configuration: Any) -> Dict[str, str]:
        environ = self._configured_environment(configuration)
        self._remember_secrets(environ)
        return environ

    def _configured_environment(self, configuration: Any) -> Dict[str, str]:
        if not isinstance(configuration, dict) or set(configuration).difference(DEMO_CONFIGURATION_FIELDS):
            raise FrozenDemoError(
                "INVALID_CONFIGURATION", "Provide an experiment configuration with only API key and Graph DB fields."
            )
        environ = dict(self.environ)
        for field, (variable, limit) in DEMO_CONFIGURATION_FIELDS.items():
            value = configuration.get(field, "")
            if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise FrozenDemoError("INVALID_CONFIGURATION", f"Invalid value for {field}.")
            if field != "graphdb_password":
                value = value.strip()
            if value:
                environ[variable] = value
        uri = environ.get("TUGRAPH_BOLT_URI", "").strip() or "bolt://localhost:7687"
        user = environ.get("TUGRAPH_BOLT_USER", "").strip() or "admin"
        try:
            parts = urlsplit(uri)
            valid_uri = (
                parts.scheme in {"bolt", "bolt+s", "bolt+ssc"}
                and bool(parts.hostname)
                and (parts.port is None or 1 <= parts.port <= 65535)
                and parts.username is None and parts.password is None
                and parts.path in {"", "/"} and not parts.query and not parts.fragment
                and not any(char.isspace() for char in uri)
            )
        except ValueError:
            valid_uri = False
        if not valid_uri:
            raise FrozenDemoError(
                "INVALID_CONFIGURATION",
                "Use a Bolt URI such as bolt://localhost:7687, with username and password in their separate fields.",
            )
        if not str(environ.get("OPENAI_API_KEY", "")).strip():
            raise FrozenDemoError("INVALID_CONFIGURATION", "Enter an API key or configure OPENAI_API_KEY on the server.")
        if not environ.get("TUGRAPH_BOLT_PASS", ""):
            raise FrozenDemoError("INVALID_CONFIGURATION", "Enter a Graph DB password or configure TUGRAPH_BOLT_PASS on the server.")
        environ["TUGRAPH_BOLT_URI"] = uri
        environ["TUGRAPH_BOLT_USER"] = user
        return environ

    def _remember_secrets(self, environ: Mapping[str, str]) -> None:
        secrets = [str(environ.get(name, "")) for name in ("OPENAI_API_KEY", "TUGRAPH_BOLT_PASS")]
        try:
            secrets.append(urlsplit(str(environ.get("TUGRAPH_BOLT_URI", ""))).password or "")
        except ValueError:
            pass
        with self._secret_lock:
            for secret in filter(None, secrets):
                self._known_secrets.update((secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]))

    def _redact(self, value: Any) -> Any:
        with self._secret_lock:
            secrets = sorted(self._known_secrets, key=len, reverse=True)

        def clean(item: Any) -> Any:
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "[REDACTED]")
                return item
            if isinstance(item, dict):
                return {key: clean(content) for key, content in item.items()}
            if isinstance(item, list):
                return [clean(content) for content in item]
            return item

        return clean(value)

    def _environment_int(self, name: str, default: int, minimum: int, maximum: int) -> int:
        raw = str(self.environ.get(name, default)).strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise FrozenDemoError(
                "CONFIGURATION_ERROR",
                f"{name} must be an integer.",
                http_status=503,
            ) from exc
        if not minimum <= value <= maximum:
            raise FrozenDemoError(
                "CONFIGURATION_ERROR",
                f"{name} must be between {minimum} and {maximum}.",
                http_status=503,
            )
        return value

    @staticmethod
    def _file_signature(path: Path) -> Optional[Tuple[int, int]]:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _load_result(self) -> Dict[str, Any]:
        try:
            text = self.result_path.read_text(encoding="utf-8")
            value = json.loads(text)
        except FileNotFoundError as exc:
            raise FrozenDemoError(
                "DEMO_RESULT_MISSING",
                "Demo run failed before result JSON was generated.",
                http_status=502,
                details={"result_file": str(self.result_path)},
            ) from exc
        except (OSError, UnicodeError, ValueError) as exc:
            raise FrozenDemoError(
                "DEMO_RESULT_INVALID",
                "The frozen demo produced an unreadable frontend result JSON.",
                http_status=502,
                details={"result_file": str(self.result_path), "reason": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise FrozenDemoError(
                "DEMO_RESULT_INVALID",
                "The frontend result JSON must contain one JSON object.",
                http_status=502,
                details={"result_file": str(self.result_path)},
            )
        return value

    def latest(self) -> Dict[str, Any]:
        result = self._load_result()
        return self._redact({"ok": True, "result": result, "result_file": str(self.result_path)})

    def run(self, *, configuration: Any = None, model: Optional[str] = None) -> Dict[str, Any]:
        # Copy for each request. Never mutate os.environ or another experiment's
        # credentials; both demo routes continue to share the same runner lock.
        environ = dict(self.environ) if configuration is None else self._configured_environment(configuration)
        if model is not None:
            try:
                environ = model_environment(environ, model)
            except ValueError as exc:
                raise FrozenDemoError("INVALID_MODEL", str(exc)) from None
        self._remember_secrets(environ)
        try:
            payload = self._run(environ, runner_path=self.apr_runner_path if model is not None else self.runner_path)
            if model is not None:
                payload["execution"]["model"] = model
            return self._redact(payload)
        except FrozenDemoError as exc:
            payload = self._redact(exc.as_payload())
            raise FrozenDemoError(payload["code"], payload["error"], exc.http_status, payload.get("details")) from None

    def _run(self, environ: Mapping[str, str], *, runner_path: Optional[Path] = None) -> Dict[str, Any]:
        runner = runner_path or self.runner_path
        command = [sys.executable, str(runner)] if runner.suffix == ".py" else [str(runner)]
        if not self._run_lock.acquire(blocking=False):
            raise FrozenDemoError(
                "DEMO_ALREADY_RUNNING",
                "The CodeGraphHunter v1.2.2 demo is already running.",
                http_status=409,
            )
        try:
            if not runner.is_file() or (runner.suffix != ".py" and not os.access(runner, os.X_OK)):
                raise FrozenDemoError(
                    "DEMO_RUNNER_UNAVAILABLE",
                    "The frozen CodeGraphHunter v1.2.2 demo runner is missing or not executable.",
                    http_status=503,
                    details={"runner": str(runner)},
                )

            previous_signature = self._file_signature(self.result_path)
            return_code: Optional[int] = None
            runner_output = ""
            timed_out = False
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(runner.parent),
                    env=dict(environ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_s,
                    check=False,
                )
                return_code = int(completed.returncode)
                runner_output = completed.stdout or ""
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                output = exc.stdout or ""
                runner_output = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)
            except OSError as exc:
                runner_output = str(exc)

            current_signature = self._file_signature(self.result_path)
            result_is_fresh = current_signature is not None and current_signature != previous_signature
            try:
                result = self._load_result()
            except FrozenDemoError as exc:
                details = dict(exc.details)
                details.update(
                    {
                        "runner": str(runner),
                        "runner_return_code": return_code,
                        "runner_timed_out": timed_out,
                        "log_tail": self._failure_log_tail(runner_output),
                    }
                )
                raise FrozenDemoError(exc.code, exc.message, exc.http_status, details) from exc

            if not result_is_fresh:
                raise FrozenDemoError(
                    "DEMO_RESULT_STALE",
                    "The demo runner did not produce a new frontend result JSON.",
                    http_status=502,
                    details={
                        "runner_return_code": return_code,
                        "runner_timed_out": timed_out,
                        "result_file": str(self.result_path),
                        "log_tail": self._failure_log_tail(runner_output),
                    },
                )

            return {
                "ok": True,
                "result": result,
                "execution": {
                    "runner": str(runner),
                    "result_file": str(self.result_path),
                    "return_code": return_code,
                    "timed_out": timed_out,
                    "output_tail": _tail_text(runner_output),
                },
            }
        finally:
            self._run_lock.release()

    def _is_allowed_path(self, path: Path) -> bool:
        return any(path == root or root in path.parents for root in self.artifact_roots)

    def _resolve_artifact(self, requested_path: str) -> Path:
        if not isinstance(requested_path, str) or not requested_path.strip():
            raise FrozenDemoError("INVALID_ARTIFACT_PATH", "An artifact path is required.")
        try:
            target = Path(requested_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise FrozenDemoError(
                "ARTIFACT_NOT_FOUND",
                "The requested demo artifact does not exist.",
                http_status=404,
            ) from exc
        if not self._is_allowed_path(target):
            raise FrozenDemoError(
                "ARTIFACT_PATH_REJECTED",
                "Artifact access is restricted to the frozen demo output directories.",
                http_status=403,
            )
        if not target.is_file():
            raise FrozenDemoError(
                "ARTIFACT_NOT_FOUND", "The requested demo artifact is not a file.", 404
            )
        if target.suffix.lower() not in ALLOWED_ARTIFACT_SUFFIXES:
            raise FrozenDemoError(
                "ARTIFACT_TYPE_REJECTED", "This artifact file type cannot be displayed.", 403
            )
        return target

    def read_artifact(self, requested_path: str) -> Dict[str, Any]:
        target = self._resolve_artifact(requested_path)
        raw = target.read_bytes()
        truncated = len(raw) > MAX_ARTIFACT_BYTES
        content = raw[:MAX_ARTIFACT_BYTES].decode("utf-8", errors="replace")
        return self._redact({
            "ok": True,
            "path": str(target),
            "name": target.name,
            "size": len(raw),
            "truncated": truncated,
            "content": content,
        })

    def _failure_log_tail(self, runner_output: str) -> str:
        candidates = [self.result_path.parent / "pipeline.log"]
        for root in self.artifact_roots:
            try:
                candidates.extend(root.glob("latest/pipeline.log"))
            except OSError:
                continue
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                if self._is_allowed_path(resolved) and resolved.is_file():
                    return _tail_text(resolved.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return _tail_text(runner_output)
