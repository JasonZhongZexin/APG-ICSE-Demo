"""Record response metadata only: never prompts, credentials or model output."""

import json
import os
import sys
import threading
from pathlib import Path

_lock = threading.Lock()
STAGES = {
    "codegraphhunter.localization.agentic_fault_localizer": "information_planner",
    "codegraphhunter.summarizer.llm_local_summary_refiner": "local_summary",
    "codegraphhunter.repair.llm_patch_generator": "patch",
}


def stage():
    frame = sys._getframe(1)
    while frame:
        name = STAGES.get(frame.f_globals.get("__name__"))
        if name:
            return name
        frame = frame.f_back
    return "other"


def record(event):
    path = os.environ.get("CGH_APR_LLM_AUDIT", "")
    if not path:
        return
    with _lock:
        with Path(path).open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
    print("[APR GPT] " + json.dumps(event, ensure_ascii=False), flush=True)
