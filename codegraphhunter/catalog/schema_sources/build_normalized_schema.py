# coding: utf-8
from __future__ import print_function

import os
import json
import glob
import hashlib
import datetime

from codegraphhunter.config import (
    DBSCHEMA_DIR,
    INHERITANCE_LABELS_PATH,
    NORMALIZED_SCHEMA_OUT_PATH,
)

from codegraphhunter.catalog.schema_sources.inheritance_labels import (
    load_inheritance_labels,
    detect_cycles,
)


def _utc_now_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"


def _sha256_text(s):
    h = hashlib.sha256()
    h.update(s.encode("utf-8"))
    return h.hexdigest()


def _read_json(path):
    with open(path, "r") as f:
        return json.load(f)


def _normalize_props_list(props_list):
    """
    Input from DBSchema json:
      [{"name":"id","type":"INT32","optional":false,"index":true}, ...]
    Output dict:
      {"id": {"type":"INT32","optional":False,"index":True}, ...}
    """
    out = {}
    for p in props_list or []:
        name = p.get("name")
        if not name:
            continue
        out[name] = {
            "type": p.get("type"),
            "optional": bool(p.get("optional", False)),
            "index": bool(p.get("index", False)),
        }
    return out


def _load_raw_schemas(dbschema_dir):
    """
    Reads all *Schema.json files in directory.
    Returns: list of entries:
      {
        "label": "...",
        "kind": "VERTEX" or "EDGE",
        "primary": "id" or None,
        "properties": {...},
        "source_file": "xxx.json",
      }
    """
    entries = []
    for path in sorted(glob.glob(os.path.join(dbschema_dir, "*Schema.json"))):
        obj = _read_json(path)
        for item in obj.get("schema", []):
            label = (item.get("label") or "").strip()
            if not label:
                continue
            kind = item.get("type")
            primary = item.get("primary")  # only for VERTEX usually
            props = _normalize_props_list(item.get("properties", []))
            entries.append({
                "label": label,
                "kind": kind,
                "primary": primary,
                "properties": props,
                "source_file": os.path.basename(path),
            })
    return entries


def _build_graph_catalog(entries):
    """
    Heuristic mapping: based on source_file prefix:
      BasicBlockNodeSchema.json -> BasicBlock graph node labels
      ICFGNodeSchema.json -> ICFG ...
      PAGNodeSchema.json -> PAG ...
      CallGraphNodeSchema.json -> CallGraph ...
      CHGNodeSchema.json -> CHG ...
      SVFTypeNodeSchema.json -> TypeGraph ...
    """
    graphs = {}
    for e in entries:
        sf = e["source_file"]
        label = e["label"]
        kind = e["kind"]

        if sf.startswith("BasicBlock"):
            g = "BasicBlock"
        elif sf.startswith("ICFG"):
            g = "ICFG"
        elif sf.startswith("PAG"):
            g = "PAG"
        elif sf.startswith("CallGraph"):
            g = "CallGraph"
        elif sf.startswith("CHG"):
            g = "CHG"
        elif sf.startswith("SVFType"):
            g = "TypeGraph"
        else:
            # fallback: unknown bucket
            g = "Unknown"

        graphs.setdefault(g, {"node_labels": [], "edge_labels": []})
        if kind == "VERTEX":
            graphs[g]["node_labels"].append(label)
        else:
            graphs[g]["edge_labels"].append(label)

    # dedup + stable order
    for g, v in graphs.items():
        v["node_labels"] = sorted(list(set(v["node_labels"])))
        v["edge_labels"] = sorted(list(set(v["edge_labels"])))
    return graphs


def _index_entries(entries, graphs):
    """
    Returns:
      node_types: dict[label]->record
      edge_types: dict[label]->record
    record contains label/kind/primary_key/primary_key_type/properties/source_file/graph_type
    """
    node_types = {}
    edge_types = {}

    label_to_graph = {}
    for g, v in graphs.items():
        for lab in v["node_labels"]:
            label_to_graph[lab] = g
        for lab in v["edge_labels"]:
            label_to_graph[lab] = g

    for e in entries:
        # ---- NEW: normalize primary info ----
        pk = (e.get("primary") or "").strip() if e.get("primary") else None
        props = e.get("properties") or {}
        pk_type = None
        if pk and pk in props:
            pk_type = props[pk].get("type")

        rec = {
            "label": e["label"],
            "kind": e["kind"],

            # ---- NEW canonical fields (used by builder) ----
            "primary_key": pk,
            "primary_key_type": pk_type,

            # ---- keep legacy field for compatibility/debug ----
            "primary": e["primary"],

            "properties": props,
            "graph_type": label_to_graph.get(e["label"]),
            "source_file": e["source_file"],
            "extends": None,
            "expanded_properties": None,
            "extra_properties": None,
        }

        if e["kind"] == "VERTEX":
            node_types[e["label"]] = rec
        else:
            edge_types[e["label"]] = rec

    return node_types, edge_types



def _assert_base_exists(child, base, node_types, edge_types, abstract_types):
    if base in node_types or base in edge_types or base in abstract_types:
        return
    raise ValueError(
        "Invalid extends mapping: %s -> %s, but base not found in node_types/edge_types/abstract_types"
        % (child, base)
    )


