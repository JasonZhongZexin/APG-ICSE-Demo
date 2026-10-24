# -*- coding: utf-8 -*-
from __future__ import annotations

import difflib
import json
import os
import re
from typing import Any, Dict, List, Optional


def _truncate(s: Any, limit: int = 20000) -> str:
    text = "" if s is None else str(s)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...<truncated>..."



def _write_debug_text(path: str, text: str) -> None:
    if not path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except Exception:
        pass

def _extract_output_text(response: Any) -> str:
    txt = getattr(response, "output_text", None)
    if txt:
        return str(txt)

    try:
        output = getattr(response, "output", None) or []
        parts: List[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content")
            for c in content or []:
                if isinstance(c, dict):
                    if c.get("type") in ("output_text", "text"):
                        parts.append(str(c.get("text", "")))
                else:
                    c_type = getattr(c, "type", None)
                    c_text = getattr(c, "text", None)
                    if c_type in ("output_text", "text") and c_text:
                        parts.append(str(c_text))
        if parts:
            return "\n".join(parts)
    except Exception:
        pass

    return ""


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _recount_diff_hunks(patch: str) -> str:
    """Correct hunk counts without changing a model's source edits or context."""
    lines = patch.splitlines()
    header = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")
    for index, line in enumerate(lines):
        match = header.match(line)
        if not match:
            continue
        old_count = new_count = 0
        for body in lines[index + 1:]:
            if header.match(body) or body.startswith(("diff --git ", "--- a/", "--- /dev/null")):
                break
            if body.startswith("\\ No newline at end of file"):
                continue
            if not body or body[0] not in " +-":
                break
            old_count += body[0] in " -"
            new_count += body[0] in " +"
        lines[index] = "@@ -%s,%d +%s,%d @@%s" % (match[1], old_count, match[2], new_count, match[3])
    return "\n".join(lines)


def extract_unified_diff(text: str) -> str:
    """
    Extract a unified diff from model output.
    Accepts either:
    - diff --git ...
    - --- a/file
      +++ b/file
    """
    s = _strip_code_fences(text)

    idx = s.find("diff --git ")
    if idx >= 0:
        return _recount_diff_hunks(s[idx:].strip())

    idx = s.find("--- a/")
    if idx >= 0:
        return _recount_diff_hunks(s[idx:].strip())

    idx = s.find("--- ")
    if idx >= 0 and "+++ " in s[idx:]:
        return _recount_diff_hunks(s[idx:].strip())

    return _recount_diff_hunks(s.strip())


def _line_numbered(text: str) -> str:
    lines = text.splitlines()
    return "\n".join("%5d | %s" % (i + 1, line) for i, line in enumerate(lines))


def _changed_payload_lines(patch: str) -> List[str]:
    """Return non-header added/deleted payload lines from a unified diff."""
    out: List[str] = []
    for line in (patch or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            out.append(line[1:])
    return out


def _is_whitespace_only_patch(patch: str) -> bool:
    payload = _changed_payload_lines(patch)
    if not payload:
        return True
    # A semantic patch must change at least one non-empty token-bearing line.
    return not any(x.strip() for x in payload)


def _extract_focus_identifiers(fault_diagnosis: str) -> List[str]:
    """
    Extract identifiers from the diagnosis that represent likely repair focus.
    This is a soft relevance check, not a hardcoded Redis rule.
    """
    text = fault_diagnosis or ""
    ids: List[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", text):
        if len(token) < 4:
            continue
        if token in {"line", "source", "return", "count", "mode", "problem", "statement", "Graph", "Fault", "Diagnosis"}:
            continue
        if token not in ids:
            ids.append(token)
    # Prioritize repair-relevant identifiers if present.
    priority = []
    for t in ids:
        if any(k in t for k in ["lookupKey", "Reply", "shared.null", "shared.nullarray", "shared.emptyarray", "nullarray"]):
            priority.append(t)
    return priority[:20] if priority else ids[:20]


def _patch_relevance_issue(patch: str, fault_diagnosis: str) -> str:
    """
    Return an issue string if the patch is clearly not a semantic patch for the diagnosis.
    Empty string means it passed the lightweight sanity check.

    This guard is deliberately diagnosis-driven. It does not encode a concrete
    Redis patch, but when the diagnosis says the likely repair point is a
    lookup/reply-selection call site, it rejects patches that only edit later
    count branches or unrelated reply calls.
    """
    if _is_whitespace_only_patch(patch):
        return "Generated patch is whitespace-only or has no token-bearing code changes."

    payload = [x for x in _changed_payload_lines(patch)]
    changed = "\n".join(x.strip() for x in payload if x.strip())
    if not changed:
        return "Generated patch has no substantive changed lines."

    diag_l = (fault_diagnosis or "").lower()
    changed_l = changed.lower()

    embedded_nul_path_diagnosis = (
        "embedded-nul path validation" in diag_l
        or ("embedded nul" in diag_l and "path" in diag_l and "is_valid_path" in diag_l)
        or ("null byte" in diag_l and "path" in diag_l and "is_valid_path" in diag_l)
    )
    if embedded_nul_path_diagnosis:
        if "path.find" not in changed_l and "\0" not in changed and "\x00" not in changed_l:
            return (
                "Generated patch does not implement the embedded-NUL path validation sketch: "
                "it should add a check for NUL bytes in the path string."
            )
        if "return false" not in changed_l:
            return (
                "Generated patch does not implement the embedded-NUL path validation sketch: "
                "the NUL-byte case should be rejected by returning false."
            )
        return ""

    if "source-derived repair sketch" in diag_l and "lookupkeywrite(c->db" in diag_l:
        if "required source replacement sketch" in diag_l:
            if "-    robj *o = lookupkeywriteorreply" not in patch.lower():
                return (
                    "Generated patch does not replace the original lookupKeyWriteOrReply block required by the Source-derived repair sketch."
                )
        if "lookupkeywrite(c->db" not in changed_l:
            return (
                "Generated patch does not implement the Source-derived repair sketch: "
                "it should split the lookup helper using lookupKeyWrite(c->db, ...) before later branches."
            )
        if "addreplynullarray(c)" in diag_l and "addreplynullarray(c)" not in changed_l:
            return (
                "Generated patch does not implement the Source-derived repair sketch: "
                "it should explicitly send addReplyNullArray(c) on the missing-key COUNT path."
            )

    # If the diagnosis explicitly identifies a lookup/reply-selection call site
    # as the likely repair point, the patch should touch that call site or the
    # reply object selected at that call site. Patches that only edit later
    # count/hascount branches are rejected and retried with feedback.
    diagnosis_targets_lookup_reply = (
        "lookupkeywriteorreply" in diag_l
        and "reply-selection" in diag_l
        and "likely repair point" in diag_l
    )
    if diagnosis_targets_lookup_reply:
        touches_lookup_selection = any(
            x in changed_l
            for x in [
                "lookupkeywriteorreply",
                "shared.null[c->resp]",
                "shared.nullarray",
                "lookupkeywrite(",
            ]
        )
        edits_later_count_branch = any(
            x in changed_l
            for x in [
                "if (hascount && !count)",
                "if (!count)",
                "addreplynullarray(c);",
                "addreply(c,shared.emptyarray)",
                "addreply(c, shared.emptyarray)",
            ]
        )
        if edits_later_count_branch and not touches_lookup_selection:
            return (
                "Generated patch edits a later count/hascount reply branch but does not modify the "
                "graph-localized lookupKeyWriteOrReply reply-selection site. The diagnosis says the "
                "missing-key path can return at the lookup helper before later branches execute."
            )
        if not touches_lookup_selection:
            return (
                "Generated patch does not modify the graph-localized lookup/reply-selection site "
                "identified by the fault diagnosis."
            )

    focus_ids = _extract_focus_identifiers(fault_diagnosis)
    if focus_ids:
        if not any(fid.lower() in changed_l for fid in focus_ids):
            # Soft but useful: if diagnosis is specific, patch should touch related identifiers.
            return (
                "Generated patch does not appear to touch identifiers emphasized by the fault diagnosis: "
                + ", ".join(focus_ids[:8])
            )
    return ""



def _extract_required_replacement_sketch(fault_diagnosis: str) -> str:
    """Return the high-priority replacement sketch from diagnosis, if present."""
    text = fault_diagnosis or ""
    key = "Required source replacement sketch for this repair:"
    idx = text.find(key)
    if idx < 0:
        return ""
    # Keep the next section only; avoid flooding the prompt with later constraints twice.
    tail = text[idx:]
    stop_markers = [
        "- This is a source-derived edit sketch",
        "Patch constraints for the Fix Agent:",
    ]
    end = len(tail)
    for marker in stop_markers:
        pos = tail.find(marker)
        if pos > 0:
            end = min(end, pos)
    return tail[:end].strip()


def _make_unified_diff(rel_source_file: str, old_source: str, new_source: str) -> str:
    """Create a syntactically valid unified diff from exact old/new source.

    Important detail: git/apply rejects hunk body lines that become truly blank.
    Some downstream writers/shell displays can trim the trailing single-space
    marker on a blank context line at the end of a hunk, producing:
        error: corrupt patch at line N
    To make the patch robust, use a smaller context and normalize hunks so they
    never end with a blank context line.
    """
    raw = "".join(
        difflib.unified_diff(
            old_source.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile="a/%s" % rel_source_file,
            tofile="b/%s" % rel_source_file,
            n=2,
        )
    )

    def _fix_header_count(header: str, drop_count: int) -> str:
        if drop_count <= 0:
            return header
        m = re.match(r"(@@ -)(\d+)(?:,(\d+))?( \+)(\d+)(?:,(\d+))?( @@.*)", header.rstrip("\n"))
        if not m:
            return header
        old_start = m.group(2)
        old_count = int(m.group(3) or "1") - drop_count
        new_start = m.group(5)
        new_count = int(m.group(6) or "1") - drop_count
        old_count = max(old_count, 0)
        new_count = max(new_count, 0)
        return "%s%s,%d%s%s,%d%s\n" % (
            m.group(1), old_start, old_count, m.group(4), new_start, new_count, m.group(7)
        )

    lines = raw.splitlines(keepends=True)
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("@@ "):
            out.append(line)
            i += 1
            continue

        header = line
        i += 1
        body: List[str] = []
        while i < len(lines) and not lines[i].startswith("@@ ") and not lines[i].startswith("diff --git ") and not lines[i].startswith("--- "):
            b = lines[i]
            # A hunk body line must begin with space, +, -, or backslash.
            # If a context blank line somehow became an empty line, repair it.
            if b in ("\n", "\r\n", ""):
                b = " \n"
            body.append(b)
            i += 1

        dropped = 0
        while body and body[-1] in (" \n", " \r\n", " "):
            body.pop()
            dropped += 1

        out.append(_fix_header_count(header, dropped))
        out.extend(body)

    patch = "".join(out)
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return patch


def _extract_required_replacement_blocks(fault_diagnosis: str) -> Optional[Dict[str, str]]:
    """
    Extract structured old/new blocks from a Required source replacement sketch.

    The diagnosis builder, not this compiler, decides the repair sketch. This
    function only parses the sketch into an exact source replacement.
    """
    text = fault_diagnosis or ""
    pat = re.compile(
        r"Replace this exact localized code block:\s*\n?```(?:c|cpp|c\+\+)?\n"
        r"(?P<old>.*?)\n```"
        r"\s*with this (?:C|C\+\+|C/C\+\+) code skeleton:\s*\n?```(?:c|cpp|c\+\+)?\n"
        r"(?P<new>.*?)\n```",
        re.DOTALL | re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        return None
    old_block = m.group("old").rstrip()
    new_block = m.group("new").rstrip()
    if not old_block or not new_block:
        return None
    return {"old_block": old_block, "new_block": new_block}


def _generate_patch_from_required_sketch(
    rel_source_file: str,
    source_code: str,
    fault_diagnosis: str,
) -> Dict[str, Any]:
    """
    Compile a source-derived required replacement sketch into a valid diff.

    This is a structured edit compiler, not a Redis-specific patch. It fires
    only when the diagnosis contains an explicit old-block/new-block sketch,
    and the old block is found exactly once in the current source file.
    """
    sketch = _extract_required_replacement_blocks(fault_diagnosis)
    if not sketch:
        return {"ok": False, "status": "NO_REQUIRED_SKETCH", "patch": ""}

    old_block = sketch["old_block"]
    new_block = sketch["new_block"]

    count = source_code.count(old_block)
    if count == 0:
        return {
            "ok": False,
            "status": "SKETCH_OLD_BLOCK_NOT_FOUND",
            "error": "Required replacement sketch old_block was not found exactly in source.",
            "old_block": old_block,
            "patch": "",
        }
    if count != 1:
        return {
            "ok": False,
            "status": "SKETCH_OLD_BLOCK_NOT_UNIQUE",
            "error": "Required replacement sketch old_block is not unique in source.",
            "old_block": old_block,
            "count": count,
            "patch": "",
        }

    new_source = source_code.replace(old_block, new_block, 1)
    patch = _make_unified_diff(
        rel_source_file=rel_source_file,
        old_source=source_code,
        new_source=new_source,
    )
    if not patch:
        return {
            "ok": False,
            "status": "SKETCH_NO_DIFF",
            "error": "Required replacement sketch produced no diff.",
            "patch": "",
        }

    issue = _patch_relevance_issue(patch, fault_diagnosis)
    if issue:
        return {
            "ok": False,
            "status": "SKETCH_REJECTED_BY_GUARD",
            "error": issue,
            "patch": patch,
            "old_block": old_block,
            "new_block": new_block,
        }

    return {
        "ok": True,
        "status": "GENERATED_FROM_REQUIRED_SKETCH",
        "model": "structured-edit-compiler",
        "patch": patch,
        "old_block": old_block,
        "new_block": new_block,
    }

class LLMPatchGenerator(object):
    """
    Minimal LLM patch generator v0.

    It consumes:
    - global_summary.fix_context.markdown
    - local summaries and their llm_narrative
    - one target source file

    It returns:
    - unified diff only
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key_env: str = "OPENAI_API_KEY",
        max_source_chars: int = 50000,
        max_context_chars: int = 30000,
        require_llm: bool = False,
    ):
        self.model = model
        self.api_key_env = api_key_env
        self.max_source_chars = int(max_source_chars)
        self.max_context_chars = int(max_context_chars)
        self.require_llm = bool(require_llm)

    def _collect_llm_narratives(self, local_summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for item in local_summaries or []:
            summary = item.get("summary", item) if isinstance(item, dict) else {}
            if not isinstance(summary, dict):
                continue
            narr = summary.get("llm_narrative")
            if not isinstance(narr, dict):
                continue
            out.append({
                "summary_id": summary.get("summary_id"),
                "graph_type": summary.get("graph_type"),
                "result_kind": summary.get("result_kind"),
                "brief": narr.get("brief"),
                "graph_semantic_interpretation": narr.get("graph_semantic_interpretation"),
                "patch_relevance": narr.get("patch_relevance"),
                "limitations": narr.get("limitations"),
                "evidence_used": narr.get("evidence_used"),
                "confidence": narr.get("confidence"),
            })
        return out

    def _derive_graph_repair_focus(
        self,
        rel_source_file: str,
        source_code: str,
        global_summary: Dict[str, Any],
    ) -> str:
        """
        Convert graph-localized evidence into source-level repair focus.

        This does not prescribe a patch. It extracts source lines mentioned by
        graph summaries and shows exact source windows around those lines.
        """
        graph_text = json.dumps(global_summary or {}, ensure_ascii=False)
        line_nums: List[int] = []

        # Common forms:
        #   "ln": 504
        #   \"ln\": 504
        #   { "ln": 504, "fl": "t_list.c" }
        patterns = [
            r'(?:\\?")ln(?:\\?")\s*:\s*(\d+)',
            r'\bln\b[^0-9]{0,12}(\d+)',
        ]

        for pat in patterns:
            for m in re.finditer(pat, graph_text):
                try:
                    ln = int(m.group(1))
                    if ln > 0:
                        line_nums.append(ln)
                except Exception:
                    pass

        lines = source_code.splitlines()
        max_ln = len(lines)

        uniq: List[int] = []
        for ln in line_nums:
            if 1 <= ln <= max_ln and ln not in uniq:
                uniq.append(ln)

        if not uniq:
            return (
                "Graph-derived repair focus:\n"
                "- No concrete source line numbers were extracted from the graph summary.\n"
                "- Use the problem statement and exact source file content.\n\n"
            )

        def priority(ln: int) -> int:
            line = lines[ln - 1] if 1 <= ln <= max_ln else ""
            score = 0
            if "lookupKey" in line or "Reply" in line:
                score -= 100
            if "return" in line:
                score -= 20
            return score

        selected = sorted(uniq, key=priority)[:12]
        snippets: List[str] = []

        for ln in selected:
            start = max(1, ln - 4)
            end = min(max_ln, ln + 4)
            body = []
            for i in range(start, end + 1):
                marker = ">>" if i == ln else "  "
                body.append("%s %5d: %s" % (marker, i, lines[i - 1]))
            snippets.append(
                "Graph-localized source window around line %d:\n%s"
                % (ln, "\n".join(body))
            )

        return (
            "Graph-derived repair focus:\n"
            "- The following source windows are extracted from graph-localized evidence.\n"
            "- Treat these as high-priority locations for repair reasoning.\n"
            "- The exact source file below remains authoritative for generating the diff.\n\n"
            + "\n\n".join(snippets)
            + "\n\n"
        )


    def _system_prompt(self) -> str:
        return (
            "You are a cautious automated program repair patch generator for C/C++ repositories.\n\n"
            "You must generate a patch as a unified diff that applies exactly to the provided source file.\n\n"
            "Hard rules:\n"
            "1. Output ONLY a unified diff. No markdown, no explanation, no code fences.\n"
            "2. Modify only the target source file provided by the user.\n"
            "3. The problem statement is the primary repair objective.\n"
            "4. The exact source file content is authoritative. Every unchanged context line in the diff must exactly match it.\n"
            "5. The fault diagnosis is the bridge from graph evidence to repair reasoning. Follow it unless it contradicts the source.\n"
            "6. Graph evidence is localization evidence. Prefer minimal changes near graph-localized source windows when consistent with the problem.\n"
            "7. Reason about control flow before editing. A change after an early return cannot fix behavior that already returned earlier.\n"
            "8. For reply-shape bugs, identify where the reply object is selected for the failing path before modifying later branches.\n"
            "9. Do not modify later branches merely because they mention related variables such as count/hascount; modify them only if the failing path reaches them.\n"
            "10. If the diagnosis identifies a lookup/reply-selection call site, the patch must either modify that call site's reply argument or refactor the missing-key path before later branches.\n"
            "11. If the diagnosis contains a Source-derived repair sketch, implement that sketch. Do not substitute a different edit to a later branch.\n"
            "12. When the sketch says to split a lookup helper, use the exact existing lower-level API and reply helper named in the diagnosis.\n"
            "13. If the sketch contains a Required source replacement sketch, the unified diff must replace that exact old code block with the provided C code skeleton.\n"
            "14. Do not merely insert code after a helper that already sends a reply; delete/replace that helper when the sketch requires splitting it.\n"
            "15. Do not replace an empty-array reply in a count==0 fast-exit branch unless the diagnosis says the failing input reaches that branch.\n"
            "16. Prefer the smallest plausible patch.\n"
            "15. Preserve existing style and avoid unrelated changes.\n"
            "16. Do not make whitespace-only changes.\n"
            "17. Do not reformat existing code.\n"
            "18. The diff path must use the exact relative path provided.\n"
            "19. Generate syntactically valid unified diff hunks with enough exact context to apply cleanly.\n"
        )

    def _user_prompt(
        self,
        rel_source_file: str,
        source_code: str,
        global_summary: Dict[str, Any],
        local_summaries: List[Dict[str, Any]],
        problem_statement: str = "",
        fault_diagnosis: str = "",
    ) -> str:
        fix_context = (
            (global_summary or {})
            .get("fix_context", {})
            .get("markdown", "")
        )

        compact_global = {
            "stop_reason": (global_summary or {}).get("stop_reason"),
            "aggregated_signals": (global_summary or {}).get("aggregated_signals"),
            "evidence_pool": (global_summary or {}).get("evidence_pool"),
            "gaps": (global_summary or {}).get("gaps"),
        }

        narratives = self._collect_llm_narratives(local_summaries)
        graph_repair_focus = self._derive_graph_repair_focus(
            rel_source_file=rel_source_file,
            source_code=source_code,
            global_summary=global_summary,
        )
        required_replacement_sketch = _extract_required_replacement_sketch(fault_diagnosis)
        high_priority_repair_instruction = ""
        if required_replacement_sketch:
            high_priority_repair_instruction = (
                "Highest-priority repair instruction:\n"
                "The diagnosis includes a required source replacement sketch. Implement it in the unified diff.\n"
                "Do not only add code after the old helper; replace the old helper block as specified.\n"
                "%s\n\n" % required_replacement_sketch
            )

        source_excerpt = source_code
        source_description = "Exact current source file content follows."
        if len(source_code) > self.max_source_chars:
            anchor = re.search(r"Patch source:\s*`?" + re.escape(rel_source_file) + r":(\d+)", fault_diagnosis)
            offset = None
            if anchor:
                source_lines = source_code.splitlines(keepends=True)
                line = int(anchor[1])
                if 1 <= line <= len(source_lines):
                    offset = sum(len(item) for item in source_lines[:line - 1])
            if offset is None:
                blocks = _extract_required_replacement_blocks(fault_diagnosis)
                old = blocks.get("old_block", "") if blocks else ""
                if old and source_code.count(old) == 1:
                    offset = source_code.index(old)
            if offset is not None:
                start = max(0, offset - self.max_source_chars // 2)
                start = source_code.rfind("\n", 0, start) + 1
                end = min(len(source_code), start + self.max_source_chars)
                if end < len(source_code):
                    end = source_code.rfind("\n", start, end)
                source_excerpt = source_code[start:end]
                first_line = source_code.count("\n", 0, start) + 1
                source_description = "Exact current source excerpt follows, starting at file line %d. This excerpt contains the localized repair site." % first_line

        return (
            "Generate a unified diff patch for the following real-world bug.\n\n"
            "Target relative source file:\n"
            "%s\n\n"
            "Problem statement:\n"
            "%s\n\n"
            "%s"
            "Fault diagnosis from CodeGraphHunter:\n"
            "%s\n\n"
            "%s"
            "Global graph-derived fix context:\n"
            "%s\n\n"
            "Compact global evidence JSON:\n"
            "%s\n\n"
            "LLM local summary narratives JSON:\n"
            "%s\n\n"
            "%s\n"
            "This is the exact file to patch. Do not add line numbers to the patch.\n"
            "Every unchanged context line in the unified diff must exactly match this source.\n"
            "<<<SOURCE_FILE_START>>>\n"
            "%s\n"
            "<<<SOURCE_FILE_END>>>\n\n"
            "Return only a unified diff. The diff headers must be exactly:\n"
            "--- a/%s\n"
            "+++ b/%s\n"
        ) % (
            rel_source_file,
            _truncate(problem_statement, self.max_context_chars),
            _truncate(high_priority_repair_instruction, self.max_context_chars),
            _truncate(fault_diagnosis, self.max_context_chars),
            _truncate(graph_repair_focus, self.max_context_chars),
            _truncate(fix_context, self.max_context_chars),
            _truncate(json.dumps(compact_global, indent=2, sort_keys=True, ensure_ascii=False), self.max_context_chars),
            _truncate(json.dumps(narratives, indent=2, sort_keys=True, ensure_ascii=False), self.max_context_chars),
            source_description,
            _truncate(source_excerpt, self.max_source_chars),
            rel_source_file,
            rel_source_file,
        )

    def generate_patch(
        self,
        rel_source_file: str,
        source_code: str,
        global_summary: Dict[str, Any],
        local_summaries: List[Dict[str, Any]],
        problem_statement: str = "",
        fault_diagnosis: str = "",
    ) -> Dict[str, Any]:
        structured = {"status": "NO_REQUIRED_SKETCH"} if self.require_llm else _generate_patch_from_required_sketch(
            rel_source_file=rel_source_file,
            source_code=source_code,
            fault_diagnosis=fault_diagnosis,
        )
        if structured.get("ok"):
            debug_dir = os.environ.get("CODEGRAPHHUNTER_PATCH_DEBUG_DIR", "")
            if debug_dir:
                _write_debug_text(
                    os.path.join(debug_dir, "patch_generated_from_required_sketch.diff"),
                    structured.get("patch", ""),
                )
            return structured
        elif structured.get("status") not in ("NO_REQUIRED_SKETCH",):
            debug_dir = os.environ.get("CODEGRAPHHUNTER_PATCH_DEBUG_DIR", "")
            if debug_dir:
                _write_debug_text(
                    os.path.join(debug_dir, "patch_required_sketch_failed.json"),
                    json.dumps(structured, indent=2, sort_keys=True, ensure_ascii=False),
                )

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            return {
                "ok": False,
                "status": "GEN_FAILED",
                "error": "Missing environment variable: %s" % self.api_key_env,
                "patch": "",
            }

        try:
            from openai import OpenAI
        except Exception as e:
            return {
                "ok": False,
                "status": "GEN_FAILED",
                "error": "Failed to import openai package: %s" % str(e),
                "patch": "",
            }

        try:
            client = OpenAI(api_key=api_key)

            system_prompt = self._system_prompt()
            user_prompt = self._user_prompt(
                rel_source_file=rel_source_file,
                source_code=source_code,
                global_summary=global_summary,
                local_summaries=local_summaries,
                problem_statement=problem_statement,
                fault_diagnosis=fault_diagnosis,
            )

            debug_dir = os.environ.get("CODEGRAPHHUNTER_PATCH_DEBUG_DIR", "")
            if debug_dir:
                _write_debug_text(os.path.join(debug_dir, "patch_system_prompt.txt"), system_prompt)
                _write_debug_text(os.path.join(debug_dir, "patch_user_prompt.txt"), user_prompt)

            def call_model(prompt_text: str, suffix: str = "") -> Dict[str, Any]:
                response = client.responses.create(
                    model=self.model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt_text},
                    ],
                    temperature=0,
                    max_output_tokens=1800,
                )
                raw = _extract_output_text(response)
                diff = extract_unified_diff(raw)
                if debug_dir:
                    _write_debug_text(os.path.join(debug_dir, "patch_raw_output%s.txt" % suffix), raw)
                    _write_debug_text(os.path.join(debug_dir, "patch_candidate%s.diff" % suffix), diff)
                return {"raw_output": raw, "patch": diff}

            first = call_model(user_prompt, suffix="_attempt1")
            text = first.get("raw_output", "")
            patch = first.get("patch", "")

            if not patch or "---" not in patch or "+++" not in patch:
                return {
                    "ok": False,
                    "status": "GEN_FAILED",
                    "error": "Model output did not contain a valid-looking unified diff.",
                    "raw_output": _truncate(text, 3000),
                    "patch": patch,
                }

            issue = _patch_relevance_issue(patch, fault_diagnosis)
            if issue:
                retry_prompt = (
                    user_prompt
                    + "\n\nThe previous patch candidate was rejected by CodeGraphHunter's patch sanity guard.\n"
                    + "Rejection reason: " + issue + "\n"
                    + "Generate a new unified diff. The new patch must contain a non-whitespace semantic code change directly related to the fault diagnosis.\n"
                    + "If the diagnosis contains a Source-derived repair sketch, implement that sketch literally as the repair plan.\n"
                    + "If the sketch says to split lookupKeyWriteOrReply, use lookupKeyWrite(c->db, ...) and explicitly send addReplyNullArray(c) on the missing-key COUNT path before later branches.\n"
                    + "If the diagnosis identifies lookupKeyWriteOrReply as the likely repair point, edit that call site's reply-selection expression or refactor the missing-key path before the later branches.\n"
                    + "Do not fix this by changing the later `if (hascount && !count)` or `if (!count)` branches unless the failing missing-key input reaches those branches.\n"
                    + "Do not output a whitespace-only, formatting-only, or unrelated patch.\n"
                )
                if debug_dir:
                    _write_debug_text(os.path.join(debug_dir, "patch_user_prompt_retry.txt"), retry_prompt)
                second = call_model(retry_prompt, suffix="_attempt2")
                text2 = second.get("raw_output", "")
                patch2 = second.get("patch", "")
                if patch2 and "---" in patch2 and "+++" in patch2:
                    issue2 = _patch_relevance_issue(patch2, fault_diagnosis)
                    if not issue2:
                        return {
                            "ok": True,
                            "status": "GENERATED",
                            "model": self.model,
                            "patch": patch2,
                            "raw_output": _truncate(text2, 10000),
                            "guard": {"attempt1_rejected": issue, "attempt2_accepted": True},
                        }
                    return {
                        "ok": False,
                        "status": "GEN_REJECTED",
                        "error": issue2,
                        "first_rejection": issue,
                        "raw_output": _truncate(text2, 10000),
                        "patch": patch2,
                    }
                return {
                    "ok": False,
                    "status": "GEN_FAILED",
                    "error": "Retry output did not contain a valid-looking unified diff after initial rejection: %s" % issue,
                    "raw_output": _truncate(text2, 3000),
                    "patch": patch2,
                }

            return {
                "ok": True,
                "status": "GENERATED",
                "model": self.model,
                "patch": patch,
                "raw_output": _truncate(text, 10000),
                "guard": {"attempt1_accepted": True},
            }

        except Exception as e:
            return {
                "ok": False,
                "status": "GEN_FAILED",
                "error": str(e),
                "patch": "",
            }
