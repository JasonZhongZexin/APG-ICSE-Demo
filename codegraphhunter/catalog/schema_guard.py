# -*- coding: utf-8 -*-
"""
SchemaGuard (MVP++ with literal type checks)
Python 3.6 compatible.

Validates:
- label existence + TuGraph "exactly one label" constraint for node/edge patterns
- property existence (dot access, map literals, WHERE predicates)
- literal type checks for:
    * map literals: (n:Label {prop: <literal>})
    * edge map literals: [:Edge {prop: <literal>}]
    * WHERE comparisons: n.prop <op> <literal> and <literal> <op> n.prop
    * WHERE IN list: n.prop IN [<literal>, ...]
    * WHERE string ops: STARTS WITH / ENDS WITH / CONTAINS / =~
    * WHERE null checks: IS NULL / IS NOT NULL
- CALL procedure allowlist
- function allowlist (strict unknown functions optional)
- forbid multi-statement (;)
- forbid dynamic bracket property access n["prop"] when pending
"""

import json
import re


class SchemaGuardErrorCodes(object):
    LABEL_NOT_FOUND = "LABEL_NOT_FOUND"
    EDGE_TYPE_ERROR = "EDGE_TYPE_ERROR"
    PROPERTY_NOT_FOUND = "PROPERTY_NOT_FOUND"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    ABSTRACT_LABEL_NOT_QUERYABLE = "ABSTRACT_LABEL_NOT_QUERYABLE"

    NO_LABEL_ERROR = "NO_LABEL_ERROR"
    MULTI_LABEL_NOT_ALLOWED = "MULTI_LABEL_NOT_ALLOWED"

    UNSUPPORTED_SYNTAX = "UNSUPPORTED_SYNTAX"
    PROCEDURE_NOT_ALLOWED = "PROCEDURE_NOT_ALLOWED"
    MULTI_STATEMENT_FORBIDDEN = "MULTI_STATEMENT_FORBIDDEN"


def _load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def _is_string_literal(s):
    s = s.strip()
    return (len(s) >= 2) and ((s[0] == "'" and s[-1] == "'") or (s[0] == '"' and s[-1] == '"'))


def _norm_func_name(name):
    return name.strip().lower()


def _extract_type_name(prop_meta):
    """
    Robustly extract type name from normalized schema property definition.

    Supported shapes:
      - "STRING"
      - {"type": "STRING", ...}
      - {"name": "...", "type": "STRING", ...}
      - {"Type": "STRING"} (fallback)
      - {"data_type": "STRING"} (fallback)
    """
    if prop_meta is None:
        return None
    if isinstance(prop_meta, str):
        return prop_meta
    if isinstance(prop_meta, dict):
        if "type" in prop_meta:
            return prop_meta.get("type")
        if "Type" in prop_meta:
            return prop_meta.get("Type")
        if "data_type" in prop_meta:
            return prop_meta.get("data_type")
    return None


def _expected_category(type_name):
    """
    Map schema type names to coarse categories.
    """
    if not type_name:
        return "ANY"
    t = str(type_name).strip().upper()

    if t in ("STRING", "STR", "TEXT"):
        return "STRING"
    if t in ("BOOL", "BOOLEAN"):
        return "BOOLEAN"

    if t.startswith("INT") or t in ("INTEGER", "LONG", "SHORT", "BYTE", "UINT32", "UINT64"):
        return "INT"

    if t in ("FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL"):
        return "FLOAT"

    if t in ("LIST", "ARRAY", "VECTOR"):
        return "LIST"
    if t in ("MAP", "OBJECT", "JSON"):
        return "MAP"

    if t in ("DATE", "DATETIME", "TIME", "TIMESTAMP"):
        return "STRING"

    return "ANY"