def _resolve_expanded_properties(label, kind, node_types, edge_types, abstract_types, extends, memo):
    """
    Returns expanded_properties dict for given label.
    """
    key = (kind, label)
    if key in memo:
        return memo[key]

    if kind == "VERTEX":
        rec = node_types[label]
    else:
        rec = edge_types[label]

    base = extends.get(label)
    own_props = rec["properties"] or {}

    # base could be abstract
    if base is None:
        expanded = dict(own_props)
        memo[key] = expanded
        return expanded

    # validate base existence
    _assert_base_exists(label, base, node_types, edge_types, abstract_types)

    if base in abstract_types:
        base_prop_names = abstract_types[base].get("properties", []) or []
        # abstract only provides names; type info comes from concrete schema,
        # so we only "seed" with those names if they exist in own props.
        # If你希望 abstract 的字段强制存在，可以在这里 assert。
        base_expanded = {}
        for pn in base_prop_names:
            if pn in own_props:
                base_expanded[pn] = own_props[pn]
            else:
                # 不强制：允许抽象字段在某些子类 schema 里不存在（你也可以改成 raise）
                pass
    else:
        # base is concrete label (node or edge)
        base_kind = "VERTEX" if base in node_types else "EDGE"
        base_expanded = _resolve_expanded_properties(
            base, base_kind, node_types, edge_types, abstract_types, extends, memo
        )

    expanded = dict(base_expanded)
    expanded.update(own_props)
    memo[key] = expanded
    return expanded


def _compute_extra_properties(label, kind, expanded, node_types, edge_types, abstract_types, extends, memo_base):
    base = extends.get(label)
    if base is None:
        return dict(expanded)

    if base in abstract_types:
        # 抽象基类：extra = expanded - (abstract prop names that exist in expanded)
        base_names = set(abstract_types[base].get("properties", []) or [])
        base_present = {k: v for k, v in expanded.items() if k in base_names}
        extra = {k: v for k, v in expanded.items() if k not in base_present}
        return extra

    base_kind = "VERTEX" if base in node_types else "EDGE"
    base_expanded = _resolve_expanded_properties(
        base, base_kind, node_types, edge_types, abstract_types, extends, memo_base
    )
    extra = {}
    for k, v in expanded.items():
        if k not in base_expanded:
            extra[k] = v
    return extra


def main():
    entries = _load_raw_schemas(DBSCHEMA_DIR)
    graphs = _build_graph_catalog(entries)
    node_types, edge_types = _index_entries(entries, graphs)
    # ---- NEW: strict validation for vertex primary key ----
    for label, rec in node_types.items():
        pk = rec.get("primary_key")
        if not pk:
            raise ValueError("VERTEX label %r missing primary_key (native schema 'primary')." % label)
        props = rec.get("properties") or {}
        if pk not in props:
            raise ValueError("VERTEX label %r primary_key=%r not found in properties." % (label, pk))
        if not rec.get("primary_key_type"):
            raise ValueError("VERTEX label %r primary_key=%r missing type in properties." % (label, pk))


    abstract_types, extends = load_inheritance_labels(INHERITANCE_LABELS_PATH)
    detect_cycles(extends)

    # apply extends mapping (authoritative)
    for child, base in extends.items():
        if child in node_types:
            node_types[child]["extends"] = base
        elif child in edge_types:
            edge_types[child]["extends"] = base
        else:
            raise ValueError(
                "extends mapping references unknown child label: %s (not in node_types/edge_types)" % child
            )

    # expand properties
    memo = {}
    for label in sorted(node_types.keys()):
        expanded = _resolve_expanded_properties(
            label, "VERTEX", node_types, edge_types, abstract_types, extends, memo
        )
        node_types[label]["expanded_properties"] = expanded
        node_types[label]["extra_properties"] = _compute_extra_properties(
            label, "VERTEX", expanded, node_types, edge_types, abstract_types, extends, memo
        )

    for label in sorted(edge_types.keys()):
        expanded = _resolve_expanded_properties(
            label, "EDGE", node_types, edge_types, abstract_types, extends, memo
        )
        edge_types[label]["expanded_properties"] = expanded
        edge_types[label]["extra_properties"] = _compute_extra_properties(
            label, "EDGE", expanded, node_types, edge_types, abstract_types, extends, memo
        )

    out = {
        "version": "svf_graphdb.v1",
        "generated_at_utc": _utc_now_iso(),
        "graphs": graphs,
        "node_types": node_types,
        "edge_types": edge_types,
        "abstract_types": abstract_types,
        "inheritance_labels_path": str(INHERITANCE_LABELS_PATH),
    }

    # hash
    content_for_hash = json.dumps(out, sort_keys=True, default=str)
    out["hash"] = _sha256_text(content_for_hash)

    out_dir = os.path.dirname(NORMALIZED_SCHEMA_OUT_PATH)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir)

    with open(NORMALIZED_SCHEMA_OUT_PATH, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)

    print("Wrote:", NORMALIZED_SCHEMA_OUT_PATH)
    print("Schema hash:", out["hash"])


if __name__ == "__main__":
    main()
