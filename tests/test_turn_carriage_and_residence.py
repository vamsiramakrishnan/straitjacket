"""Tests for turn-carriage inline/retrieval sizing, non-git child isolation, and WatermarkResidence."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema

from conftest import make_store, make_ws
from ctx._retrieval.get import Selector, _effective_result_tokens, get
from ctx.execution import generation_hash, run_capture
from ctx.proxywindow import window_path
from ctx.residence import WatermarkResidence
from ctx.toolresult import _command_intent, _inline_thresholds, render_command_result

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "spec/schemas/command-result-v1.schema.json").read_text(
        encoding="utf-8"
    )
)


def _write_window(ws_root: Path, doc: dict) -> None:
    p = Path(window_path(ws_root))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc), encoding="utf-8")


# ------------------------------------------------------------ command intent & inline delivery
def test_command_intent_classifies_projections_floods_and_standard():
    assert _command_intent("python3 -c 'print(1)'") == "projection"
    assert _command_intent("cd sub && jq '.items' data.json") == "projection"
    assert _command_intent("cat rows.jsonl | head -n 20") == "projection"
    assert _command_intent('bash -lc "python -c \'import json\'"') == "projection"

    assert _command_intent("ps aux") == "flood"
    assert _command_intent("git log -n 50") == "flood"
    assert _command_intent("git diff HEAD~1") == "flood"
    assert _command_intent("find . -maxdepth 4") == "flood"
    assert _command_intent("ls -la /tmp") == "flood"

    assert _command_intent("pytest tests/test_bounds.py -q") == "standard"
    assert _command_intent("") == "standard"


def test_inline_passthrough_honours_16k_default_and_projection_ceiling(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    try:
        # 10 KiB standard output (> 4,800 B, <= 16,384 B) stays inline rather than
        # tripping a 4,800-byte result_tokens*4 wire clamp.
        cap_10k = run_capture(
            ws,
            ["sh", "-c", f"{sys.executable} -c \"print('a' * 100 * 100)\""],
            record_argv=["my-tool", "--check"],
            store=store,
        )
        res_10k = render_command_result(ws, store, cap_10k)
        jsonschema.validate(res_10k.as_dict(), SCHEMA)
        assert res_10k.delivery == "inline"
        assert len(res_10k.stdout) == 10_001

        # 20 KiB projection output (python3 -c) stays inline under the 24 KiB projection floor.
        cap_proj = run_capture(
            ws,
            [sys.executable, "-c", "for i in range(200): print(f'row-{i:03d}: ' + 'x' * 90)"],
            store=store,
        )
        res_proj = render_command_result(ws, store, cap_proj)
        jsonschema.validate(res_proj.as_dict(), SCHEMA)
        assert res_proj.delivery == "inline"
        assert "row-199:" in res_proj.stdout

        # 12 KiB flood command (recorded as `git log`) is digested above the 8 KiB flood cap.
        cap_flood = run_capture(
            ws,
            [sys.executable, "-c", "for i in range(150): print(f'commit {i:04d} ' + 'y' * 75)"],
            record_argv=["git", "log"],
            store=store,
        )
        res_flood = render_command_result(ws, store, cap_flood)
        jsonschema.validate(res_flood.as_dict(), SCHEMA)
        assert res_flood.delivery == "digest"
        # First truncated digest in an auto-engagement workspace preserves its `next:` hint.
        assert "next:" in res_flood.digest
    finally:
        store.close()


def test_inline_thresholds_scale_with_carriage_and_tighten_under_pressure(workspace_dir):
    ws = make_ws(workspace_dir)
    assert _inline_thresholds(ws, "pytest -q", dense=False) == (16384, 240)

    _write_window(workspace_dir, {"turn_carriage_tokens": 20_000, "window_pct": 18.0})
    b_std, l_std = _inline_thresholds(ws, "pytest -q", dense=False)
    assert b_std == 16384 and l_std == 280
    b_proj, l_proj = _inline_thresholds(ws, "python3 -c 'print(1)'", dense=False)
    assert b_proj == 30_000 and l_proj == 420

    # Window pressure (>= 70%) overrides carriage and clamps inline thresholds down.
    _write_window(workspace_dir, {"turn_carriage_tokens": 40_000, "window_pct": 75.0})
    assert _inline_thresholds(ws, "python3 -c 'print(1)'", dense=False) == (8192, 120)


# ------------------------------------------------------------ ctx get tail-slack & carriage scaling
def test_get_absorbs_small_tail_slack_without_extra_pagination_hop(state_home, workspace_dir):
    # 55 lines * ~94 B ≈ 5.2 KiB: ~650 B past the 4,544 B base room, well within
    # the 40% / 2,400 B tail-slack window and <= 40 remaining lines.
    lines = [f"item-{i:03d}: " + ("v" * 82) for i in range(1, 56)]
    target = workspace_dir / "items.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    try:
        out = get(store, ws, "repo:items.txt", Selector(lines=(1, 55)))
        assert "item-001:" in out
        assert "item-055:" in out
        assert "next:" not in out
        assert "[ctx:truncated" not in out
    finally:
        store.close()


def test_get_scales_with_turn_carriage_and_respects_explicit_tight_budgets(
    state_home, tmp_path
):
    ws_root = tmp_path / "carriage_ws"
    ws_root.mkdir()
    (ws_root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    lines = [f"record-{i:03d}: " + ("z" * 80) for i in range(1, 160)]
    (ws_root / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ws = make_ws(ws_root)
    store = make_store(ws)
    try:
        base_out = get(store, ws, "repo:big.txt", Selector(lines=(1, 150)))
        assert "next: ctx get repo:big.txt --lines" in base_out

        # High turn carriage (32K tokens -> 2,000 result tokens) widens single-hop window.
        _write_window(ws_root, {"turn_carriage_tokens": 32_000, "window_pct": 12.0})
        assert _effective_result_tokens(ws, ws.config.budgets) == 2000
        scaled_out = get(store, ws, "repo:big.txt", Selector(lines=(1, 150)))
        assert len(scaled_out) > len(base_out) + 2000

        # Under window pressure, effective result tokens drop back to base 1,200.
        _write_window(ws_root, {"turn_carriage_tokens": 32_000, "window_pct": 80.0})
        assert _effective_result_tokens(ws, ws.config.budgets) == 1200
    finally:
        store.close()

    # Explicitly tightened result_tokens (< 1200) never scales and never absorbs slack.
    tight_root = tmp_path / "tight_ws"
    tight_root.mkdir()
    (tight_root / "ctx.toml").write_text(
        "version = 1\n[budgets]\nresult_tokens = 200\n", encoding="utf-8"
    )
    (tight_root / "big.txt").write_text("\n".join(lines[:40]) + "\n", encoding="utf-8")
    _write_window(tight_root, {"turn_carriage_tokens": 48_000, "window_pct": 10.0})
    tight_ws = make_ws(tight_root)
    tight_store = make_store(tight_ws)
    try:
        assert _effective_result_tokens(tight_ws, tight_ws.config.budgets) == 200
        tight_out = get(tight_store, tight_ws, "repo:big.txt", Selector(lines=(1, 40)))
        assert len(tight_out.encode("utf-8")) <= 900
        assert "next:" in tight_out
    finally:
        tight_store.close()


# ------------------------------------------------------------ non-git child isolation
def test_non_git_workspace_blocks_ancestor_git_discovery(state_home, tmp_path):
    outer = tmp_path / "outer_host_repo"
    outer.mkdir()
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True, env=git_env)
    (outer / "host_secret_log.txt").write_text("host log\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=outer, check=True, env=git_env)
    subprocess.run(["git", "commit", "-qm", "host-init"], cwd=outer, check=True, env=git_env)

    # Plain non-git task directory nested inside `outer`.
    plain_ws_dir = outer / "sub" / "plain_task"
    plain_ws_dir.mkdir(parents=True)
    (plain_ws_dir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")

    ws = make_ws(plain_ws_dir)
    assert ws.git is None
    assert generation_hash(plain_ws_dir) is None

    store = make_store(ws)
    try:
        cap_git = run_capture(
            ws, ["git", "rev-parse", "--is-inside-work-tree"], store=store
        )
        assert cap_git.manifest["result"]["exitCode"] != 0

        cap_env = run_capture(
            ws,
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('GIT_DISCOVERY_ACROSS_FILESYSTEM', ''))",
            ],
            store=store,
        )
        res_env = render_command_result(ws, store, cap_env)
        assert res_env.stdout.strip() == "0"
    finally:
        store.close()


def test_real_git_workspace_retains_git_commands_and_generation_hash(
    state_home, git_workspace
):
    ws = make_ws(git_workspace)
    assert ws.git is not None
    gen = generation_hash(git_workspace)
    assert gen is not None and gen.startswith("sha256:")

    store = make_store(ws)
    try:
        cap = run_capture(
            ws, ["git", "rev-parse", "--is-inside-work-tree"], store=store
        )
        res = render_command_result(ws, store, cap)
        assert res.command_exit_code == 0
        assert res.stdout.strip() == "true"
    finally:
        store.close()


# ------------------------------------------------------------ WatermarkResidence
def _tool_pair(
    name: str,
    args: dict,
    output: str,
    *,
    media_parts: list[dict] | None = None,
    visible_text: str = "",
) -> list[dict]:
    m_parts: list[dict] = [{"thought": True, "text": "internal deliberation"}]
    if visible_text:
        m_parts.append({"text": visible_text})
    m_parts.append(
        {
            "functionCall": {"name": name, "args": args},
            "thoughtSignature": "enc_sig_" + name,
        }
    )
    fr: dict = {"name": name, "response": {"output": output}}
    if media_parts:
        fr["parts"] = list(media_parts)
    u_parts: list[dict] = [{"functionResponse": fr}]
    return [
        {"role": "model", "parts": m_parts},
        {"role": "user", "parts": u_parts},
    ]


def test_watermark_residence_preserves_prefix_state_notes_and_media():
    res = WatermarkResidence(k_keep=2, thought_watermark=10_000, max_result_chars=4096)
    contents: list[dict] = [{"role": "user", "parts": [{"text": "Audit workspace"}]}]

    # Pair 1 (indices 1, 2): carries state_note + multimodal image in functionResponse.parts.
    img_part = {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}
    contents.extend(
        _tool_pair(
            "bash",
            {"command": "python3 inspect_img.py", "state_note": "sku=A12 verified bounding box"},
            "width=800 height=600",
            media_parts=[img_part],
        )
    )
    res.record_model_turn(1, 6_000)

    # Pair 2 (indices 3, 4): oversized output + second state_note; pushes eligible thoughts to 12K.
    long_out = "HEAD_MARKER\n" + ("m" * 5000) + "\nTAIL_MARKER"
    contents.extend(
        _tool_pair(
            "bash",
            {"command": "python3 batch_calc.py", "state_note": "margin=0.425"},
            long_out,
        )
    )
    res.record_model_turn(3, 6_000)

    # Pair 3 (indices 5, 6): only 1 eligible pair so far (6K < 10K watermark) -> no compaction.
    contents.extend(_tool_pair("bash", {"command": "ls"}, "a.json\n"))
    res.record_model_turn(5, 4_000)
    assert res.maybe_compact(contents) == (0, 0)
    assert "functionCall" in contents[1]["parts"][-1]

    # Pair 4 (indices 7, 8): now pairs 1 & 2 are eligible (6K + 6K = 12K >= 10K) -> Epoch 1 fires!
    contents.extend(_tool_pair("bash", {"command": "pwd"}, "/task\n"))
    res.record_model_turn(7, 4_000)
    compacted_pairs, compacted_thoughts = res.maybe_compact(contents)
    assert (compacted_pairs, compacted_thoughts) == (2, 12_000)
    assert res.epochs == 1
    assert res.state_notes_preserved == 2

    # Pair 1 is rewritten in-place: state_note surfaced as [state], stripped from [called], image lifted.
    m1_text = contents[1]["parts"][0]["text"]
    assert "[state] sku=A12 verified bounding box" in m1_text
    assert "state_note" not in m1_text
    assert "[called] bash('python3 inspect_img.py')" in m1_text
    assert contents[2]["parts"][0]["text"] == "[result]\nwidth=800 height=600"
    assert contents[2]["parts"][1] == img_part

    # Pair 2 oversized output keeps both HEAD_MARKER and TAIL_MARKER.
    u2_text = contents[4]["parts"][0]["text"]
    assert "HEAD_MARKER" in u2_text and "TAIL_MARKER" in u2_text
    assert "omitted" in u2_text

    # Trailing k_keep=2 pairs (indices 5..8) remain live functionCall/functionResponse turns.
    assert any("functionCall" in p for p in contents[5]["parts"])
    assert any("functionCall" in p for p in contents[7]["parts"])

    # Snapshot compacted prefix (turns 0..4) and verify Epoch 2 leaves it byte-identical.
    frozen_prefix = copy.deepcopy(contents[:5])
    contents.extend(_tool_pair("bash", {"command": "echo 5"}, "5\n"))
    res.record_model_turn(9, 7_000)
    contents.extend(_tool_pair("bash", {"command": "echo 6"}, "6\n"))
    res.record_model_turn(11, 5_000)
    # Eligible uncompacted turns are now indices 5 (4K) and 7 (4K) = 8K < 10K -> still append-only.
    assert res.maybe_compact(contents) == (0, 0)
    contents.extend(_tool_pair("bash", {"command": "echo 7"}, "7\n"))
    res.record_model_turn(13, 3_000)
    # Now eligible indices 5 (4K), 7 (4K), 9 (7K) = 15K >= 10K -> Epoch 2 compacts only 5, 7, 9!
    assert res.maybe_compact(contents) == (3, 15_000)
    assert res.epochs == 2
    assert contents[:5] == frozen_prefix


def test_watermark_residence_honours_media_and_standard_epoch_ceilings():
    res = WatermarkResidence(
        k_keep=1,
        thought_watermark=1_000,
        max_epochs=3,
        media_max_epochs=1,
    )
    img = {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}
    contents: list[dict] = [{"role": "user", "parts": [{"text": "inspect"}]}]
    contents.extend(_tool_pair("bash", {"command": "step1"}, "ok1", media_parts=[img]))
    res.record_model_turn(1, 2_000)
    contents.extend(_tool_pair("bash", {"command": "step2"}, "ok2"))
    res.record_model_turn(3, 2_000)

    assert res.maybe_compact(contents) == (1, 2_000)
    assert res.epochs == 1

    # Because multimodal inlineData is present in contents, media_max_epochs=1 blocks Epoch 2.
    contents.extend(_tool_pair("bash", {"command": "step3"}, "ok3"))
    res.record_model_turn(5, 5_000)
    assert res.maybe_compact(contents) == (0, 0)
    assert res.epochs == 1