def _literal_category(value_token):
    """
    Classify a Cypher literal token:
      - "STRING", "INT", "FLOAT", "BOOLEAN", "NULL", "LIST", "MAP", "UNKNOWN"
    Only returns concrete for obvious literals; otherwise UNKNOWN.
    """
    v = value_token.strip()
    if not v:
        return "UNKNOWN"

    if _is_string_literal(v):
        return "STRING"

    vl = v.lower()
    if vl == "true" or vl == "false":
        return "BOOLEAN"
    if vl == "null":
        return "NULL"

    if v.startswith("[") and v.endswith("]"):
        return "LIST"
    if v.startswith("{") and v.endswith("}"):
        return "MAP"

    if re.match(r"^-?\d+$", v):
        return "INT"
    if re.match(r"^-?\d+\.\d+([eE][+-]?\d+)?$", v) or re.match(r"^-?\d+([eE][+-]?\d+)$", v):
        return "FLOAT"

    return "UNKNOWN"


def _split_top_level_commas(s):
    """
    Split by commas not inside quotes/brackets/braces/parens.
    """
    items = []
    cur = []
    depth_paren = 0
    depth_brack = 0
    depth_brace = 0
    in_single = False
    in_double = False

    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            cur.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            cur.append(ch)
            i += 1
            continue

        if in_single or in_double:
            cur.append(ch)
            i += 1
            continue

        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack = max(0, depth_brack - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)

        if ch == "," and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
            item = "".join(cur).strip()
            if item:
                items.append(item)
            cur = []
            i += 1
            continue

        cur.append(ch)
        i += 1

    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return items


def _parse_map_literal_body(map_body):
    """
    Parse "k1: v1, k2: v2" -> [(k1, v1token), ...] (top-level only)
    """
    pairs = []
    for it in _split_top_level_commas(map_body):
        if ":" not in it:
            continue
        k, v = it.split(":", 1)
        key = k.strip()
        val = v.strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            pairs.append((key, val))
    return pairs


