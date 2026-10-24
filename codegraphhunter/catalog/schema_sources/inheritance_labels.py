# coding: utf-8
from __future__ import print_function

import json


def _strip_label(s):
    if s is None:
        return None
    return s.strip()


def load_inheritance_labels(path):
    """
    Returns:
      abstract_types: dict[str, dict]  (e.g., {"ICFGNode": {"properties":[...]}, ...})
      extends: dict[str, str]          child -> base
    """
    with open(path, "r") as f:
        obj = json.load(f)

    abstract_types = obj.get("abstract_types", {}) or {}
    raw_extends = obj.get("extends", {}) or {}

    extends = {}
    for child, base in raw_extends.items():
        c = _strip_label(child)
        b = _strip_label(base)
        if not c or not b:
            continue
        extends[c] = b

    # 清洗 abstract_types key
    abstract_types2 = {}
    for k, v in abstract_types.items():
        kk = _strip_label(k)
        if not kk:
            continue
        abstract_types2[kk] = v

    return abstract_types2, extends


def detect_cycles(extends):
    """
    extends: child->base
    Raise ValueError if cycle exists.
    """
    visiting = set()
    visited = set()

    def dfs(n):
        if n in visited:
            return
        if n in visiting:
            raise ValueError("Inheritance cycle detected at: %s" % n)
        visiting.add(n)
        base = extends.get(n)
        if base is not None and base in extends:
            dfs(base)
        visiting.remove(n)
        visited.add(n)

    for node in list(extends.keys()):
        dfs(node)
