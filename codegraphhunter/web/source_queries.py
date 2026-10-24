"""Bounded source queries confined to the server-configured source repository."""

import re
import shutil
import subprocess
from pathlib import Path

from codegraphhunter.localization.agentic_fault_localizer import AgenticFaultLocalizer


class SourceQueries:
    def __init__(self, root, check):
        self.root, self.check = Path(root).resolve(), check
        self._files = self._functions = None
        self.complete = True

    def path(self, file):
        path = (self.root / file).resolve()
        if self.root not in path.parents or not path.is_file() or path.suffix.lower() not in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}:
            raise ValueError("Choose a C/C++ source file inside the configured source repository.")
        return path

    def files(self):
        if self._files is None:
            if not self.root.is_dir():
                raise ValueError("The configured source repository is unavailable.")
            if shutil.which("rg"):
                result = subprocess.run(["rg", "--files", "-g", "*.{c,cc,cpp,cxx,h,hh,hpp,hxx}",
                                         "-g", "!build*", "-g", "!third_party", "-g", "!vendor"], cwd=self.root,
                                        capture_output=True, text=True, timeout=5, check=False)
                files = sorted(result.stdout.splitlines())
            else:
                files = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")
                               if path.suffix.lower() in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})
            self.complete = len(files) <= 500
            self._files = files[:500]
        return self._files

    def read(self, file):
        self.check()
        path = self.path(file)
        if path.stat().st_size > 4 * 1024 * 1024:
            self.complete = False
            return ""
        return path.read_text(errors="replace")

    def functions(self):
        if self._functions is None:
            parser = AgenticFaultLocalizer(repo_root=str(self.root), problem_statement="", bolt_router=None)
            self._functions = []
            for file in self.files():
                self.check()
                self._functions.extend(parser._extract_c_functions(file, self.read(file)))
        return self._functions

    def search(self, text, limit, file=""):
        terms = [term.lower() for term in re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", text)]
        ignored = {"find", "show", "source", "function", "functions", "where", "which", "the", "and", "for", "can", "that", "with", "does", "this", "what", "code"}
        terms = [term for term in terms if term not in ignored]
        matches = []
        for function in self.functions():
            if file and str(function["source_file"]) != file:
                continue
            name = function["function"].lower()
            body = function.get("body", "").lower()
            score = sum((12 if term in name else 2 if term in body else 0) for term in terms)
            if terms and not score:
                continue
            matches.append(dict(function=function["function"], file=function["source_file"],
                                start_line=function["start_line"], end_line=function["end_line"],
                                signature=function.get("signature", ""), source_score=score))
        return sorted(matches, key=lambda row: (-row["source_score"], row["file"], row["start_line"]))[:limit]

    def window(self, file, line, context=12):
        lines = self.read(file).splitlines()
        if not 1 <= line <= len(lines):
            raise ValueError("The source line is outside the selected file.")
        start, end = max(1, line - context), min(len(lines), line + context)
        return dict(file=file, start_line=start, end_line=end,
                    source="\n".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, end)))

    def usages(self, keyword, limit, file=""):
        if not keyword:
            raise ValueError("Specify a literal keyword, API, or symbol to find.")
        rows = []
        for name in ([file] if file else self.files()):
            for number, line in enumerate(self.read(name).splitlines(), 1):
                self.check()
                if keyword in line:
                    rows.append(dict(file=name, line=number, source=line))
                    if len(rows) >= limit:
                        self.complete = False
                        return rows
        return rows