def _strip_outer_brackets(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == "[" and s[-1] == "]":
        return s[1:-1]
    return s


class NormalizedSchema(object):
    def __init__(self, schema_json):
        self.raw = schema_json
        self.node_types = schema_json.get("node_types", {}) or {}
        self.edge_types = schema_json.get("edge_types", {}) or {}
        self.abstract_types = schema_json.get("abstract_types", {}) or {}

        self._node_props = {}
        self._node_prop_types = {}
        for lbl, meta in self.node_types.items():
            props = meta.get("expanded_properties") or meta.get("properties") or {}
            self._node_props[lbl] = set(list(props.keys()))
            self._node_prop_types[lbl] = {}
            for p, pmeta in props.items():
                self._node_prop_types[lbl][p] = _extract_type_name(pmeta)

        self._edge_props = {}
        self._edge_prop_types = {}
        for lbl, meta in self.edge_types.items():
            props = meta.get("expanded_properties") or meta.get("properties") or {}
            self._edge_props[lbl] = set(list(props.keys()))
            self._edge_prop_types[lbl] = {}
            for p, pmeta in props.items():
                self._edge_prop_types[lbl][p] = _extract_type_name(pmeta)

    def is_abstract(self, label):
        return label in self.abstract_types

    def has_node_label(self, label):
        return label in self.node_types

    def has_edge_label(self, label):
        return label in self.edge_types

    def node_has_prop(self, label, prop):
        s = self._node_props.get(label)
        return (s is not None) and (prop in s)

    def edge_has_prop(self, label, prop):
        s = self._edge_props.get(label)
        return (s is not None) and (prop in s)

    def node_prop_type(self, label, prop):
        return (self._node_prop_types.get(label) or {}).get(prop)

    def edge_prop_type(self, label, prop):
        return (self._edge_prop_types.get(label) or {}).get(prop)


class TuGraphCypherCapabilities(object):
    def __init__(self, caps_json):
        self.raw = caps_json
        self.cypher = caps_json.get("cypher", {}) or {}
        self.clauses = (self.cypher.get("clauses") or {})
        self.functions_supported = set(
            [_norm_func_name(x) for x in ((self.cypher.get("functions") or {}).get("supported") or [])]
        )
        self.functions_not_supported = set(
            [_norm_func_name(x) for x in ((self.cypher.get("functions") or {}).get("not_supported") or [])]
        )

        self.semantic = self.cypher.get("semantic_constraints") or {}
        self.procedures = (caps_json.get("procedures") or {})
        self.proc_signatures = (self.procedures.get("builtin_signatures") or {})
        self.supports_call_yield = bool(self.procedures.get("supports_call_yield", True))

        ops = self.cypher.get("operators") or {}
        self.dynamic_prop_pending = False
        if isinstance(ops, dict):
            if ops.get("BRACKET_DYNAMIC_PROPERTY_ACCESS") == "pending":
                self.dynamic_prop_pending = True

    def has_procedure(self, name):
        return name in self.proc_signatures

    def is_function_supported(self, fn_name):
        fn = _norm_func_name(fn_name)
        if fn in self.functions_not_supported:
            return False
        if fn in self.functions_supported:
            return True
        return False

    def is_function_known_not_supported(self, fn_name):
        return _norm_func_name(fn_name) in self.functions_not_supported


class SchemaGuard(object):
    def __init__(self, normalized_schema_path, cypher_caps_path, strict_unknown_functions=True, allow_wildcard_edge=False, allow_unlabeled_nodes=False, ):
        self.schema = NormalizedSchema(_load_json(normalized_schema_path))
        self.caps = TuGraphCypherCapabilities(_load_json(cypher_caps_path))
        self.strict_unknown_functions = bool(strict_unknown_functions)
        self.allow_wildcard_edge = bool(allow_wildcard_edge)
        self.allow_unlabeled_nodes = bool(allow_unlabeled_nodes)

    def validate(self, query):
        if query is None:
            return self._err(SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX, "Empty query")
        q = query.strip()
        if not q:
            return self._err(SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX, "Empty query")

        if ";" in q:
            return self._err(SchemaGuardErrorCodes.MULTI_STATEMENT_FORBIDDEN, "Semicolon is not allowed")

        if q.upper().startswith("CALL "):
            return self._validate_call(q)

        return self._validate_cypher_subset(q)

    # ---------------- CALL ----------------

    def _validate_call(self, q):
        m = re.match(r"(?is)^\s*CALL\s+([A-Za-z_][A-Za-z0-9_.]*)\s*\(", q)
        if not m:
            return self._err(SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX, "Invalid CALL syntax (cannot find procedure name)")

        proc = m.group(1)
        if not self.caps.has_procedure(proc):
            return self._err(
                SchemaGuardErrorCodes.PROCEDURE_NOT_ALLOWED,
                "Procedure not allowed in query stage: %s" % proc,
            )

        if (not self.caps.supports_call_yield) and re.search(r"(?is)\bYIELD\b", q):
            return self._err(SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX, "YIELD is not supported")

        fn_err = self._check_functions(q)
        if fn_err:
            return fn_err

        dp_err = self._check_dynamic_property_access(q)
        if dp_err:
            return dp_err

        return {"status": "ok", "validated_query": q, "warnings": []}

    # ---------------- Cypher subset ----------------

    def _validate_cypher_subset(self, q):
        warnings = []

        node_bindings, err = self._extract_node_bindings(q)
        if err:
            return err

        edge_labels, edge_err = self._extract_edge_labels(q)
        if edge_err:
            return edge_err

        # label existence
        for _, lbl in node_bindings.items():
            if self.schema.is_abstract(lbl) and (not self.schema.has_node_label(lbl)):
                return self._err(
                    SchemaGuardErrorCodes.ABSTRACT_LABEL_NOT_QUERYABLE,
                    "Abstract label is not queryable: %s" % lbl,
                )
            if not self.schema.has_node_label(lbl):
                return self._err(SchemaGuardErrorCodes.LABEL_NOT_FOUND, "Node label not found: %s" % lbl)

        for e_lbl in edge_labels:
            if self.schema.is_abstract(e_lbl) and (not self.schema.has_edge_label(e_lbl)):
                return self._err(
                    SchemaGuardErrorCodes.ABSTRACT_LABEL_NOT_QUERYABLE,
                    "Abstract edge label is not queryable: %s" % e_lbl,
                )
            if not self.schema.has_edge_label(e_lbl):
                return self._err(SchemaGuardErrorCodes.EDGE_TYPE_ERROR, "Edge label not found: %s" % e_lbl)

        # property + type checks
        prop_err = self._check_properties_and_types(q, node_bindings)
        if prop_err:
            return prop_err

        where_err = self._check_where_predicates(q, node_bindings)
        if where_err:
            return where_err

        edge_prop_err = self._check_edge_properties_in_patterns(q)
        if edge_prop_err:
            return edge_prop_err

        # function + bracket checks
        fn_err = self._check_functions(q)
        if fn_err:
            return fn_err

        dp_err = self._check_dynamic_property_access(q)
        if dp_err:
            return dp_err

        return {"status": "ok", "validated_query": q, "warnings": warnings}

    # ---------------- extraction ----------------

    def _extract_node_bindings(self, q):
        """
        Enforce TuGraph: each node pattern must have exactly ONE label.
        """
        bindings = {}
        for m in re.finditer(r"\(([^)]*)\)", q):
            inside = m.group(1).strip()
            if not inside:
                return bindings, self._err(SchemaGuardErrorCodes.NO_LABEL_ERROR, "Node pattern has no label: ()")

            labels = re.findall(r":\s*([A-Za-z_][A-Za-z0-9_]*)", inside)
            if not labels:
                # REGTEST/DEBUG ONLY: allow unlabeled node pattern like "(m)" or "(m {id: ...})"
                # We do NOT bind var->label in this case; property/type checks on this var will be skipped.
                if self.allow_unlabeled_nodes:
                    # basic sanity: must have a variable name if unlabeled
                    var_m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\b", inside)
                    if var_m:
                        continue
                return bindings, self._err(
                    SchemaGuardErrorCodes.NO_LABEL_ERROR,
                    "Node pattern must have exactly one label (missing): (%s)" % inside,
                )
            if len(labels) > 1:
                return bindings, self._err(
                    SchemaGuardErrorCodes.MULTI_LABEL_NOT_ALLOWED,
                    "Node pattern has multiple labels (not allowed): %s" % (":".join(labels)),
                )

            label = labels[0]
            var_m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\:|\s|\{)", inside)
            if var_m:
                var = var_m.group(1)
                if var in bindings and bindings[var] != label:
                    return bindings, self._err(
                        SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX,
                        "Variable '%s' is bound to multiple labels: %s vs %s" % (var, bindings[var], label),
                    )
                bindings[var] = label

        return bindings, None

    def _extract_edge_labels(self, q):
        """
        Enforce TuGraph: each relationship pattern must have exactly ONE label.
        """
        edge_labels = set()
        for m in re.finditer(r"\[([^\]]*)\]", q):
            inside = m.group(1).strip()
            # [] is always invalid
            if not inside:
                return edge_labels, self._err(
                    SchemaGuardErrorCodes.NO_LABEL_ERROR,
                    "Relationship pattern must have exactly one label (missing): []",
                )
            # allow wildcard relationship pattern ONLY if enabled ----
            # examples:
            #   [*] , [*1..3] , [r*1..3] , [r * 1..3]
            # We treat "no :Label" + having "*" as wildcard edge pattern.
            if self.allow_wildcard_edge:
                has_label = bool(re.search(r":\s*[A-Za-z_][A-Za-z0-9_]*", inside))
                has_star = ("*" in inside)
                if (not has_label) and has_star:
                    # skip label existence checks because there is no edge label to validate
                    # (property checks on edge maps also won't trigger without a label)
                    continue

            labels = re.findall(r":\s*([A-Za-z_][A-Za-z0-9_]*)", inside)
            if not labels:
                return edge_labels, self._err(
                    SchemaGuardErrorCodes.NO_LABEL_ERROR,
                    "Relationship pattern must have exactly one label (missing): [%s]" % inside,
                )
            if len(labels) > 1:
                return edge_labels, self._err(
                    SchemaGuardErrorCodes.MULTI_LABEL_NOT_ALLOWED,
                    "Relationship pattern has multiple labels (not allowed): %s" % (":".join(labels)),
                )

            edge_labels.add(labels[0])

        return edge_labels, None

    # ---------------- property + type checking ----------------

    def _type_check_literal(self, label_kind, label, prop, value_token, context_hint):
        """
        label_kind: "VERTEX" or "EDGE"
        Checks only if value_token is an obvious literal; otherwise skip.
        """
        lit_cat = _literal_category(value_token)
        if lit_cat in ("UNKNOWN", "NULL"):
            return None

        if label_kind == "VERTEX":
            tname = self.schema.node_prop_type(label, prop)
        else:
            tname = self.schema.edge_prop_type(label, prop)

        exp_cat = _expected_category(tname)
        if exp_cat == "ANY":
            return None

        # allow INT literal for FLOAT schema
        if exp_cat == "FLOAT" and lit_cat == "INT":
            return None

        if lit_cat != exp_cat:
            return self._err(
                SchemaGuardErrorCodes.TYPE_MISMATCH,
                "Type mismatch (%s) for %s.%s: schema expects %s but query literal is %s (value=%s)"
                % (context_hint, label, prop, exp_cat, lit_cat, value_token.strip()),
            )
        return None

    def _check_properties_and_types(self, q, node_bindings):
        """
        - dot access: n.prop (existence)
        - node map literal: (n:Label {k: v})  (existence + literal type)
        """
        # dot access existence
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b", q):
            var = m.group(1)
            prop = m.group(2)
            lbl = node_bindings.get(var)
            if not lbl:
                continue
            if not self.schema.node_has_prop(lbl, prop):
                return self._err(
                    SchemaGuardErrorCodes.PROPERTY_NOT_FOUND,
                    "%s has no property '%s' (var=%s)" % (lbl, prop, var),
                )

        # node map literals
        for m in re.finditer(r"\(([^)]*?)\{([^}]*)\}([^)]*?)\)", q):
            inside_left = (m.group(1) or "").strip()
            map_body = (m.group(2) or "").strip()

            labels = re.findall(r":\s*([A-Za-z_][A-Za-z0-9_]*)", inside_left)
            if not labels or len(labels) != 1:
                continue
            lbl = labels[0]

            for (k, vtok) in _parse_map_literal_body(map_body):
                if not self.schema.node_has_prop(lbl, k):
                    return self._err(
                        SchemaGuardErrorCodes.PROPERTY_NOT_FOUND,
                        "%s has no property '%s' (in node map literal)" % (lbl, k),
                    )
                type_err = self._type_check_literal("VERTEX", lbl, k, vtok, "NODE_MAP")
                if type_err:
                    return type_err

        return None

    def _check_edge_properties_in_patterns(self, q):
        """
        relationship pattern property maps: [ ... :Edge {k:v} ... ] (existence + literal type)
        """
        for m in re.finditer(r"\[([^\]]*?)\{([^}]*)\}([^\]]*?)\]", q):
            inside_left = (m.group(1) or "").strip()
            map_body = (m.group(2) or "").strip()

            labels = re.findall(r":\s*([A-Za-z_][A-Za-z0-9_]*)", inside_left)
            if not labels or len(labels) != 1:
                continue
            lbl = labels[0]

            for (k, vtok) in _parse_map_literal_body(map_body):
                if not self.schema.edge_has_prop(lbl, k):
                    return self._err(
                        SchemaGuardErrorCodes.PROPERTY_NOT_FOUND,
                        "%s has no property '%s' (in edge map literal)" % (lbl, k),
                    )
                type_err = self._type_check_literal("EDGE", lbl, k, vtok, "EDGE_MAP")
                if type_err:
                    return type_err
        return None

    # ---------------- WHERE predicates (full coverage) ----------------

    def _check_where_predicates(self, q, node_bindings):
        """
        Covers:
        - comparisons: n.prop <op> literal, literal <op> n.prop
        - IN list: n.prop IN [lit, ...]
        - string ops: STARTS WITH/ENDS WITH/CONTAINS/=~
        - NULL checks: IS NULL / IS NOT NULL
        We scan the whole query for robustness (MATCH/WHERE, WITH/WHERE, OPTIONAL MATCH/WHERE...).
        """

        # ---------- Helpers ----------
        def get_label(var):
            return node_bindings.get(var)

        def ensure_prop_exists(lbl, prop, var, ctx):
            if not self.schema.node_has_prop(lbl, prop):
                return self._err(
                    SchemaGuardErrorCodes.PROPERTY_NOT_FOUND,
                    "%s has no property '%s' (%s, var=%s)" % (lbl, prop, ctx, var),
                )
            return None

        # literal token (single token)
        lit = r"(" \
              r"(?:'[^']*')" \
              r"|(?:\"[^\"]*\")" \
              r"|(?:true|false|null)" \
              r"|(?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)" \
              r")"

        # ---------- 1) Comparisons: n.prop <op> lit ----------
        ops = r"(=|<>|!=|<=|>=|<|>)"
        pat_cmp1 = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*%s\s*%s"
            % (ops, lit),
            re.IGNORECASE,
        )
        for m in pat_cmp1.finditer(q):
            var = m.group(1)
            prop = m.group(2)
            value_token = m.group(m.lastindex)

            lbl = get_label(var)
            if not lbl:
                continue

            e = ensure_prop_exists(lbl, prop, var, "WHERE_CMP")
            if e:
                return e
            t = self._type_check_literal("VERTEX", lbl, prop, value_token, "WHERE_CMP")
            if t:
                return t

        # ---------- 2) Comparisons: lit <op> n.prop ----------
        pat_cmp2 = re.compile(
            r"\b%s\s*%s\s*([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)"
            % (lit, ops),
            re.IGNORECASE,
        )
        for m in pat_cmp2.finditer(q):
            value_token = m.group(1)
            var = m.group(3)
            prop = m.group(4)

            lbl = get_label(var)
            if not lbl:
                continue

            e = ensure_prop_exists(lbl, prop, var, "WHERE_CMP_REV")
            if e:
                return e
            t = self._type_check_literal("VERTEX", lbl, prop, value_token, "WHERE_CMP_REV")
            if t:
                return t

        # ---------- 3) IN list: n.prop IN [ ... ] ----------
        # We parse the list elements at top-level commas and require all *obvious literals* match the schema type.
        pat_in = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s+IN\s+\[([^\]]*)\]",
            re.IGNORECASE,
        )
        for m in pat_in.finditer(q):
            var = m.group(1)
            prop = m.group(2)
            list_body = (m.group(3) or "").strip()

            lbl = get_label(var)
            if not lbl:
                continue

            e = ensure_prop_exists(lbl, prop, var, "WHERE_IN")
            if e:
                return e

            # schema expected category
            tname = self.schema.node_prop_type(lbl, prop)
            exp_cat = _expected_category(tname)
            if exp_cat == "ANY":
                continue

            elems = _split_top_level_commas(list_body)
            # if list is empty -> ok
            if not elems:
                continue

            for elem in elems:
                elem = elem.strip()
                cat = _literal_category(elem)
                if cat in ("UNKNOWN", "NULL"):
                    # skip expressions/vars; allow null
                    continue
                # FLOAT accepts INT
                if exp_cat == "FLOAT" and cat == "INT":
                    continue
                if cat != exp_cat:
                    return self._err(
                        SchemaGuardErrorCodes.TYPE_MISMATCH,
                        "Type mismatch (WHERE_IN) for %s.%s: schema expects %s but list element is %s (value=%s)"
                        % (lbl, prop, exp_cat, cat, elem),
                    )

        # ---------- 4) String operators ----------
        # STARTS WITH / ENDS WITH / CONTAINS / =~ require STRING property and STRING literal on RHS (for literal cases).
        def check_string_op(ctx, var, prop, rhs_token):
            lbl = get_label(var)
            if not lbl:
                return None
            e = ensure_prop_exists(lbl, prop, var, ctx)
            if e:
                return e

            # property must be stringy
            exp_cat = _expected_category(self.schema.node_prop_type(lbl, prop))
            if exp_cat != "STRING" and exp_cat != "ANY":
                return self._err(
                    SchemaGuardErrorCodes.TYPE_MISMATCH,
                    "Type mismatch (%s) for %s.%s: operator requires STRING property but schema is %s"
                    % (ctx, lbl, prop, exp_cat),
                )

            # if RHS is an obvious literal, it must be STRING (not int/bool)
            cat = _literal_category(rhs_token)
            if cat in ("UNKNOWN", "NULL"):
                return None
            if cat != "STRING":
                return self._err(
                    SchemaGuardErrorCodes.TYPE_MISMATCH,
                    "Type mismatch (%s) RHS for %s.%s: operator requires STRING literal but got %s (value=%s)"
                    % (ctx, lbl, prop, cat, rhs_token.strip()),
                )
            return None

        pat_starts = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s+STARTS\s+WITH\s+(%s)" % lit,
            re.IGNORECASE,
        )
        for m in pat_starts.finditer(q):
            err = check_string_op("WHERE_STARTS_WITH", m.group(1), m.group(2), m.group(3))
            if err:
                return err

        pat_ends = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s+ENDS\s+WITH\s+(%s)" % lit,
            re.IGNORECASE,
        )
        for m in pat_ends.finditer(q):
            err = check_string_op("WHERE_ENDS_WITH", m.group(1), m.group(2), m.group(3))
            if err:
                return err

        pat_contains = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s+CONTAINS\s+(%s)" % lit,
            re.IGNORECASE,
        )
        for m in pat_contains.finditer(q):
            err = check_string_op("WHERE_CONTAINS", m.group(1), m.group(2), m.group(3))
            if err:
                return err

        # regex match: n.prop =~ "regex"
        # require string property; RHS if literal must be string literal
        pat_regex = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*=~\s+(%s)" % lit,
            re.IGNORECASE,
        )
        for m in pat_regex.finditer(q):
            err = check_string_op("WHERE_REGEX", m.group(1), m.group(2), m.group(3))
            if err:
                return err

        # ---------- 5) IS NULL / IS NOT NULL ----------
        pat_isnull = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s+IS\s+NULL\b",
            re.IGNORECASE,
        )
        for m in pat_isnull.finditer(q):
            var, prop = m.group(1), m.group(2)
            lbl = get_label(var)
            if not lbl:
                continue
            e = ensure_prop_exists(lbl, prop, var, "WHERE_IS_NULL")
            if e:
                return e

        pat_isnotnull = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s+IS\s+NOT\s+NULL\b",
            re.IGNORECASE,
        )
        for m in pat_isnotnull.finditer(q):
            var, prop = m.group(1), m.group(2)
            lbl = get_label(var)
            if not lbl:
                continue
            e = ensure_prop_exists(lbl, prop, var, "WHERE_IS_NOT_NULL")
            if e:
                return e

        return None

    # ---------------- function + dynamic property ----------------

    def _check_functions(self, q):
        candidates = []
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", q):
            name = m.group(1)
            up = name.upper()
            if up in ("MATCH", "WHERE", "RETURN", "WITH", "UNWIND", "OPTIONAL", "CALL"):
                continue
            candidates.append(name)

        for name in candidates:
            if self.caps.is_function_known_not_supported(name):
                return self._err(
                    SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX,
                    "Function not supported by TuGraph: %s()" % name,
                )
            ok = self.caps.is_function_supported(name)
            if (not ok) and self.strict_unknown_functions:
                return self._err(
                    SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX,
                    "Function not supported/unknown in capabilities: %s()" % name,
                )
        return None

    def _check_dynamic_property_access(self, q):
        """
        Reject n["prop"] if pending. Allow arr[0] / arr[i] (list access).
        """
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*([^\]]+?)\s*\]", q):
            inside = (m.group(2) or "").strip()
            if _is_string_literal(inside):
                if self.caps.dynamic_prop_pending:
                    return self._err(
                        SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX,
                        "Dynamic property access via brackets is pending in TuGraph: [%s]" % inside,
                    )
        return None

    # ---------------- utility ----------------

    def _err(self, reason, detail):
        return {"status": "error", "reason": reason, "detail": detail}
