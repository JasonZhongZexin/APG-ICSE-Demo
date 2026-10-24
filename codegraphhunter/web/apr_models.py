"""GPT text models for APR, verified against official docs on 2026-10-24.

Sources: https://developers.openai.com/api/docs/models/all
         https://developers.openai.com/api/docs/deprecations
Keep gpt-4o as the explicitly requested default. Exclude audio/image/ChatGPT
aliases and retired models (including GPT-4.1 nano and GPT-4 Turbo).
"""

import os
from html import escape
from pathlib import Path
from typing import Dict, Mapping


DEFAULT_MODEL = "gpt-4o"
GPT_MODELS = (
    ("gpt-4o", "GPT-4"),
    ("gpt-6-astra", "GPT-6"),
    ("gpt-6.1-sol", "GPT-6"),
    ("gpt-6-sol", "GPT-6"),
    ("gpt-6-luna", "GPT-6"),
    ("gpt-5.6-sol", "GPT-5.6"),
    ("gpt-5.6-terra", "GPT-5.6"),
    ("gpt-5.6-luna", "GPT-5.6"),
    ("gpt-5.5", "GPT-5"),
    ("gpt-5.5-pro", "GPT-5"),
    ("gpt-5.4", "GPT-5"),
    ("gpt-5.4-pro", "GPT-5"),
    ("gpt-5.4-mini", "GPT-5"),
    ("gpt-5.4-nano", "GPT-5"),
    ("gpt-5.2", "GPT-5"),
    ("gpt-5.2-pro", "GPT-5"),
    ("gpt-5.1", "GPT-5"),
    ("gpt-5", "GPT-5"),
    ("gpt-5-mini", "GPT-5"),
    ("gpt-5-nano", "GPT-5"),
    ("gpt-5-pro", "GPT-5"),
    ("gpt-4o-mini", "GPT-4"),
    ("gpt-4.1", "GPT-4"),
    ("gpt-4.1-mini", "GPT-4"),
)
MODEL_IDS = frozenset(model for model, _ in GPT_MODELS)
MODEL_VARIABLES = (
    "CODEGRAPHHUNTER_LLM_MODEL",
    "CODEGRAPHHUNTER_INFO_PLANNER_MODEL",
    "CODEGRAPHHUNTER_PATCH_MODEL",
)


def model_catalog():
    return {
        "ok": True,
        "default_model": DEFAULT_MODEL,
        "models": [{"id": model, "group": group} for model, group in GPT_MODELS],
        "verified_on": "2026-10-24",
        "source": "https://developers.openai.com/api/docs/models/all",
    }


def model_options_html() -> str:
    """Render both pickers from the same catalog, without a browser API fetch."""
    groups = {}
    for model, group in GPT_MODELS:
        selected = ' selected' if model == DEFAULT_MODEL else ''
        label = model + (' (default)' if model == DEFAULT_MODEL else '')
        groups.setdefault(group, []).append(
            f'<option value="{escape(model, quote=True)}"{selected}>{escape(label)}</option>'
        )
    return ''.join(
        f'<optgroup label="{escape(group, quote=True)}">{"".join(options)}</optgroup>'
        for group, options in groups.items()
    )


def model_environment(environ: Mapping[str, str], model: str) -> Dict[str, str]:
    if not isinstance(model, str) or model not in MODEL_IDS:
        raise ValueError("Select a supported GPT model from the list.")
    result = dict(environ)
    for variable in MODEL_VARIABLES:
        result[variable] = model
    # Scope SDK compatibility to the selected experiment's Python children.
    # The frozen runner and its repository snapshot remain unchanged.
    compatibility = str(Path(__file__).parent / "runtime_compat")
    project_root = str(Path(__file__).resolve().parents[2])
    result["PYTHONPATH"] = os.pathsep.join(filter(None, (compatibility, project_root, result.get("PYTHONPATH", ""))))
    result["CGH_APR_MODEL"] = model
    return result
