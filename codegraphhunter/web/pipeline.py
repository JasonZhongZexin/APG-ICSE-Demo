"""Build the two-input CodeGraphHunter execution pipeline."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRAPHDB_WPA = Path("/home/zexin/dev/svf/SVF-GraphDB/build/bin/graphdb-wpa")
SCHEMA_PATH = PROJECT_ROOT / "codegraphhunter" / "resources" / "normalized_schema.v1.json"
CAPS_PATH = PROJECT_ROOT / "codegraphhunter" / "datasource" / "tugraph_cypher_capabilities.v1.json"
DEFAULT_DEMO_SCRIPT = Path("/tmp/cgh_demo_cpphttplib_full_patch.sh")
DEFAULT_DEMO_CASE = Path("/tmp/codegraphhunter_swe/yhirose__cpp-httplib-1765")
DEFAULT_DEMO_OUTPUT = Path("/tmp/cgh_demo_cpphttplib_final")
DEMO_OUTPUT_FILES = (
    "final_patch.diff",
    "pipeline_key.log",
    "pipeline_full.log",
    "pre_patch_test.log",
    "post_patch_test.log",
)


class PipelineConfigurationError(ValueError):
    """Raised when a submitted path or server-side setting is invalid."""


@dataclass(frozen=True)
class PipelineStage:
    key: str
    label: str
    command: Tuple[str, ...]

    @property
    def display_command(self) -> str:
        redacted: List[str] = []
        hide_next = False
        sensitive_flags = {"--bolt-pass", "--api-key", "--token"}
        for part in self.command:
            if hide_next:
                redacted.append("***")
                hide_next = False
                continue
            flag, separator, _ = part.partition("=")
            if flag in sensitive_flags:
                if separator:
                    redacted.append(f"{flag}=***")
                else:
                    redacted.append(part)
                    hide_next = True
                continue
            redacted.append(part)
        return shlex.join(redacted)


@dataclass(frozen=True)
class PipelinePlan:
    bc_path: Optional[Path]
    problem_path: Path
    repo_root: Path
    stages: Tuple[PipelineStage, ...]
    mode: str = "build"
    database: str = "default"
    external_artifacts: Tuple[Path, ...] = ()
    external_artifact_roots: Tuple[Path, ...] = ()


def _existing_file(raw_path: str, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PipelineConfigurationError(f"Select a {label} path.")
    path = Path(raw_path.strip()).expanduser().resolve()
    if not path.is_file():
        raise PipelineConfigurationError(f"The {label} does not exist or is not a file: {path}")
    return path


def _existing_directory(raw_path: str, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PipelineConfigurationError(f"Select a {label} path.")
    path = Path(raw_path.strip()).expanduser().resolve()
    if not path.is_dir():
        raise PipelineConfigurationError(f"The {label} does not exist or is not a directory: {path}")
    return path


def infer_repo_root(bc_path: Path, environ: Optional[Mapping[str, str]] = None) -> Path:
    """Infer the source root without adding another field to the UI."""
    env = environ if environ is not None else os.environ
    configured = str(env.get("CODEGRAPHHUNTER_SOURCE_ROOT", "")).strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        if not root.is_dir():
            raise PipelineConfigurationError(
                f"CODEGRAPHHUNTER_SOURCE_ROOT is not a valid directory: {root}"
            )
        return root

    start = bc_path.parent.resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
        if candidate.name == ".codegraphhunter" and candidate.parent.is_dir():
            return candidate.parent

    raise PipelineConfigurationError(
        "Cannot infer the source repository from the .bc path. Place the .bc file "
        "inside the Git repository or set CODEGRAPHHUNTER_SOURCE_ROOT before starting the server."
    )


def _resolve_graphdb_wpa(environ: Mapping[str, str]) -> Path:
    configured = str(environ.get("GRAPHDB_WPA", "")).strip()
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    discovered = shutil.which("graphdb-wpa")
    if discovered:
        candidates.append(Path(discovered))
    candidates.append(DEFAULT_GRAPHDB_WPA)

    for candidate in candidates:
        path = candidate.resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise PipelineConfigurationError(
        "Cannot find an executable graphdb-wpa. Set GRAPHDB_WPA before starting the server."
    )


def _env_int(environ: Mapping[str, str], name: str, default: int) -> str:
    raw = str(environ.get(name, default)).strip()
    try:
        return str(int(raw))
    except ValueError as exc:
        raise PipelineConfigurationError(f"{name} must be an integer; received {raw!r}.") from exc


def _truthy(environ: Mapping[str, str], name: str) -> bool:
    return str(environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _validate_controller_configuration(environ: Mapping[str, str]) -> None:
    if not str(environ.get("TUGRAPH_BOLT_PASS", "")).strip():
        raise PipelineConfigurationError(
            "TUGRAPH_BOLT_PASS is not configured. Set it in the terminal that starts the web server."
        )
    if not SCHEMA_PATH.is_file() or not CAPS_PATH.is_file():
        raise PipelineConfigurationError("The project schema/capabilities files are missing.")


def _controller_command(repo_root: Path, problem: Path, environ: Mapping[str, str]) -> List[str]:
    controller: List[str] = [
        sys.executable,
        "-m",
        "codegraphhunter.controller.attempt_controller_cli",
        "--schema",
        str(SCHEMA_PATH),
        "--caps",
        str(CAPS_PATH),
        "--use-builder",
        "--use-db",
        "--use-graph-localizer",
        "--patch-target-repo",
        str(repo_root),
        "--patch-problem-file",
        str(problem),
        "--bolt-db",
        str(environ.get("TUGRAPH_BOLT_DB", "default")).strip() or "default",
        "--max-iterations",
        _env_int(environ, "CODEGRAPHHUNTER_WEB_MAX_ITERATIONS", 1),
        "--max-queries-total",
        _env_int(environ, "CODEGRAPHHUNTER_WEB_MAX_QUERIES", 30),
        "--max-replans",
        _env_int(environ, "CODEGRAPHHUNTER_WEB_MAX_REPLANS", 3),
    ]
    if _truthy(environ, "CODEGRAPHHUNTER_USE_LLM_INFO_PLANNER"):
        controller.append("--use-llm-info-planner")
    if _truthy(environ, "CODEGRAPHHUNTER_USE_LLM_SUMMARY"):
        controller.append("--use-llm-summary")

    extra_args = str(environ.get("CODEGRAPHHUNTER_WEB_CONTROLLER_ARGS", "")).strip()
    if extra_args:
        controller.extend(shlex.split(extra_args))
    return controller


def build_pipeline_plan(
    bc_path: str,
    problem_path: str,
    environ: Optional[Mapping[str, str]] = None,
) -> PipelinePlan:
    env: Mapping[str, str] = environ if environ is not None else os.environ
    bitcode = _existing_file(bc_path, ".bc file")
    if bitcode.suffix.lower() != ".bc":
        raise PipelineConfigurationError(f"The selected file is not a .bc file: {bitcode}")
    problem = _existing_file(problem_path, "problem statement file")
    if problem.stat().st_size == 0:
        raise PipelineConfigurationError(f"The problem statement file is empty: {problem}")

    repo_root = infer_repo_root(bitcode, env)
    graphdb_wpa = _resolve_graphdb_wpa(env)
    _validate_controller_configuration(env)
    controller = _controller_command(repo_root, problem, env)

    stages = (
        PipelineStage(
            key="graph",
            label="Build program graph",
            command=(str(graphdb_wpa), "-ander", "-write2db", str(bitcode)),
        ),
        PipelineStage(
            key="localize",
            label="Localize and retrieve fault evidence",
            command=tuple(controller),
        ),
    )
    return PipelinePlan(
        bc_path=bitcode,
        problem_path=problem,
        repo_root=repo_root,
        stages=stages,
        mode="build",
        database=str(env.get("TUGRAPH_BOLT_DB", "default")).strip() or "default",
    )


def build_demo_pipeline_plan(
    test_case_path: str,
    problem_path: str,
    environ: Optional[Mapping[str, str]] = None,
) -> PipelinePlan:
    """Build the fixed cpp-httplib demo script plan and its allowlisted outputs."""
    env: Mapping[str, str] = environ if environ is not None else os.environ
    script = _existing_file(
        str(env.get("CODEGRAPHHUNTER_DEMO_SCRIPT", DEFAULT_DEMO_SCRIPT)),
        "demo script",
    )
    if not os.access(script, os.X_OK):
        raise PipelineConfigurationError(f"The demo script is not executable: {script}")

    test_case = _existing_directory(
        str(env.get("CODEGRAPHHUNTER_DEMO_CASE", DEFAULT_DEMO_CASE)),
        "fixed cpp-httplib demo case directory",
    )
    problem = _existing_file(
        str(
            env.get(
                "CODEGRAPHHUNTER_DEMO_PROBLEM",
                test_case / ".codegraphhunter" / "problem_statement.txt",
            )
        ),
        "fixed cpp-httplib problem statement file",
    )
    if problem.stat().st_size == 0:
        raise PipelineConfigurationError(f"The problem statement file is empty: {problem}")

    _validate_controller_configuration(env)
    output_dir = Path(
        str(env.get("CODEGRAPHHUNTER_DEMO_OUTPUT", DEFAULT_DEMO_OUTPUT))
    ).expanduser().resolve()
    database = str(env.get("TUGRAPH_BOLT_DB", "default")).strip() or "default"
    return PipelinePlan(
        bc_path=None,
        problem_path=problem,
        repo_root=test_case,
        stages=(
            PipelineStage(
                key="cpphttplib_demo",
                label="Run cpp-httplib localization, patch, and validation demo",
                command=(str(script),),
            ),
        ),
        mode="demo",
        database=database,
        external_artifacts=tuple(output_dir / name for name in DEMO_OUTPUT_FILES),
        external_artifact_roots=(output_dir / "debug",),
    )


def configuration_status(environ: Optional[Mapping[str, str]] = None) -> Dict[str, object]:
    env: Mapping[str, str] = environ if environ is not None else os.environ
    try:
        graphdb = str(_resolve_graphdb_wpa(env))
        graphdb_ready = True
    except PipelineConfigurationError:
        graphdb = ""
        graphdb_ready = False
    demo_script = Path(
        str(env.get("CODEGRAPHHUNTER_DEMO_SCRIPT", DEFAULT_DEMO_SCRIPT))
    ).expanduser().resolve()
    demo_case = Path(
        str(env.get("CODEGRAPHHUNTER_DEMO_CASE", DEFAULT_DEMO_CASE))
    ).expanduser().resolve()
    return {
        "graphdb_ready": graphdb_ready,
        "graphdb_wpa": graphdb,
        "schema_ready": SCHEMA_PATH.is_file() and CAPS_PATH.is_file(),
        "tugraph_password_ready": bool(str(env.get("TUGRAPH_BOLT_PASS", "")).strip()),
        "openai_key_ready": bool(str(env.get("OPENAI_API_KEY", "")).strip()),
        "demo_script_ready": demo_script.is_file() and os.access(demo_script, os.X_OK),
        "demo_case_ready": demo_case.is_dir(),
    }
