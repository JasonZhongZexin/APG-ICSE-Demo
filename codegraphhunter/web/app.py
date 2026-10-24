"""Dependency-free local HTTP server for the CodeGraphHunter front end."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from codegraphhunter.web.callgraph_query_backend import (
    CallGraphQueryBackend,
    CallGraphQueryError,
)
from codegraphhunter.web.icfg_query_backend import ICFGQueryBackend, ICFGQueryError
from codegraphhunter.web.pag_query_backend import PAGQueryBackend, PAGQueryError
from codegraphhunter.web.capability_query_backend import CapabilityQueryBackend, CapabilityQueryError
from codegraphhunter.web.capability_catalog import catalog as capability_catalog
from codegraphhunter.web.query_limits import QueryLimitError, query_limit_arguments
from codegraphhunter.web.frozen_demo_backend import DEMO_CONFIGURATION_FIELDS, FrozenDemoBackend, FrozenDemoError
from codegraphhunter.web.icse_demo_settings import IcseDemoSettings, SESSION_COOKIE
from codegraphhunter.web.apr_models import DEFAULT_MODEL, model_catalog, model_environment, model_options_html
from codegraphhunter.web.pipeline import (
    PROJECT_ROOT,
    PipelineConfigurationError,
    PipelinePlan,
    build_demo_pipeline_plan,
    build_pipeline_plan,
    configuration_status,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
MAX_BODY_BYTES = 1024 * 1024
MAX_LOG_LINES = 20_000
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_EXTERNAL_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_EXTERNAL_ARTIFACT_FILES = 500
MAX_EXTERNAL_ARTIFACT_DEPTH = 8
ARTIFACT_SUFFIXES = {".json", ".txt", ".md", ".patch", ".diff", ".log"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobConflict(RuntimeError):
    pass


class PipelineJob:
    def __init__(self, plan: PipelinePlan, artifact_root: Optional[Path] = None):
        self.id = uuid.uuid4().hex[:12]
        self.plan = plan
        configured_root = os.environ.get("CODEGRAPHHUNTER_WEB_ARTIFACT_ROOT", "").strip()
        base = artifact_root or (
            Path(configured_root).expanduser()
            if configured_root
            else Path(tempfile.gettempdir()) / "codegraphhunter_web_artifacts"
        )
        self.artifact_dir = (base.resolve() / self.id).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.status = "queued"
        self.stage = "Queued"
        self.created_at = _utc_now()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.return_code: Optional[int] = None
        self.error = ""
        self.selected_function = ""
        self.selected_file = ""
        self.stop_reason = ""
        self._logs: List[str] = []
        self._log_offset = 0
        self._process: Optional[subprocess.Popen[str]] = None
        self._cancel_requested = False
        self._lock = threading.RLock()

    def append_log(self, line: str) -> None:
        clean = line.rstrip("\r\n")
        with self._lock:
            self._logs.append(clean)
            if len(self._logs) > MAX_LOG_LINES:
                drop = min(1000, len(self._logs) - MAX_LOG_LINES)
                del self._logs[:drop]
                self._log_offset += drop
            match = re.search(r"\[GRAPH LOCALIZER\] function=(\S+) file=(\S+)", clean)
            if match:
                self.selected_function, self.selected_file = match.groups()
            if clean.startswith("last_retrieval_stop_reason") and "=" in clean:
                self.stop_reason = clean.split("=", 1)[1].strip()

    def set_process(self, process: Optional[subprocess.Popen[str]]) -> None:
        with self._lock:
            self._process = process

    def cancel(self) -> bool:
        with self._lock:
            if self.status in TERMINAL_STATES:
                return False
            self._cancel_requested = True
            process = self._process
            if self.status == "queued":
                self.status = "cancelled"
                self.stage = "Cancelled"
                self.finished_at = _utc_now()
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
        return True

    def snapshot(self, cursor: int = 0) -> Dict[str, Any]:
        with self._lock:
            start = max(int(cursor), self._log_offset)
            local_start = start - self._log_offset
            logs = self._logs[local_start:]
            next_cursor = self._log_offset + len(self._logs)
            return {
                "id": self.id,
                "status": self.status,
                "stage": self.stage,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "return_code": self.return_code,
                "error": self.error,
                "bc_path": str(self.plan.bc_path) if self.plan.bc_path else "",
                "problem_path": str(self.plan.problem_path),
                "repo_root": str(self.plan.repo_root),
                "mode": self.plan.mode,
                "database": self.plan.database,
                "artifact_dir": str(self.artifact_dir),
                "artifacts": self.list_artifacts(),
                "selected_function": self.selected_function,
                "selected_file": self.selected_file,
                "stop_reason": self.stop_reason,
                "logs": logs,
                "cursor": next_cursor,
                "log_truncated": int(cursor) < self._log_offset,
            }

    @staticmethod
    def _artifact_kind(path: Path) -> str:
        name = path.as_posix().lower()
        if path.suffix.lower() in {".diff", ".patch"}:
            return "Patch diff"
        if "pipeline_full" in name:
            return "Full pipeline log"
        if "pipeline_key" in name:
            return "Key pipeline log"
        if "pre_patch_test" in name:
            return "Pre-patch test"
        if "post_patch_test" in name:
            return "Post-patch test"
        if "local_summ" in name:
            return "Local summary"
        if "global_summ" in name:
            return "Global summary"
        if "stage_summ" in name:
            return "Stage summary"
        if "localization" in name:
            return "Fault localization"
        if "diagnosis" in name:
            return "Fault diagnosis"
        return "Artifact"

    def collect_external_artifacts(self) -> int:
        """Copy allowlisted files and summary trees declared by the pipeline plan."""
        if not self.plan.external_artifacts and not self.plan.external_artifact_roots:
            return 0
        copied = 0

        def copy_one(source: Path, destination: Path) -> bool:
            nonlocal copied
            try:
                if copied >= MAX_EXTERNAL_ARTIFACT_FILES:
                    return False
                if source.is_symlink() or not source.is_file():
                    return False
                if source.suffix.lower() not in ARTIFACT_SUFFIXES:
                    return False
                if source.stat().st_size > MAX_EXTERNAL_ARTIFACT_BYTES:
                    self.append_log(
                        f"[Artifact warning] Skipped oversized artifact {source} "
                        f"(limit {MAX_EXTERNAL_ARTIFACT_BYTES} bytes)"
                    )
                    return False
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                shutil.copyfile(source, temporary)
                temporary.replace(destination)
                copied += 1
                relative = destination.relative_to(self.artifact_dir).as_posix()
                self.append_log(f"[Artifact] Collected {source} as {relative}")
                return True
            except (OSError, ValueError) as exc:
                self.append_log(f"[Artifact warning] Could not collect {source}: {exc}")
                return False

        destination_dir = self.artifact_dir / "demo_outputs"
        for configured_source in self.plan.external_artifacts:
            source = Path(configured_source)
            copy_one(source, destination_dir / source.name)

        intermediate_dir = self.artifact_dir / "demo_intermediates"
        for configured_root in self.plan.external_artifact_roots:
            if copied >= MAX_EXTERNAL_ARTIFACT_FILES:
                break
            root = Path(configured_root)
            try:
                if root.is_symlink() or not root.is_dir():
                    continue
                resolved_root = root.resolve()
                candidates = sorted(root.rglob("*"))
            except OSError as exc:
                self.append_log(f"[Artifact warning] Could not scan {root}: {exc}")
                continue
            for source in candidates:
                if copied >= MAX_EXTERNAL_ARTIFACT_FILES:
                    self.append_log(
                        f"[Artifact warning] Intermediate artifact limit reached "
                        f"({MAX_EXTERNAL_ARTIFACT_FILES} files)"
                    )
                    break
                try:
                    relative = source.relative_to(root)
                    if len(relative.parts) > MAX_EXTERNAL_ARTIFACT_DEPTH:
                        continue
                    resolved_source = source.resolve()
                    if resolved_root not in resolved_source.parents:
                        continue
                except (OSError, ValueError):
                    continue
                destination = intermediate_dir / root.name / relative
                copy_one(source, destination)
        return copied

    def list_artifacts(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            candidates = sorted(self.artifact_dir.rglob("*"))
        except OSError:
            return items
        for path in candidates:
            try:
                if not path.is_file() or path.suffix.lower() not in ARTIFACT_SUFFIXES:
                    continue
                stat = path.stat()
                relative = path.relative_to(self.artifact_dir).as_posix()
            except (OSError, ValueError):
                continue
            items.append({
                "path": relative,
                "name": path.name,
                "kind": self._artifact_kind(path),
                "size": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
            })
            if len(items) >= 500:
                break
        return items

    def read_artifact(self, relative_path: str) -> Dict[str, Any]:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("Artifact path is required.")
        target = (self.artifact_dir / relative_path).resolve()
        if not (target == self.artifact_dir or self.artifact_dir in target.parents):
            raise ValueError("Artifact path escapes the job directory.")
        if target.suffix.lower() not in ARTIFACT_SUFFIXES:
            raise ValueError("Unsupported artifact file type.")
        if not target.is_file():
            raise FileNotFoundError("Artifact not found.")
        size = target.stat().st_size
        with target.open("rb") as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        truncated = len(raw) > MAX_ARTIFACT_BYTES
        if truncated:
            raw = raw[:MAX_ARTIFACT_BYTES]
        return {
            "path": target.relative_to(self.artifact_dir).as_posix(),
            "name": target.name,
            "kind": self._artifact_kind(target),
            "size": size,
            "truncated": truncated,
            "content": raw.decode("utf-8", errors="replace"),
        }

    def write_stage_summary(
        self,
        index: int,
        stage: Any,
        started_at: str,
        finished_at: str,
        return_code: Optional[int],
        error: str = "",
    ) -> None:
        stage_name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(stage.key)).strip("_") or "stage"
        path = self.artifact_dir / "stages" / f"{index:02d}_{stage_name}_stage_summary.json"
        payload = {
            "stage_index": index,
            "stage_key": stage.key,
            "stage_label": stage.label,
            "status": (
                "cancelled"
                if self._cancel_requested
                else "succeeded" if return_code == 0 and not error else "failed"
            ),
            "started_at": started_at,
            "finished_at": finished_at,
            "return_code": return_code,
            "error": error,
            "command": stage.display_command,
            "bc_path": str(self.plan.bc_path) if self.plan.bc_path else "",
            "problem_path": str(self.plan.problem_path),
            "repo_root": str(self.plan.repo_root),
            "mode": self.plan.mode,
            "database": self.plan.database,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp_path.replace(path)


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, PipelineJob] = {}
        self._lock = threading.RLock()

    def create(self, bc_path: str, problem_path: str) -> PipelineJob:
        plan = build_pipeline_plan(bc_path, problem_path)
        return self._start(plan)

    def create_demo(self, test_case_path: str, problem_path: str) -> PipelineJob:
        plan = build_demo_pipeline_plan(test_case_path, problem_path)
        return self._start(plan)

    def _start(self, plan: PipelinePlan) -> PipelineJob:
        with self._lock:
            active = [j for j in self._jobs.values() if j.status not in TERMINAL_STATES]
            if active:
                raise JobConflict(f"Job {active[0].id} is running. Wait for it to finish or cancel it first.")
            job = PipelineJob(plan)
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"cgh-{job.id}").start()
        return job

    def get(self, job_id: str) -> Optional[PipelineJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: PipelineJob) -> None:
        with job._lock:
            if job._cancel_requested:
                return
            job.status = "running"
            job.started_at = _utc_now()

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env["CODEGRAPHHUNTER_PATCH_DEBUG_DIR"] = str(job.artifact_dir)
        try:
            for index, stage in enumerate(job.plan.stages, start=1):
                with job._lock:
                    if job._cancel_requested:
                        break
                    job.stage = stage.label
                job.append_log(f"\n[Stage {index}/{len(job.plan.stages)}] {stage.label}")
                job.append_log(f"[Working directory] {PROJECT_ROOT}")
                job.append_log(f"[Command] {stage.display_command}")
                stage_started_at = _utc_now()
                try:
                    process = subprocess.Popen(
                        list(stage.command),
                        cwd=str(PROJECT_ROOT),
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        start_new_session=True,
                    )
                except Exception as exc:
                    job.write_stage_summary(index, stage, stage_started_at, _utc_now(), None, str(exc))
                    raise
                job.set_process(process)
                with job._lock:
                    cancelled_after_spawn = job._cancel_requested
                if cancelled_after_spawn:
                    job.cancel()
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        job.append_log(line)
                finally:
                    process.stdout.close()
                return_code = process.wait()
                job.set_process(None)
                job.return_code = return_code
                job.write_stage_summary(index, stage, stage_started_at, _utc_now(), return_code)
                job.collect_external_artifacts()
                if job._cancel_requested:
                    break
                if return_code != 0:
                    raise RuntimeError(f"{stage.label} failed with exit code {return_code}.")

            with job._lock:
                if job._cancel_requested:
                    job.status = "cancelled"
                    job.stage = "Cancelled"
                else:
                    job.status = "succeeded"
                    job.stage = "Analysis complete"
        except Exception as exc:
            job.append_log(f"[Error] {exc}")
            with job._lock:
                job.error = str(exc)
                job.status = "cancelled" if job._cancel_requested else "failed"
                job.stage = "Cancelled" if job._cancel_requested else "Run failed"
        finally:
            job.set_process(None)
            with job._lock:
                job.finished_at = _utc_now()


JOBS = JobManager()
_CALLGRAPH_QUERY_BACKEND: Optional[CallGraphQueryBackend] = None
_CALLGRAPH_QUERY_BACKEND_LOCK = threading.Lock()
_ICFG_QUERY_BACKEND: Optional[ICFGQueryBackend] = None
_ICFG_QUERY_BACKEND_LOCK = threading.Lock()
_PAG_QUERY_BACKEND: Optional[PAGQueryBackend] = None
_PAG_QUERY_BACKEND_LOCK = threading.Lock()
_FROZEN_DEMO_BACKEND = FrozenDemoBackend()
_ICSE_DEMO_SETTINGS = IcseDemoSettings()


def get_callgraph_query_backend() -> CallGraphQueryBackend:
    """Lazily initialize clients so ordinary build/demo pages do not require them."""
    global _CALLGRAPH_QUERY_BACKEND
    if _CALLGRAPH_QUERY_BACKEND is not None:
        return _CALLGRAPH_QUERY_BACKEND
    with _CALLGRAPH_QUERY_BACKEND_LOCK:
        if _CALLGRAPH_QUERY_BACKEND is None:
            _CALLGRAPH_QUERY_BACKEND = CallGraphQueryBackend()
    return _CALLGRAPH_QUERY_BACKEND




def get_icfg_query_backend() -> ICFGQueryBackend:
    """Lazily initialize the ICFG clients only when its query page is used."""
    global _ICFG_QUERY_BACKEND
    if _ICFG_QUERY_BACKEND is not None:
        return _ICFG_QUERY_BACKEND
    with _ICFG_QUERY_BACKEND_LOCK:
        if _ICFG_QUERY_BACKEND is None:
            _ICFG_QUERY_BACKEND = ICFGQueryBackend()
    return _ICFG_QUERY_BACKEND


def get_pag_query_backend() -> PAGQueryBackend:
    """Initialize the PAG clients only when its query API is used."""
    global _PAG_QUERY_BACKEND
    if _PAG_QUERY_BACKEND is not None:
        return _PAG_QUERY_BACKEND
    with _PAG_QUERY_BACKEND_LOCK:
        if _PAG_QUERY_BACKEND is None:
            _PAG_QUERY_BACKEND = PAGQueryBackend()
    return _PAG_QUERY_BACKEND


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "CodeGraphHunterWeb/0.1"



    def log_message(self, fmt: str, *args: Any) -> None:
        print("[web] " + (fmt % args))

    def _json(self, status: int, payload: Dict[str, Any], *, headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _icse_session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(getattr(self, "headers", {}).get("Cookie", ""))
        except CookieError:
            cookie = SimpleCookie()
        previous = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""
        token, configuration = _ICSE_DEMO_SETTINGS.resolve(previous)
        headers = {}
        if token != previous:
            headers["Set-Cookie"] = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        return token, configuration, headers

    def _execute_graph_query(self, backend, data, *, headers=None):
        options = query_limit_arguments(data)
        if isinstance(backend, CapabilityQueryBackend):
            options.update(capability=data.get("capability", "auto"), scope=data.get("scope", ""))
        if "application/x-ndjson" not in self.headers.get("Accept", ""):
            return backend.execute(data.get("query", ""), **options)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True

        def emit(payload):
            safe = _FROZEN_DEMO_BACKEND._redact(payload)
            self.wfile.write(json.dumps(safe, ensure_ascii=False).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = backend.execute(data.get("query", ""), on_progress=lambda value: emit(dict(value, type="progress")), **options)
            emit({"type": "result", "payload": result})
        except (ICFGQueryError, PAGQueryError, CallGraphQueryError, CapabilityQueryError) as exc:
            try:
                emit({"type": "error", "payload": exc.as_payload()})
            except (BrokenPipeError, ConnectionError):
                pass
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception:
            try:
                emit({"type": "error", "payload": {"ok": False, "code": "INTERNAL_ERROR", "error": "Unexpected graph query failure."}})
            except (BrokenPipeError, ConnectionError):
                pass
        return None

    def _icse_configuration_payload(self, configuration):
        payload = _FROZEN_DEMO_BACKEND.configuration_defaults(configuration)
        state = payload["configuration"]
        payload["settings_saved"] = bool(configuration)
        payload["ready"] = state["api_key_configured"] and state["graphdb_password_configured"]
        return payload

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("The request body is empty or too large.")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("The request body must be a JSON object.")
        return data

    def _serve_static(self, relative: str) -> None:
        relative = relative or "index.html"
        target = (STATIC_DIR / relative).resolve()
        if not (target == STATIC_DIR or STATIC_DIR in target.parents) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        if relative in {"icse-demo.html", "icse-query.html"}:
            body = re.sub(
                br"<!-- APR_MODEL_OPTIONS -->.*?<!-- /APR_MODEL_OPTIONS -->",
                model_options_html().encode("utf-8"),
                body,
                flags=re.DOTALL,
            )
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/codegraphhunter/capabilities":
            self._json(HTTPStatus.OK, capability_catalog())
            return
        if parsed.path == "/api/codegraphhunter/icse-demo/models":
            self._json(HTTPStatus.OK, model_catalog())
            return
        if parsed.path == "/api/codegraphhunter/demo/latest":
            try:
                self._json(HTTPStatus.OK, _FROZEN_DEMO_BACKEND.latest())
            except FrozenDemoError as exc:
                self._json(exc.http_status, exc.as_payload())
            return
        if parsed.path == "/api/codegraphhunter/icse-demo/config":
            _, configuration, headers = self._icse_session()
            self._json(HTTPStatus.OK, self._icse_configuration_payload(configuration), headers=headers)
            return
        if parsed.path == "/api/codegraphhunter/demo/artifact":
            query = parse_qs(parsed.query)
            artifact_path = query.get("path", [""])[0]
            try:
                self._json(HTTPStatus.OK, _FROZEN_DEMO_BACKEND.read_artifact(artifact_path))
            except FrozenDemoError as exc:
                self._json(exc.http_status, exc.as_payload())
            return
        if parsed.path == "/api/health":
            self._json(HTTPStatus.OK, configuration_status())
            return
        if parsed.path == "/api/browse":
            self._browse(parse_qs(parsed.query))
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})", parsed.path)
        if match:
            job = JOBS.get(match.group(1))
            if job is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Job not found."})
                return
            query = parse_qs(parsed.query)
            try:
                cursor = max(0, int(query.get("cursor", ["0"])[0]))
            except ValueError:
                cursor = 0
            self._json(HTTPStatus.OK, job.snapshot(cursor))
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/artifact", parsed.path)
        if match:
            job = JOBS.get(match.group(1))
            if job is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Job not found."})
                return
            query = parse_qs(parsed.query)
            artifact_path = query.get("path", [""])[0]
            try:
                self._json(HTTPStatus.OK, job.read_artifact(artifact_path))
            except FileNotFoundError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except (ValueError, OSError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if parsed.path == "/":
            self._serve_static("index.html")
            return
        if parsed.path in {"/demo", "/demo/"}:
            self._serve_static("demo-v122.html")
            return
        if parsed.path in {"/icse-demo", "/icse-demo/"}:
            self._serve_static("icse-demo.html")
            return
        if parsed.path in {"/icse-demo/settings", "/icse-demo/settings/"}:
            self._serve_static("icse-settings.html")
            return
        if parsed.path.rstrip("/") in {
            "/icse-demo/query/icfg", "/icse-demo/query/pag", "/icse-demo/query/callgraph", "/icse-demo/query/capabilities"
        }:
            self._serve_static("icse-query.html")
            return
        if parsed.path.rstrip("/") in {"/query/icfg", "/query/pag", "/query/callgraph", "/query/capabilities"}:
            self._serve_static("query.html")
            return
        if parsed.path.startswith("/assets/"):
            self._serve_static(unquote(parsed.path[len("/assets/"):]))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/api/query/capabilities", "/api/codegraphhunter/icse-demo/query/capabilities"}:
            backend = None
            try:
                data = self._read_json()
                if set(data).difference({"query", "model", "max_depth", "max_entities", "capability", "scope"}):
                    raise ValueError("Unexpected query fields.")
                query_limit_arguments(data)
                headers = {}
                if parsed.path.startswith("/api/codegraphhunter/icse-demo/"):
                    _, saved, headers = self._icse_session()
                    environ = _FROZEN_DEMO_BACKEND.execution_environment(saved)
                else:
                    environ = dict(os.environ)
                environ = model_environment(environ, data.get("model", DEFAULT_MODEL))
                backend = CapabilityQueryBackend(environ=environ)
                result = self._execute_graph_query(backend, data, headers=headers)
                if result is not None:
                    self._json(HTTPStatus.OK, _FROZEN_DEMO_BACKEND._redact(result), headers=headers)
            except (CapabilityQueryError, FrozenDemoError) as exc:
                self._json(exc.http_status, _FROZEN_DEMO_BACKEND._redact(exc.as_payload()))
            except (ValueError, QueryLimitError):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "code": "INVALID_REQUEST", "error": "Provide a valid capability query and numeric limits."})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "code": "INTERNAL_ERROR", "error": "Unexpected capability query failure."})
            finally:
                if backend is not None:
                    backend.close()
            return
        if parsed.path in {
            "/api/codegraphhunter/icse-demo/query/icfg",
            "/api/codegraphhunter/icse-demo/query/pag",
            "/api/codegraphhunter/icse-demo/query/callgraph",
        }:
            backend = None
            try:
                data = self._read_json()
                if set(data).difference({"query", "model", "max_depth", "max_entities"}):
                    raise ValueError("Unexpected query fields.")
                query_limit_arguments(data)
                _, saved, headers = self._icse_session()
                environ = _FROZEN_DEMO_BACKEND.execution_environment(saved)
                try:
                    environ = model_environment(environ, data.get("model", DEFAULT_MODEL))
                except ValueError as exc:
                    raise FrozenDemoError("INVALID_MODEL", str(exc)) from None
                backend_type = {
                    "icfg": ICFGQueryBackend,
                    "pag": PAGQueryBackend,
                    "callgraph": CallGraphQueryBackend,
                }[parsed.path.rsplit("/", 1)[-1]]
                backend = backend_type(environ=environ)
                result = self._execute_graph_query(backend, data, headers=headers)
                if result is not None:
                    self._json(HTTPStatus.OK, _FROZEN_DEMO_BACKEND._redact(result), headers=headers)
            except (FrozenDemoError, ICFGQueryError, PAGQueryError, CallGraphQueryError) as exc:
                self._json(exc.http_status, _FROZEN_DEMO_BACKEND._redact(exc.as_payload()))
            except QueryLimitError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "code": "INVALID_REQUEST", "error": str(exc)})
            except (ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "code": "INVALID_REQUEST", "error": "Provide a valid JSON query object."})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "code": "INTERNAL_ERROR", "error": "Unexpected graph query failure."})
            finally:
                if backend is not None:
                    backend.close()
            return
        if parsed.path == "/api/codegraphhunter/icse-demo/config":
            try:
                data = self._read_json()
                submitted = data.get("configuration")
                if set(data) != {"configuration"} or not isinstance(submitted, dict) or set(submitted).difference(DEMO_CONFIGURATION_FIELDS):
                    raise FrozenDemoError("INVALID_CONFIGURATION", "Provide API key and Graph DB settings.")
                token, configuration, headers = self._icse_session()
                for field, value in submitted.items():
                    # Empty secret inputs retain saved credentials when editing.
                    if value == "" or (field != "graphdb_password" and isinstance(value, str) and not value.strip()):
                        continue
                    configuration[field] = value
                configuration = _FROZEN_DEMO_BACKEND.prepare_configuration(configuration)
                _ICSE_DEMO_SETTINGS.save(token, configuration)
                self._json(HTTPStatus.OK, self._icse_configuration_payload(configuration), headers=headers)
            except FrozenDemoError as exc:
                self._json(exc.http_status, exc.as_payload())
            except (ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "code": "INVALID_REQUEST", "error": "Provide a valid JSON configuration object."})
            return
        if parsed.path == "/api/codegraphhunter/icse-demo/run":
            try:
                data = self._read_json()
                _, saved, headers = self._icse_session()
                configuration = data.get("configuration", saved)
                if not isinstance(configuration, dict) or set(data).difference({"configuration", "model"}):
                    raise FrozenDemoError("INVALID_CONFIGURATION", "Provide an experiment configuration object.")
                selected_model = data.get("model", DEFAULT_MODEL)
                if not isinstance(selected_model, str):
                    raise FrozenDemoError("INVALID_MODEL", "Select a supported GPT model from the list.")
                result = _FROZEN_DEMO_BACKEND.run(configuration=configuration, model=selected_model)
                self._json(HTTPStatus.OK, result, headers=headers)
            except FrozenDemoError as exc:
                self._json(exc.http_status, exc.as_payload())
            except (ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "code": "INVALID_REQUEST", "error": "Provide a valid JSON configuration object."})
            except Exception:
                # A child process or driver error may contain a submitted secret.
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "code": "INTERNAL_ERROR", "error": "Unexpected experiment runner failure."})
            return
        if parsed.path == "/api/codegraphhunter/demo/run":
            try:
                result = _FROZEN_DEMO_BACKEND.run()
                self._json(HTTPStatus.OK, result)
            except FrozenDemoError as exc:
                self._json(exc.http_status, exc.as_payload())
            except Exception as exc:
                print("[frozen-demo] unexpected failure: %s" % exc)
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "code": "INTERNAL_ERROR",
                        "error": "Unexpected frozen demo runner failure.",
                    },
                )
            return
        if parsed.path == "/api/query/pag":
            try:
                data = self._read_json()
                result = self._execute_graph_query(get_pag_query_backend(), data)
                if result is not None:
                    self._json(HTTPStatus.OK, _FROZEN_DEMO_BACKEND._redact(result))
            except PAGQueryError as exc:
                self._json(exc.http_status, _FROZEN_DEMO_BACKEND._redact(exc.as_payload()))
            except QueryLimitError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "code": "INVALID_REQUEST", "error": str(exc)})
            except (ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "code": "INVALID_REQUEST", "error": "Provide a valid JSON query object."})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "code": "INTERNAL_ERROR", "error": "Unexpected PAG query failure."})
            return
        if parsed.path == "/api/query/icfg":
            try:
                data = self._read_json()
                result = self._execute_graph_query(get_icfg_query_backend(), data)
                if result is not None:
                    self._json(HTTPStatus.OK, result)
            except ICFGQueryError as exc:
                self._json(exc.http_status, exc.as_payload())
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "code": "INVALID_REQUEST", "error": str(exc)},
                )
            except Exception as exc:
                print("[icfg-query] unexpected failure: %s" % exc)
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "code": "INTERNAL_ERROR",
                        "error": "Unexpected ICFG query failure.",
                    },
                )
            return
        if parsed.path == "/api/query/callgraph":
            try:
                data = self._read_json()
                result = self._execute_graph_query(get_callgraph_query_backend(), data)
                if result is not None:
                    self._json(HTTPStatus.OK, result)
            except CallGraphQueryError as exc:
                self._json(exc.http_status, exc.as_payload())
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "code": "INVALID_REQUEST", "error": str(exc)},
                )
            except Exception as exc:
                print("[callgraph-query] unexpected failure: %s" % exc)
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "code": "INTERNAL_ERROR",
                        "error": "Unexpected CallGraph query failure.",
                    },
                )
            return
        if parsed.path == "/api/jobs":
            try:
                data = self._read_json()
                job = JOBS.create(str(data.get("bc_path", "")), str(data.get("problem_path", "")))
                self._json(HTTPStatus.ACCEPTED, job.snapshot())
            except PipelineConfigurationError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except JobConflict as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if parsed.path == "/api/demo/jobs":
            try:
                data = self._read_json()
                job = JOBS.create_demo(
                    str(data.get("test_case_path", "")),
                    str(data.get("problem_path", "")),
                )
                self._json(HTTPStatus.ACCEPTED, job.snapshot())
            except PipelineConfigurationError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except JobConflict as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/cancel", parsed.path)
        if match:
            job = JOBS.get(match.group(1))
            if job is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Job not found."})
                return
            changed = job.cancel()
            self._json(HTTPStatus.OK, {"cancelled": changed, "job": job.snapshot()})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _browse(self, query: Dict[str, List[str]]) -> None:
        requested = query.get("path", [str(PROJECT_ROOT)])[0].strip() or str(PROJECT_ROOT)
        kind = query.get("kind", ["problem"])[0]
        path = Path(requested).expanduser()
        if path.is_file():
            path = path.parent
        try:
            path = path.resolve()
            if not path.is_dir():
                raise OSError("not a directory")
            entries = []
            for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                try:
                    is_dir = item.is_dir()
                    is_file = item.is_file()
                except OSError:
                    continue
                if not is_dir and not is_file:
                    continue
                if kind == "directory" and is_file:
                    continue
                selectable = is_file and (kind != "bc" or item.suffix.lower() == ".bc")
                if is_file and not selectable:
                    continue
                entries.append({
                    "name": item.name,
                    "path": str(item.resolve()),
                    "is_dir": is_dir,
                    "selectable": selectable,
                })
                if len(entries) >= 500:
                    break
            parent = str(path.parent) if path.parent != path else ""
            self._json(HTTPStatus.OK, {"path": str(path), "parent": parent, "entries": entries})
        except (OSError, PermissionError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"Cannot open directory: {path} ({exc})"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CodeGraphHunter local web interface")
    parser.add_argument("--host", default="127.0.0.1", help="Listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Listen port (default: 8765)")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow a non-loopback listen address. This exposes the server-side file picker.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_remote:
        parser.error("A non-loopback address requires --allow-remote because this service can browse server paths.")
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"CodeGraphHunter Web: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server…")
    finally:
        server.server_close()
        if _CALLGRAPH_QUERY_BACKEND is not None:
            _CALLGRAPH_QUERY_BACKEND.close()
        if _ICFG_QUERY_BACKEND is not None:
            _ICFG_QUERY_BACKEND.close()


if __name__ == "__main__":
    main()
