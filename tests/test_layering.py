"""Architectural ratchets for the CLI / HTTP / MCP collapse.

Equality, not subset: green at every commit, red the moment someone adds a
site. Narrow the allowlist in the PR that removes a caller.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "sqldash"

COMPILE_METRIC_ALLOWED = {
    "sqldash/semantics/bind.py",
}

NO_API_IMPORT = (
    ROOT / "params.py",
    ROOT / "lint.py",
    *(ROOT / "semantics").rglob("*.py"),
    *(ROOT / "project").rglob("*.py"),
)

API_IMPORT = re.compile(r"^(?:from sqldash\.api|import sqldash\.api)\b", re.MULTILINE)


def test_compile_metric_call_sites_are_the_allowlist():
    found: set[str] = set()
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == "compile_metric":
                found.add(path.relative_to(ROOT.parent).as_posix())
    assert found == COMPILE_METRIC_ALLOWED


def test_decision_modules_do_not_import_api():
    offenders = []
    for path in NO_API_IMPORT:
        if API_IMPORT.search(path.read_text()):
            offenders.append(path.relative_to(ROOT.parent).as_posix())
    assert offenders == []
