# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Dict, Optional


def _truncate(s: Any, limit: int = 4000) -> str:
    text = "" if s is None else str(s)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...<truncated>..."


class PatchValidator(object):
    """
    Patch validator v0.

    It never modifies the original repo.
    It copies the repo into a temporary workdir, applies the patch there,
    then runs the validation command.
    """

    def __init__(
        self,
        target_repo: str,
        workdir_root: str,
        test_cmd: str,
        timeout_s: int = 120,
    ):
        self.target_repo = os.path.abspath(target_repo)
        self.workdir_root = os.path.abspath(workdir_root)
        self.test_cmd = str(test_cmd or "").strip()
        self.timeout_s = int(timeout_s)

    def _copy_repo(self, patch_iter: int) -> str:
        os.makedirs(self.workdir_root, exist_ok=True)

        stamp = int(time.time())
        dst = os.path.join(self.workdir_root, "attempt_%d_%d" % (int(patch_iter), stamp))

        def ignore(dirpath, names):
            ignored = set()
            for n in names:
                if n in (".git", "__pycache__", ".pytest_cache"):
                    ignored.add(n)
            return ignored

        shutil.copytree(self.target_repo, dst, ignore=ignore)
        return dst

    def _run(self, cmd, cwd: str, shell: bool = False) -> Dict[str, Any]:
        try:
            p = subprocess.run(
                cmd,
                cwd=cwd,
                shell=shell,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_s,
            )
            return {
                "returncode": int(p.returncode),
                "stdout": _truncate(p.stdout),
                "stderr": _truncate(p.stderr),
            }
        except subprocess.TimeoutExpired as e:
            return {
                "returncode": -999,
                "stdout": _truncate(getattr(e, "stdout", "")),
                "stderr": "TIMEOUT after %s seconds\n%s" % (self.timeout_s, _truncate(getattr(e, "stderr", ""))),
            }

    def validate(self, patch_text: str, patch_iter: int = 1) -> Dict[str, Any]:
        if not os.path.isdir(self.target_repo):
            return {
                "status": "INVALID_REPO",
                "ok": False,
                "error": "Target repo not found: %s" % self.target_repo,
            }

        if not self.test_cmd:
            return {
                "status": "INVALID_TEST_CMD",
                "ok": False,
                "error": "Empty test command.",
            }

        workdir = self._copy_repo(patch_iter)
        patch_file = os.path.join(workdir, "codegraphhunter_patch.diff")

        with open(patch_file, "w", encoding="utf-8") as f:
            f.write(patch_text.rstrip() + "\n")

        check = self._run(["git", "apply", "--check", patch_file], cwd=workdir, shell=False)
        if check["returncode"] != 0:
            return {
                "status": "APPLY_CHECK_FAILED",
                "ok": False,
                "workdir": workdir,
                "patch_file": patch_file,
                "apply_check": check,
            }

        apply = self._run(["git", "apply", patch_file], cwd=workdir, shell=False)
        if apply["returncode"] != 0:
            return {
                "status": "APPLY_FAILED",
                "ok": False,
                "workdir": workdir,
                "patch_file": patch_file,
                "apply": apply,
            }

        test = self._run(self.test_cmd, cwd=workdir, shell=True)

        if test["returncode"] == 0:
            return {
                "status": "PASSED",
                "ok": True,
                "workdir": workdir,
                "patch_file": patch_file,
                "test": test,
            }

        return {
            "status": "TEST_FAILED",
            "ok": False,
            "workdir": workdir,
            "patch_file": patch_file,
            "test": test,
        }