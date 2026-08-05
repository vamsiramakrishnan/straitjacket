"""Acceptance: a budget-truncated retrieval still says how to get the rest.

The digest side of this was fixed with the rest of the errors/output audit.
The audit claimed the retrieval path already passed a continuation always —
it does not. `_retrieval/get.py` sets one only when *it* clips a line span; when
`bounded()` cuts on the token budget instead, the result ended at the bare
`[ctx:truncated ...]` note with no address anywhere in it. That is the worst
case to lose it: the output was too big, so the reader most needs the way back.
"""

from __future__ import annotations

from conftest import make_ws

from ctx._retrieval.get import Selector
from ctx.store import Store


def _tiny_budget(root, tokens: int = 40):
    """Budgets are frozen config, so set them the way a user would."""
    (root / "ctx.toml").write_text(
        f"version = 1\n[budgets]\nresult_tokens = {tokens}\n", encoding="utf-8")
    return make_ws(root)


def _big_file(root, name="big.txt", lines=400):
    p = root / name
    p.write_text("".join(f"line {i} with enough text to cost tokens\n" for i in range(lines)),
                 encoding="utf-8")
    return p


def test_budget_truncated_get_still_carries_a_handle(state_home, git_workspace):
    from ctx._retrieval.get import get

    ws = _tiny_budget(git_workspace)
    _big_file(ws.root)
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)

    out = get(store, ws, "repo:big.txt", Selector())

    assert "ctx:truncated" in out, "expected the budget to actually cut; test is not exercising it"
    # The address must come AFTER the cut. Asserting on the whole body would be
    # vacuous: every digest opens with a `[ctx get <ref>]` header, so the verb
    # is present whether or not a usable handle survived the truncation.
    tail = out.split("[ctx:truncated", 1)[1]
    assert "ctx get" in tail, f"nothing after the truncation note points at the rest:\n{out}"


def test_budget_truncated_search_still_carries_a_handle(state_home, git_workspace):
    from ctx._retrieval.search import search

    ws = _tiny_budget(git_workspace)
    _big_file(ws.root)
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)

    out = search(store, ws, "repo:big.txt", ["line"])

    assert "ctx:truncated" in out
    # search already supplies its own `next:` continuation, so this one was
    # never broken — it is here so a future change cannot quietly drop it.
    tail = out.split("[ctx:truncated", 1)[1]
    assert "ctx " in tail, f"truncated search has no way back to the rest:\n{out}"


def test_an_untruncated_result_is_unchanged(state_home, git_workspace):
    """The handle rides along only on an actual cut — a result that fits must
    stay byte-identical, or every small retrieval grows a pointless line."""
    from ctx._retrieval.get import get

    ws = make_ws(git_workspace)
    (ws.root / "small.txt").write_text("one\ntwo\n", encoding="utf-8")
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)

    out = get(store, ws, "repo:small.txt", Selector())

    assert "ctx:truncated" not in out
    assert not out.rstrip().endswith("ctx get repo:small.txt")
