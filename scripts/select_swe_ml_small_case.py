# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Dict, List, Tuple
from datasets import load_dataset


CPP_EXTS = (
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx",
)

PREFERRED_REPOS = {
    "redis/redis": "C",
    "fmtlib/fmt": "C++",
}


def changed_files_from_patch(patch: str) -> List[str]:
    files = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            # diff --git a/src/foo.c b/src/foo.c
            parts = line.split()
            if len(parts) >= 4:
                b = parts[3]
                if b.startswith("b/"):
                    files.append(b[2:])
        elif line.startswith("+++ b/"):
            files.append(line[len("+++ b/"):].strip())
    out = []
    seen = set()
    for f in files:
        if f not in seen and f != "/dev/null":
            seen.add(f)
            out.append(f)
    return out


def count_changed_lines(patch: str) -> int:
    n = 0
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            n += 1
    return n


def is_cpp_source_file(path: str) -> bool:
    return path.endswith(CPP_EXTS)


def score_instance(row: Dict) -> Tuple[int, int, int, int]:
    patch = row.get("patch") or ""
    test_patch = row.get("test_patch") or ""
    problem = row.get("problem_statement") or ""
    files = changed_files_from_patch(patch)
    source_files = [f for f in files if is_cpp_source_file(f)]

    # lower is better
    return (
        len(source_files),
        count_changed_lines(patch),
        count_changed_lines(test_patch),
        len(problem),
    )


def main():
    ds = load_dataset("SWE-bench/SWE-bench_Multilingual", split="test")

    candidates = []

    for row in ds:
        repo = row.get("repo")
        if repo not in PREFERRED_REPOS:
            continue

        patch = row.get("patch") or ""
        files = changed_files_from_patch(patch)
        source_files = [f for f in files if is_cpp_source_file(f)]

        if len(source_files) != 1:
            continue

        patch_lines = count_changed_lines(patch)
        test_patch_lines = count_changed_lines(row.get("test_patch") or "")

        # keep small cases
        if patch_lines > 80:
            continue
        if test_patch_lines > 120:
            continue

        candidates.append({
            "instance_id": row.get("instance_id"),
            "repo": repo,
            "language": PREFERRED_REPOS[repo],
            "base_commit": row.get("base_commit"),
            "source_file": source_files[0],
            "patch_lines": patch_lines,
            "test_patch_lines": test_patch_lines,
            "problem_len": len(row.get("problem_statement") or ""),
            "fail_to_pass": row.get("FAIL_TO_PASS"),
            "pass_to_pass_count": len(row.get("PASS_TO_PASS") or []),
            "problem_statement_preview": (row.get("problem_statement") or "")[:600].replace("\n", " "),
        })

    candidates.sort(key=lambda x: (
        x["patch_lines"],
        x["test_patch_lines"],
        x["problem_len"],
    ))

    print("Found candidates:", len(candidates))
    print()

    for i, c in enumerate(candidates[:20], 1):
        print("=" * 80)
        print("RANK:", i)
        print("instance_id      :", c["instance_id"])
        print("repo             :", c["repo"])
        print("language         :", c["language"])
        print("base_commit      :", c["base_commit"])
        print("source_file      :", c["source_file"])
        print("patch_lines      :", c["patch_lines"])
        print("test_patch_lines :", c["test_patch_lines"])
        print("problem_len      :", c["problem_len"])
        print("fail_to_pass     :", c["fail_to_pass"])
        print("pass_to_pass_cnt :", c["pass_to_pass_count"])
        print("problem_preview  :", c["problem_statement_preview"])
        print()

    if candidates:
        best = candidates[0]
        print("=" * 80)
        print("BEST_CANDIDATE")
        print("export CODEGRAPHHUNTER_SWE_INSTANCE_ID='%s'" % best["instance_id"])
        print("export CODEGRAPHHUNTER_PATCH_TARGET_REPO='/tmp/codegraphhunter_swe/%s'" % best["instance_id"])
        print("export CODEGRAPHHUNTER_PATCH_SOURCE_FILE='%s'" % best["source_file"])
        print("export CODEGRAPHHUNTER_PATCH_TEST_CMD='true'")
        print("repo=%s" % best["repo"])
        print("base_commit=%s" % best["base_commit"])


if __name__ == "__main__":
    main()