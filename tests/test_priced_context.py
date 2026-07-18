"""Acceptance: priced context (docs/PRICED-CONTEXT.md) — every expensive
choice carries a token price at decision time, and degraded reads offer a
priced structured menu instead of a truncated head."""

import json
import textwrap

import pytest

PYFILE = textwrap.dedent('''\
    """Module docstring."""


    def small():
        return 1


    class Widget:
        def render(self):
            return "w" * 10

        def resize(self, n):
            self.n = n


    def large():
        parts = []
        for i in range(10):
            parts.append(str(i))
        return "".join(parts)
    ''')


# ------------------------------------------------------------ formatting
def test_fmt_tokens_coarse_buckets():
    from ctx.textutil import fmt_tokens_coarse

    assert fmt_tokens_coarse(37) == "~50"
    assert fmt_tokens_coarse(880) == "~900"
    assert fmt_tokens_coarse(8_432) == "~8k"
    assert fmt_tokens_coarse(22_100) == "~20k"
    assert fmt_tokens_coarse(347_595) == "~350k"
    # Deterministic and never falsely precise.
    assert fmt_tokens_coarse(8_432) == fmt_tokens_coarse(8_432)
    assert "8432" not in fmt_tokens_coarse(8_432)


# ------------------------------------------------- M1: guard price tags
@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return d


def test_oversized_read_reason_carries_price_and_menu(ws):
    from ctx.hook import classify

    big = ws / "big.py"
    big.write_text("x = 1\n" * 20000, encoding="utf-8")  # 120 KB ≈ 30k tok
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(big)},
        "cwd": str(ws),
    }
    decision = classify(payload)
    reason = decision.get("rewrite", {}).get("reason") or decision.get("reason", "")
    assert "~30k tok" in reason  # the price, coarse
    assert "priced symbol outline" in reason  # the menu
    assert "ctx stats repo:" in reason


def test_price_relativized_to_window_when_proxy_present(ws):
    from ctx.hook import classify

    proxy = ws / ".ctx-session-reads" / "proxy"
    proxy.mkdir(parents=True)
    (proxy / "window.json").write_text(
        json.dumps({"window_pct": 10.0, "context_limit": 200_000}), encoding="utf-8"
    )
    big = ws / "big.txt"
    big.write_text("y" * 100_000, encoding="utf-8")  # 25k tok = 12-13% of 200k
    decision = classify(
        {"tool_name": "Read", "tool_input": {"file_path": str(big)}, "cwd": str(ws)}
    )
    reason = decision.get("rewrite", {}).get("reason") or decision.get("reason", "")
    assert "% of window" in reason
    # Non-python file: no outline hint (menu only where a menu exists).
    assert "priced symbol outline" not in reason


# --------------------------------------------- M2: priced symbol outline
def test_stats_single_py_file_returns_priced_outline(state_home, workspace_dir):
    from ctx.retrieval import stats
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    (workspace_dir / "widget.py").write_text(PYFILE, encoding="utf-8")
    ws = resolve_workspace(str(workspace_dir))
    store = Store(ws.workspace_id)
    out = stats(store, ws, "repo:widget.py")
    assert "[ctx stats repo:widget.py]" in out
    assert "outline (priced):" in out
    assert "def small L4-5" in out
    assert "class Widget L8-" in out
    assert "def Widget.render L9-10" in out  # methods dotted, nested indent
    for line in out.splitlines():
        if " L" in line and "span" in line:
            assert " tok · span " in line  # every entry priced + addressable
    assert "ctx get repo:widget.py --symbol" in out


def test_outline_is_deterministic_and_span_backed(state_home, workspace_dir):
    from ctx.retrieval import Selector, get, stats
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    (workspace_dir / "widget.py").write_text(PYFILE, encoding="utf-8")
    ws = resolve_workspace(str(workspace_dir))
    store = Store(ws.workspace_id)
    a = stats(store, ws, "repo:widget.py")
    b = stats(store, ws, "repo:widget.py")
    assert a == b
    # A minted span resolves back to the exact symbol region.
    span_id = next(
        ln.rsplit("span ", 1)[1] for ln in a.splitlines() if "def small" in ln
    )
    zoom = get(store, ws, "repo:widget.py", Selector(span=span_id))
    assert "def small" in zoom
    assert "return 1" in zoom


def test_stats_directory_still_aggregates(state_home, workspace_dir):
    from ctx.retrieval import stats
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    (workspace_dir / "widget.py").write_text(PYFILE, encoding="utf-8")
    ws = resolve_workspace(str(workspace_dir))
    out = stats(Store(ws.workspace_id), ws, "repo:")
    assert "files (exact):" in out  # repo aggregate path unchanged
    assert "outline (priced):" not in out


# ------------------------------------------------- M3: priced map entries
def test_map_survivors_carry_price_and_def_count(state_home, workspace_dir):
    from ctx.repomap import repo_map
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    (workspace_dir / "widget.py").write_text(PYFILE, encoding="utf-8")
    ws = resolve_workspace(str(workspace_dir))
    out = repo_map(Store(ws.workspace_id), ws, budget=400)
    entry = next(ln for ln in out.splitlines() if ln.startswith("repo:widget.py"))
    assert " tok · " in entry
    assert entry.rstrip().split("·")[-1].strip().endswith("d")  # def count
