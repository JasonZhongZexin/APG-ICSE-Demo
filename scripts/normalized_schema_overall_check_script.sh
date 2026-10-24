#!/usr/bin/env bash
set -euo pipefail

# Resolve project root as: parent dir of this script's directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

python3 - << 'EOF'
import json, sys
from collections import defaultdict

PATH = "codegraphhunter/resources/normalized_schema.v1.json"

d = json.load(open(PATH, "r"))
node_types = d.get("node_types", {})
edge_types = d.get("edge_types", {})
abstract_types = d.get("abstract_types", {})

node_labels = set(node_types.keys())
edge_labels = set(edge_types.keys())
abs_labels  = set(abstract_types.keys())
all_labels  = node_labels | edge_labels | abs_labels

def kind_of(label):
    if label in node_labels: return "VERTEX"
    if label in edge_labels: return "EDGE"
    if label in abs_labels:  return "ABSTRACT"
    return None

# ---------- 1) missing base / child validity ----------
missing_base = []
kind_mismatch = []
extends_map = {}

for lb, info in node_types.items():
    base = info.get("extends")
    if base:
        extends_map[lb] = base
        if base not in all_labels:
            missing_base.append((lb, "VERTEX", base))
        else:
            bk = kind_of(base)
            if bk in ("VERTEX","EDGE") and bk != "VERTEX":
                kind_mismatch.append((lb, "VERTEX", base, bk))

for lb, info in edge_types.items():
    base = info.get("extends")
    if base:
        extends_map[lb] = base
        if base not in all_labels:
            missing_base.append((lb, "EDGE", base))
        else:
            bk = kind_of(base)
            if bk in ("VERTEX","EDGE") and bk != "EDGE":
                kind_mismatch.append((lb, "EDGE", base, bk))

# ---------- 2) cycle detection ----------
# build adjacency child->base (only for labels that have base)
sys.setrecursionlimit(10000)
visiting=set()
visited=set()
cycles=[]

def dfs(u, stack):
    if u in visiting:
        # record cycle path
        idx = stack.index(u) if u in stack else 0
        cycles.append(stack[idx:] + [u])
        return
    if u in visited:
        return
    visiting.add(u)
    stack.append(u)
    v = extends_map.get(u)
    if v and v in extends_map:
        dfs(v, stack)
    stack.pop()
    visiting.remove(u)
    visited.add(u)

for u in list(extends_map.keys()):
    if u not in visited:
        dfs(u, [])

# ---------- 3) expanded/extra consistency ----------
# Check: expanded == base_expanded U own_properties (keys-wise)
# And: extra == expanded - base_expanded (keys-wise)
def keys(x):
    return set((x or {}).keys())

expanded_err = []
extra_err = []

def get_base_expanded(base):
    bk = kind_of(base)
    if bk == "VERTEX":
        return node_types[base].get("expanded_properties", {})
    if bk == "EDGE":
        return edge_types[base].get("expanded_properties", {})
    if bk == "ABSTRACT":
        # abstract only provides property names; treat as set
        return {p: None for p in abstract_types[base].get("properties", [])}
    return {}

def check_one(label, table, kind):
    info = table[label]
    own = info.get("properties", {}) or {}
    exp = info.get("expanded_properties", {}) or {}
    ext = info.get("extra_properties", {}) or {}
    base = info.get("extends")

    if not base:
        # root: usually expanded should at least include own
        if not keys(own).issubset(keys(exp)):
            expanded_err.append((kind, label, "ROOT_MISSING_OWN_IN_EXPANDED"))
        return

    base_exp = get_base_expanded(base)
    should = keys(base_exp) | keys(own)
    if keys(exp) != should:
        expanded_err.append((kind, label, "EXPANDED_KEYS_MISMATCH", len(keys(exp)), len(should)))

    should_extra = keys(exp) - keys(base_exp)
    if keys(ext) != should_extra:
        extra_err.append((kind, label, "EXTRA_KEYS_MISMATCH", len(keys(ext)), len(should_extra)))

for lb in node_labels:
    check_one(lb, node_types, "VERTEX")
for lb in edge_labels:
    check_one(lb, edge_types, "EDGE")

# ---------- 4) abstract conflicts (same name in both abstract and concrete) ----------
abs_conflict = sorted(list((abs_labels & node_labels) | (abs_labels & edge_labels)))

# ---------- report ----------
print("=== CodeGraphHunter normalized_schema self-check ===")
print("File:", PATH)

print("\n[1] Missing base labels:", len(missing_base))
if missing_base[:20]:
    for x in missing_base[:20]:
        print("  child=%s (%s) -> base=%s (NOT FOUND)" % x)
    if len(missing_base) > 20:
        print("  ... (+%d more)" % (len(missing_base)-20))

print("\n[2] Kind mismatch (vertex extends edge / edge extends vertex):", len(kind_mismatch))
if kind_mismatch[:20]:
    for (c, ck, b, bk) in kind_mismatch[:20]:
        print("  %s(%s) extends %s(%s)" % (c, ck, b, bk))

print("\n[3] Inheritance cycles:", len(cycles))
if cycles[:5]:
    for cyc in cycles[:5]:
        print("  cycle:", " -> ".join(cyc))

print("\n[4] expanded_properties inconsistencies:", len(expanded_err))
if expanded_err[:20]:
    for e in expanded_err[:20]:
        print(" ", e)

print("\n[5] extra_properties inconsistencies:", len(extra_err))
if extra_err[:20]:
    for e in extra_err[:20]:
        print(" ", e)

print("\n[6] abstract label conflicts (same name appears as concrete too):", len(abs_conflict))
if abs_conflict:
    print(" ", abs_conflict[:50])

# exit non-zero if any serious issues
bad = (len(missing_base) + len(kind_mismatch) + len(cycles) + len(expanded_err) + len(extra_err)) > 0
print("\nOverall:", "PASS ✅" if not bad else "FAIL ❌")
sys.exit(1 if bad else 0)
EOF
