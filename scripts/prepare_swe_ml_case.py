# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import subprocess
import sys
from datasets import load_dataset


def run(cmd, cwd=None):
    print("[RUN]", " ".join(cmd), "cwd=%s" % (cwd or os.getcwd()))
    p = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p.stdout:
        print(p.stdout)
    if p.stderr:
        print(p.stderr)
    if p.returncode != 0:
        raise SystemExit("Command failed: %s" % " ".join(cmd))


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 scripts/prepare_swe_ml_case.py <instance_id>")

    instance_id = sys.argv[1]
    out_root = os.environ.get("CODEGRAPHHUNTER_SWE_ROOT", "/tmp/codegraphhunter_swe")
    os.makedirs(out_root, exist_ok=True)

    ds = load_dataset("SWE-bench/SWE-bench_Multilingual", split="test")

    row = None
    for x in ds:
        if x.get("instance_id") == instance_id:
            row = dict(x)
            break

    if row is None:
        raise SystemExit("Instance not found: %s" % instance_id)

    repo = row["repo"]
    base_commit = row["base_commit"]
    repo_url = "https://github.com/%s.git" % repo

    target_dir = os.path.join(out_root, instance_id)

    if not os.path.isdir(target_dir):
        run(["git", "clone", repo_url, target_dir])
    else:
        print("[INFO] target repo already exists:", target_dir)

    run(["git", "fetch", "--all"], cwd=target_dir)
    run(["git", "checkout", base_commit], cwd=target_dir)
    run(["git", "reset", "--hard", base_commit], cwd=target_dir)
    run(["git", "clean", "-fdx"], cwd=target_dir)

    meta_dir = os.path.join(target_dir, ".codegraphhunter")
    os.makedirs(meta_dir, exist_ok=True)

    with open(os.path.join(meta_dir, "problem_statement.txt"), "w", encoding="utf-8") as f:
        f.write(row.get("problem_statement") or "")

    with open(os.path.join(meta_dir, "gold.patch"), "w", encoding="utf-8") as f:
        f.write(row.get("patch") or "")

    with open(os.path.join(meta_dir, "test.patch"), "w", encoding="utf-8") as f:
        f.write(row.get("test_patch") or "")

    meta = {
        "instance_id": row.get("instance_id"),
        "repo": row.get("repo"),
        "base_commit": row.get("base_commit"),
        "FAIL_TO_PASS": row.get("FAIL_TO_PASS"),
        "PASS_TO_PASS": row.get("PASS_TO_PASS"),
    }

    with open(os.path.join(meta_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)

    print()
    print("=== PREPARED ===")
    print("export CODEGRAPHHUNTER_SWE_INSTANCE_ID='%s'" % instance_id)
    print("export CODEGRAPHHUNTER_PATCH_TARGET_REPO='%s'" % target_dir)
    print("export CODEGRAPHHUNTER_PATCH_PROBLEM_FILE='%s'" % os.path.join(meta_dir, "problem_statement.txt"))
    print("export CODEGRAPHHUNTER_PATCH_GOLD_PATCH='%s'" % os.path.join(meta_dir, "gold.patch"))
    print("export CODEGRAPHHUNTER_PATCH_TEST_PATCH='%s'" % os.path.join(meta_dir, "test.patch"))
    print("repo=%s" % repo)
    print("base_commit=%s" % base_commit)


if __name__ == "__main__":
    main()