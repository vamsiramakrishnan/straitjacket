"""No model-visible text is clipped with a raw slice.

Clipping a line to a display width and redacting afterwards leaves a secret
that straddles the cut as a prefix too short for its pattern to match. Every
cut of text that can reach the model goes through ``ctx.textutil``'s clip
helpers (``clip_redacting`` / ``elide_redacting``), which judge the text
around the cut first. This scan keeps it that way: it flags every raw
``X[:N]`` / ``X[-N:]`` slice that looks like text -- inside an f-string,
applied to ``str()`` / ``.strip()`` / ``" ".join()`` / ``json.dumps()`` and
friends, followed by ``+ "…"``, or on a variable named like text -- unless
it is listed below with the reason it is not model-visible payload text.

The allow-list lives in tests/raw_clip_allowlist.py: (file, the slice's
source text) -> reason. Line numbers are not used, so edits elsewhere do not
churn it.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "ctx"

TEXT_CALLS = {"str", "strip", "rstrip", "lstrip", "join", "decode", "dumps", "format",
              "replace", "lower", "upper", "splitlines", "repr"}
TEXT_NAMES = {"line", "ln", "lj", "raw", "text", "msg", "message", "summary", "detail",
              "desc", "content", "note", "subject", "sample", "tpl", "template", "err",
              "err_first", "fallback", "cmd", "body", "title", "signature", "sig", "entry",
              "label", "value", "cell", "s", "t", "flat", "evidence", "stderr", "task",
              "query", "prompt", "argv", "out"}
SMALL = 16  # [:12] short ids, [:8] tags, [:1] first char ...

from raw_clip_allowlist import ALLOWED  # noqa: E402  (file, slice) -> reason


def _src(node: ast.AST, text: str) -> str:
    return ast.get_source_segment(text, node) or ""


def _is_text_slice(node: ast.Subscript, parents: dict) -> bool:
    sl = node.slice
    if not isinstance(sl, ast.Slice) or sl.step is not None:
        return False
    head = sl.lower is None and sl.upper is not None
    tail = sl.upper is None and isinstance(sl.lower, ast.UnaryOp) and isinstance(sl.lower.op, ast.USub)
    if not (head or tail):
        return False
    bound = sl.upper if head else sl.lower.operand
    if isinstance(bound, ast.Constant) and isinstance(bound.value, int) and bound.value <= SMALL:
        return False
    v = node.value
    p = parents.get(node)
    while p is not None and not isinstance(p, (ast.stmt, ast.JoinedStr)):
        p = parents.get(p)
    if isinstance(p, ast.JoinedStr):
        return True
    if isinstance(v, ast.Call):
        f = v.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in TEXT_CALLS:
            return True
    up = parents.get(node)
    if isinstance(up, ast.BinOp) and isinstance(up.op, ast.Add) and any(
            isinstance(x, ast.Constant) and isinstance(x.value, str) for x in (up.left, up.right)):
        return True
    name = v.id if isinstance(v, ast.Name) else (v.attr if isinstance(v, ast.Attribute) else "")
    return name in TEXT_NAMES


def _scan() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "_vendor" in path.parts:
            continue
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and _is_text_slice(node, parents):
                found[(rel, _src(node, text))] = node.lineno
    return found


def test_every_text_clip_goes_through_the_clip_helper():
    found = _scan()
    unexplained = {f"{k[0]}:{line}: {k[1]}" for k, line in found.items() if k not in ALLOWED}
    assert not unexplained, (
        "raw slices of what looks like model-visible text; use "
        "textutil.clip_redacting / elide_redacting, or add an ALLOWED entry "
        "with the reason it is not payload text:\n  " + "\n  ".join(sorted(unexplained)))


def test_allow_list_has_no_dead_entries():
    found = _scan()
    dead = [k for k in ALLOWED if k not in found]
    assert not dead, f"ALLOWED entries that no longer match a slice: {dead}"
