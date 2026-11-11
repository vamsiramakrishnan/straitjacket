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
from ctx.deliverables import audit_deliverables, extract_declared_deliverables
from ctx.execution import generation_hash, run_capture
from ctx.proxywindow import window_path
from ctx.residence import WatermarkResidence, extract_mutation_receipts
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
    assert (
        _command_intent(
            "bwrap --die-with-parent --unshare-pid --dev /dev --proc /proc "
            "--bind /tmp/run /task --chdir /task -- bash -c \"python3 -c 'print(42)'\""
        )
        == "projection"
    )
    assert (
        _command_intent(
            "bwrap --die-with-parent --unshare-pid --bind /tmp/run /task -- "
            "bash -c 'cat /task/workspace/rows.jsonl | jq .'"
        )
        == "projection"
    )

    assert _command_intent("ps aux") == "flood"
    assert _command_intent("git log -n 50") == "flood"
    assert _command_intent("git diff HEAD~1") == "flood"
    assert _command_intent("find . -maxdepth 4") == "flood"
    assert _command_intent("ls -la /tmp") == "flood"
    assert (
        _command_intent(
            "bwrap --die-with-parent --unshare-pid --dev /dev --proc /proc "
            "--chdir /task -- bash -c 'ps aux'"
        )
        == "flood"
    )

    assert _command_intent("pytest tests/test_bounds.py -q") == "standard"
    assert _command_intent("") == "standard"

    from ctx.reflex import command_signature

    assert command_signature(
        "bwrap --die-with-parent --unshare-pid --bind /tmp/run /task -- bash -c 'pytest tests/test_x.py -q'"
    ) == command_signature("pytest tests/test_x.py -q")


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


def test_watermark_residence_char_watermark_and_superseded_ui_controls():
    res = WatermarkResidence(
        k_keep=1,
        thought_watermark=20_000,
        char_watermark=10_000,
        strip_superseded_ui_controls=True,
    )
    controls_block = "\n".join(f"[e{i}] <button> Item {i} action button" for i in range(1, 90))
    inbox_body = "Inbox view:\n" + "\n".join(f"Thread #{i}: Urgent launch review item {i}" for i in range(1, 35))
    detail_body = "Thread detail:\nSupplier confirmed MOQ=500 and FOB=$3.20 with carton spec."

    snap1 = (
        "[Snapshot] targetId=t1 | url=http://localhost/mail | title=Mail\n"
        f"=== Interactive Elements (89) ===\n{controls_block}\n\n"
        f"=== Visible Page Text ===\n{inbox_body}"
    )
    snap2_dup_inbox = (
        "[Clicked e2] targetId=t1 | url=http://localhost/mail | title=Mail\n"
        f"=== Interactive Elements (89) ===\n{controls_block}\n\n"
        f"=== Visible Page Text ===\n{inbox_body}"
    )
    snap3_unique_detail = (
        "[Clicked e5] targetId=t1 | url=http://localhost/mail/5 | title=Thread 5\n"
        f"=== Interactive Elements (89) ===\n{controls_block}\n\n"
        f"=== Visible Page Text ===\n{detail_body}"
    )
    snap4_latest = (
        "[Clicked e9] targetId=t1 | url=http://localhost/mail/9 | title=Thread 9\n"
        f"=== Interactive Elements (89) ===\n{controls_block}\n\n"
        "=== Visible Page Text ===\nLatest live page text"
    )

    contents: list[dict] = [{"role": "user", "parts": [{"text": "triage inbox"}]}]
    for idx_step, snap in enumerate((snap1, snap2_dup_inbox, snap3_unique_detail, snap4_latest)):
        contents.extend(_tool_pair("browser_click", {"ref": f"e{idx_step + 1}"}, snap))
        # Low thoughts (300 tok/step << 20,000 thought_watermark), so char_watermark triggers compaction!
        res.record_model_turn(1 + 2 * idx_step, 300)

    compacted_pairs, compacted_thoughts = res.maybe_compact(contents)
    assert compacted_pairs == 3
    assert compacted_thoughts == 900
    assert res.ui_controls_stripped_chars > 5_000

    # Pair 1: superseded [e1]..[e89] stripped, unique inbox Visible Page Text retained verbatim.
    u1 = contents[2]["parts"][0]["text"]
    assert "[superseded element refs" in u1
    assert "[e1] <button>" not in u1
    assert "Thread #34: Urgent launch review item 34" in u1

    # Pair 2: identical >400-char inbox Visible Page Text deduplicated.
    u2 = contents[4]["parts"][0]["text"]
    assert "[visible page text identical to earlier snapshot above]" in u2

    # Pair 3: unique thread detail text preserved 100% verbatim while controls stripped.
    u3 = contents[6]["parts"][0]["text"]
    assert "[superseded element refs" in u3
    assert "MOQ=500 and FOB=$3.20" in u3

    # Pair 4 (trailing k_keep=1): live interactive controls stay completely untouched.
    u4 = contents[8]["parts"][0]["functionResponse"]["response"]["output"]
    assert "[e1] <button> Item 1 action button" in u4


def test_watermark_residence_cache_breakeven_media_pinning_and_mutation_ledger():
    # Default WatermarkResidence() uses zero arbitrary watermarks or epoch caps:
    # min_cache_tokens=6_144, cache_read_ratio=0.10, pin_media_turns=True.
    res = WatermarkResidence()
    assert res.thought_watermark is None
    assert res.max_epochs is None
    assert res.pin_media_turns is True

    contents: list[dict] = [{"role": "user", "parts": [{"text": "Audit and update records"}]}]
    img = {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}

    # Step 1 (indices 1, 2): multimodal image inspection turn -> pinned in place under normal window occupancy.
    contents.extend(
        _tool_pair(
            "read",
            {"file_path": "label.png", "state_note": "label SKU=B9 verified"},
            "Loaded image label.png",
            media_parts=[img],
        )
    )
    res.record_model_turn(1, 5_000)

    # Steps 2..5 (indices 3..10): low thoughts (400 tok/step -> 1,200 dead tokens < 6,144 min_cache_tokens).
    contents.extend(
        _tool_pair(
            "write",
            {"file_path": "out/stage1.json", "content": '{"ok": true}', "state_note": "wrote stage1"},
            "wrote out/stage1.json",
        )
    )
    res.record_model_turn(3, 400)
    for step_n in (3, 4, 5):
        contents.extend(_tool_pair("bash", {"command": f"echo {step_n}"}, f"{step_n}\n"))
        res.record_model_turn(1 + 2 * (step_n - 1), 400)

    # Below 6,144 min_cache_tokens -> stays strictly append-only.
    assert res.maybe_compact(contents) == (0, 0)

    # Steps 6..9: step 6 (6K) enters eligible (D=7,600 >= 6,144), while steps 7 (6K) & 8 (5K)
    # sit inside the cached k_keep tail (invalidated_cached_tokens ~11,100). Compacting right
    # now would re-bill steps 7 & 8 uncached (cost 9,990 > savings 6,840), so break-even defers!
    for step_n, t_tok in ((6, 6_000), (7, 6_000), (8, 5_000), (9, 1_000)):
        contents.extend(_tool_pair("bash", {"command": f"python3 check_{step_n}.py"}, "verified\n"))
        res.record_model_turn(1 + 2 * (step_n - 1), t_tok)
    assert res.maybe_compact(contents) == (0, 0)

    # Steps 10..11 (low thoughts): steps 7 & 8 age out of k_keep into eligible (D=18,600,
    # invalidated tail ~1,500) -> single amortized compaction epoch fires!
    for step_n in (10, 11):
        contents.extend(_tool_pair("bash", {"command": f"python3 check_{step_n}.py"}, "verified\n"))
        res.record_model_turn(1 + 2 * (step_n - 1), 500)

    compacted_pairs, compacted_thoughts = res.maybe_compact(contents)
    assert compacted_pairs == 7  # steps 2..8 compacted; step 1 (media) stays pinned; steps 9..11 are k_keep=3
    assert compacted_thoughts == (400 * 4 + 6_000 + 6_000 + 5_000)
    assert res.mutations_preserved == 1

    # Step 1 (indices 1, 2) remains an uncompacted multimodal functionCall/functionResponse turn!
    assert any("functionCall" in p for p in contents[1]["parts"])
    assert "functionResponse" in contents[2]["parts"][0]

    # Step 2 (index 3) was tagged [mutated], and the epoch boundary (step 8, index 15) carries [prior-mutations].
    assert "[mutated] write(" in contents[3]["parts"][0]["text"]
    boundary_text = contents[15]["parts"][0]["text"]
    assert "[prior-mutations (1)] write(file_path='out/stage1.json'" in boundary_text

    # Verify frozen prefix invariance across Epoch 2 (indices 0..16 stay byte-identical).
    frozen_prefix = copy.deepcopy(contents[:17])
    for step_n in (12, 13, 14, 15):
        contents.extend(_tool_pair("bash", {"command": f"echo {step_n}"}, "ok\n"))
        res.record_model_turn(1 + 2 * (step_n - 1), 3_000)
    for step_n in (16, 17):
        contents.extend(_tool_pair("bash", {"command": f"echo {step_n}"}, "ok\n"))
        res.record_model_turn(1 + 2 * (step_n - 1), 400)

    comp2, _ = res.maybe_compact(contents)
    assert comp2 >= 4
    assert contents[:17] == frozen_prefix
    assert "[prior-mutations (1)]" in contents[15 + 2 * comp2]["parts"][0]["text"]


def test_deliverables_extraction_and_audit_sparsity_and_hygiene(tmp_path: Path):
    prompt = (
        "Inspect `/task/workspace/raw_claims.csv` and write `/task/outputs/claims_audit.json` "
        "plus `outputs/executive_brief.md`.\n\n"
        "Save all deliverables under `/task/outputs/`: `remediation.csv`, `badge.svg`, "
        "ignoring wildcard examples like `outputs/*.tmp` or `outputs/claim_0X.json`."
    )
    declared = extract_declared_deliverables(prompt)
    assert declared == [
        "outputs/claims_audit.json",
        "outputs/executive_brief.md",
        "outputs/remediation.csv",
        "outputs/badge.svg",
    ]

    ws = tmp_path / "task_ws"
    out_dir = ws / "outputs"
    out_dir.mkdir(parents=True)

    # 1. Missing declared deliverables + stray helper script -> blocking defects.
    (out_dir / "helper_build.py").write_text("print('temp')\n", encoding="utf-8")
    (out_dir / "claims_audit.json").write_text(
        json.dumps(
            {
                "claims": [
                    {"claim_id": "CLM-001", "status": "substantiated", "replacement_text": "Verified 12h"},
                    {"claim_id": "CLM-002", "status": "needs_edit", "replacement_text": ""},
                    {"claim_id": "CLM-003", "status": "needs_edit", "replacement_text": "Tested to IPX4"},
                    {"claim_id": "CLM-004", "status": "substantiated", "replacement_text": "2-year warranty"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "executive_brief.md").write_text(
        "# Executive Brief\n\n- Audited 4 marketplace claims.\n",
        encoding="utf-8",
    )

    audit1 = audit_deliverables(
        ws,
        ["/task/outputs/claims_audit.json"],
        declared_paths=declared,
    )
    assert audit1.has_blocking_defects is True
    assert audit1.should_gate is True
    gate_txt = audit1.format_gate_message()
    assert "outputs/remediation.csv" in gate_txt
    assert "outputs/badge.svg" in gate_txt
    assert "helper_build.py" in gate_txt
    # Existing executive_brief.md omitted from present_files is flagged as [CHECK].
    assert "executive_brief.md" in gate_txt and "omitted from present_files" in gate_txt
    # Sparse empty `replacement_text` on CLM-002 (populated on 3/4 records) is flagged with record ID!
    assert "CLM-002" in gate_txt and "replacement_text" in gate_txt

    # 2. Populate remaining files cleanly and remove stray script -> zero defects or advisories!
    (out_dir / "helper_build.py").unlink()
    (out_dir / "claims_audit.json").write_text(
        json.dumps(
            {
                "claims": [
                    {"claim_id": "CLM-001", "status": "substantiated", "replacement_text": "Verified 12h"},
                    {"claim_id": "CLM-002", "status": "needs_edit", "replacement_text": "Water-resistant shell"},
                    {"claim_id": "CLM-003", "status": "needs_edit", "replacement_text": "Tested to IPX4"},
                    {"claim_id": "CLM-004", "status": "substantiated", "replacement_text": "2-year warranty"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "remediation.csv").write_text("claim_id,action\nCLM-002,rewrite\n", encoding="utf-8")
    (out_dir / "badge.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>', encoding="utf-8")

    audit2 = audit_deliverables(
        ws,
        [
            "/task/outputs/claims_audit.json",
            "/task/outputs/executive_brief.md",
            "/task/outputs/remediation.csv",
            "/task/outputs/badge.svg",
        ],
        declared_paths=declared,
    )
    assert audit2.has_blocking_defects is False
    assert audit2.should_gate is False
    assert len(audit2.structural_summary) == 4


def test_deliverables_multistate_enum_conditional_nullability_without_binary_false_positives(tmp_path: Path):
    ws = tmp_path / "enum_ws"
    out_dir = ws / "outputs"
    out_dir.mkdir(parents=True)

    # 1. Multi-state enum (>=3 states: revise, approve, needs_more_evidence, remove) where
    #    1 state is 100% populated (4/12 < 50%) and >=2 other states collapse to "".
    (out_dir / "review.json").write_text(
        json.dumps(
            {
                "items": [
                    {"id": "I-01", "decision": "revise", "replacement": "Fix A"},
                    {"id": "I-02", "decision": "revise", "replacement": "Fix B"},
                    {"id": "I-03", "decision": "revise", "replacement": "Fix C"},
                    {"id": "I-04", "decision": "revise", "replacement": "Fix D"},
                    {"id": "I-05", "decision": "approve", "replacement": ""},
                    {"id": "I-06", "decision": "approve", "replacement": ""},
                    {"id": "I-07", "decision": "needs_more_evidence", "replacement": ""},
                    {"id": "I-08", "decision": "remove", "replacement": ""},
                    {"id": "I-09", "decision": "remove", "replacement": ""},
                    {"id": "I-10", "decision": "remove", "replacement": ""},
                    {"id": "I-11", "decision": "remove", "replacement": ""},
                    {"id": "I-12", "decision": "remove", "replacement": ""},
                ]
            }
        ),
        encoding="utf-8",
    )
    audit_multi = audit_deliverables(ws, ["outputs/review.json"])
    assert audit_multi.should_gate is True
    msg = audit_multi.format_gate_message()
    assert "approve=0/2 [I-05, I-06]" in msg
    assert "needs_more_evidence=0/1 [I-07]" in msg

    # 2. Binary 2-state enum (error vs ok) must NOT fire a false-positive advisory!
    (out_dir / "status.json").write_text(
        json.dumps(
            {
                "checks": [
                    {"id": "C-1", "status": "error", "error_msg": "timeout"},
                    {"id": "C-2", "status": "error", "error_msg": "bad_crc"},
                    {"id": "C-3", "status": "ok", "error_msg": ""},
                    {"id": "C-4", "status": "ok", "error_msg": ""},
                    {"id": "C-5", "status": "ok", "error_msg": ""},
                    {"id": "C-6", "status": "ok", "error_msg": ""},
                ]
            }
        ),
        encoding="utf-8",
    )
    audit_bin = audit_deliverables(ws, ["outputs/status.json"])
    assert audit_bin.should_gate is False


def test_watermark_residence_empirical_mutation_receipts_and_bash_classifiers():
    cli_out = 'Uploading supplier dossier...\n✓ Created page "Supplier Evidence Handoff" (page_9f8e7d)\nDone.'
    json_out = '{"ok": true, "id": "draft_4412", "status": "draft_created"}'
    assert extract_mutation_receipts(cli_out) == ['Created page "Supplier Evidence Handoff" (page_9f8e7d)']
    assert extract_mutation_receipts(json_out) == ["draft_4412 (draft_created)"]

    res = WatermarkResidence(k_keep=1, thought_watermark=1_000)
    contents: list[dict] = [{"role": "user", "parts": [{"text": "Run handoff"}]}]
    # Opaque script command string, but empirical output carries creation receipt!
    contents.extend(_tool_pair("bash", {"command": "python3 run_handoff.py"}, cli_out))
    res.record_model_turn(1, 2_000)
    # Multiline urllib POST + JSON-RPC tools/call mutation
    mcp_cmd = (
        "python3 - <<'PY'\n"
        "import json, urllib.request\n"
        'body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", '
        '"params": {"name": "gmail.createDraft", "arguments": {"to": "v@example.com"}}}\n'
        "req = urllib.request.Request('http://127.0.0.1:3000/mcp', headers={'Content-Type': 'application/json'}, data=json.dumps(body).encode())\n"
        "PY"
    )
    contents.extend(_tool_pair("bash", {"command": mcp_cmd}, json_out))
    res.record_model_turn(3, 2_000)
    contents.extend(_tool_pair("bash", {"command": "ls outputs"}, "ok\n"))
    res.record_model_turn(5, 500)

    compacted_pairs, _ = res.maybe_compact(contents)
    assert compacted_pairs == 2
    assert res.mutations_preserved == 2
    assert "[mutated] bash('python3 run_handoff.py')" in contents[1]["parts"][0]["text"]
    boundary_summary = contents[3]["parts"][0]["text"]
    assert '[prior-mutations (2)]' in boundary_summary
    assert 'Created page "Supplier Evidence Handoff" (page_9f8e7d)' in boundary_summary
    assert "draft_4412 (draft_created)" in boundary_summary


def test_watermark_residence_empirical_single_tip_cache_avoids_thrash_and_respects_remaining_steps():
    # 1. Single-tip rolling cache simulator: post-compaction cached_tokens drops to Turn-0 floor (3,600).
    res_single = WatermarkResidence(k_keep=3)
    contents: list[dict] = [{"role": "user", "parts": [{"text": "Initial task instructions " * 80}]}]
    floor_tokens = 3_600

    # Accumulate 8 heavy turns -> Epoch 1 compacts first 5 pairs (first_idx == 1, measures Turn-0 floor).
    p_tok = floor_tokens
    c_tok = 0
    for step in range(1, 9):
        res_single.observe_cache_usage(p_tok, c_tok)
        idx = len(contents)
        contents.extend(_tool_pair("bash", {"command": f"echo step_{step}"}, "result payload " * 180))
        res_single.record_model_turn(idx, 3_500)
        n_c, _ = res_single.maybe_compact(contents, remaining_steps=120 - step)
        p_tok += 4_200
        c_tok = floor_tokens if n_c else (p_tok - 4_200)

    assert res_single.epochs == 1
    assert res_single._single_tip_cache is False

    # Next step observes post-Epoch-1 Turn-0 floor (first_idx == 1).
    res_single.observe_cache_usage(22_000, floor_tokens)
    assert res_single._retained_cache_floor == floor_tokens

    # Accumulate enough turns for Epoch 2 (first_idx > 1, expected_retained_prefix >= min_probe).
    p_tok = 26_000
    c_tok = 21_000
    while res_single.epochs < 2:
        res_single.observe_cache_usage(p_tok, c_tok)
        idx = len(contents)
        contents.extend(_tool_pair("bash", {"command": f"echo mid_{idx}"}, "mid result " * 200))
        res_single.record_model_turn(idx, 4_000)
        n_c, _ = res_single.maybe_compact(contents, remaining_steps=90)
        p_tok += 4_800
        c_tok = floor_tokens if n_c else (p_tok - 4_800)

    # Post-Epoch-2 API call reports cached_tokens reset to 3,600 despite >20K unchanged prefix -> learns single-tip!
    res_single.observe_cache_usage(30_000, floor_tokens)
    assert res_single._single_tip_cache is True
    assert res_single._retained_cache_floor == floor_tokens

    # Now simulate a deep 95K cached prefix with modest 7K dead thoughts across only 4 eligible turns:
    # single-tip pricing + EOQ horizon must suppress premature thrashing!
    for n_small in range(4):
        res_single.observe_cache_usage(98_000 + n_small * 2_000, 94_000 + n_small * 2_000)
        idx = len(contents)
        contents.extend(_tool_pair("bash", {"command": f"echo small_{n_small}"}, "ok " * 60))
        res_single.record_model_turn(idx, 1_800)
        n_c, _ = res_single.maybe_compact(contents, remaining_steps=50)
        assert n_c == 0
    assert res_single.epochs == 2

    # Near-end remaining_steps guard: even when EOQ break-even is reached over a full interval,
    # a short remaining step budget skips an unamortizable cache reset.
    for n_big in range(8):
        idx = len(contents)
        contents.extend(_tool_pair("bash", {"command": f"echo big_{n_big}"}, "ok " * 80))
        res_single.record_model_turn(idx, 7_500)
    res_single.observe_cache_usage(68_000, 55_000)
    assert res_single.maybe_compact(contents, remaining_steps=0) == (0, 0)
    assert res_single.maybe_compact(contents, remaining_steps=4) == (0, 0)

    # With ample remaining steps and EOQ-amortized dead thoughts (12 * 0.10 * ~70K > 0.90 * 51.4K), compacts cleanly.
    n_final, _ = res_single.maybe_compact(contents, remaining_steps=40)
    assert n_final > 0
    assert res_single.epochs == 3

    # 2. Multi-checkpoint cache simulator: post-Epoch-2 cached_tokens retains prefix -> _single_tip_cache stays False.
    res_multi = WatermarkResidence(k_keep=2)
    m_contents: list[dict] = [{"role": "user", "parts": [{"text": "Task " * 100}]}]
    for step in range(1, 12):
        res_multi.observe_cache_usage(10_000 + step * 4_000, 8_000 + step * 3_500)
        idx = len(m_contents)
        m_contents.extend(_tool_pair("bash", {"command": f"echo m_{step}"}, "payload " * 250))
        res_multi.record_model_turn(idx, 5_000)
        n_mc, _ = res_multi.maybe_compact(m_contents, remaining_steps=80)
        if n_mc and res_multi.epochs == 2:
            # Retain 90% of expected prefix on the call immediately following Epoch 2.
            res_multi.observe_cache_usage(45_000, max(20_000, int(0.9 * res_multi._expected_retained_prefix_tokens)))
            break
    assert res_multi._single_tip_cache is False


def test_watermark_residence_c1_horizon_gates_min_turn_gap_and_sub_block_single_tip_detector():
    # 1. Pre-Epoch-2 single-tip detector via >=3 sub-block cache increments (256 < delta_c < 3_000).
    res = WatermarkResidence(k_keep=2)
    contents: list[dict] = [{"role": "user", "parts": [{"text": "Task instructions " * 60}]}]
    res.observe_cache_usage(4_000, 3_200)
    res.observe_cache_usage(5_200, 4_200)  # delta_c = 1,000 (#1)
    res.observe_cache_usage(6_500, 5_400)  # delta_c = 1,200 (#2)
    assert res.single_tip_cache_detected is False
    res.observe_cache_usage(7_800, 6_600)  # delta_c = 1,200 (#3 -> triggers single_tip_cache_detected)
    assert res.single_tip_cache_detected is True
    assert res.get_telemetry()["single_tip_cache_detected"] is True
    assert res.get_telemetry()["sub_block_cache_steps"] == 3

    # 2. remaining_steps <= 10 gate: never compact in the final 10 steps regardless of dead_tokens.
    for step in range(1, 7):
        idx = len(contents)
        contents.extend(_tool_pair("bash", {"command": f"echo {step}"}, "output " * 120))
        res.record_model_turn(idx, 5_000)
    assert res.maybe_compact(contents, remaining_steps=10) == (0, 0)
    assert res.maybe_compact(contents, remaining_steps=1) == (0, 0)

    # 3. has_final_output_write=True caps expected_horizon at 4 -> blocks marginal compaction on a deep prefix.
    res.observe_cache_usage(36_000, 32_000)
    assert (
        res.maybe_compact(
            contents,
            remaining_steps=40,
            has_final_output_write=True,
        )
        == (0, 0)
    )

    # With has_final_output_write=False and enough dead thoughts to beat full-prefix invalidation, Epoch 1 fires.
    for step in range(7, 10):
        idx = len(contents)
        contents.extend(_tool_pair("bash", {"command": f"echo {step}"}, "output " * 80))
        res.record_model_turn(idx, 9_000)
    comp1, _ = res.maybe_compact(contents, remaining_steps=40, has_final_output_write=False)
    assert comp1 > 0
    assert res.epochs == 1

    # 4. min_turn_gap=6 blocks another epoch within <6 new pairs when uncompacted_thoughts < 25,000,
    #    then allows an emergency override once uncompacted_thoughts >= 25,000.
    res_gap = WatermarkResidence(k_keep=2)
    g_contents: list[dict] = [{"role": "user", "parts": [{"text": "Start"}]}]
    for step in range(1, 6):
        idx = len(g_contents)
        g_contents.extend(_tool_pair("bash", {"command": f"echo g_{step}"}, "ok\n"))
        res_gap.record_model_turn(idx, 3_500)
    assert res_gap.maybe_compact(g_contents, remaining_steps=50)[0] == 3
    assert res_gap.epochs == 1

    # Add 2 more pairs (gap = 2 < 6), total uncompacted thoughts = 2*3,500 + 2*4,000 = 15,000 < 25,000 -> blocked!
    for step in (6, 7):
        idx = len(g_contents)
        g_contents.extend(_tool_pair("bash", {"command": f"echo g_{step}"}, "ok\n"))
        res_gap.record_model_turn(idx, 4_000)
    assert res_gap.maybe_compact(g_contents, remaining_steps=50, min_turn_gap=6) == (0, 0)

    # Push uncompacted thoughts to >= 25,000 within the same <6 turn window -> emergency override fires!
    idx = len(g_contents)
    g_contents.extend(_tool_pair("bash", {"command": "echo spike"}, "ok\n"))
    res_gap.record_model_turn(idx, 12_000)
    assert res_gap.maybe_compact(g_contents, remaining_steps=50, min_turn_gap=6)[0] > 0
    assert res_gap.epochs == 2


def test_watermark_residence_c3_strips_thoughts_on_media_turns_inside_invalidated_range():
    res = WatermarkResidence(k_keep=2)
    img = {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}
    contents: list[dict] = [{"role": "user", "parts": [{"text": "Inspect images and compute"}]}]

    # Pair 1 (idx 1, 2): early media turn BEFORE first_idx -> must stay 100% untouched (thoughts kept).
    contents.extend(_tool_pair("read", {"file_path": "early.png"}, "Loaded early.png", media_parts=[img]))
    res.record_model_turn(1, 2_000)

    # Pair 2 (idx 3, 4): non-media turn (becomes first_idx = 3).
    contents.extend(_tool_pair("bash", {"command": "echo p2"}, "p2\n"))
    res.record_model_turn(3, 4_000)

    # Pair 3 (idx 5, 6): mid-array media turn at idx=5 >= first_idx=3 -> pinned for image retention,
    # but its 6,000 thought tokens and {"thought": True} part MUST be stripped by C3!
    contents.extend(_tool_pair("read", {"file_path": "mid.png"}, "Loaded mid.png", media_parts=[img]))
    res.record_model_turn(5, 6_000)

    # Pair 4 (idx 7, 8): non-media turn inside eligible.
    contents.extend(_tool_pair("bash", {"command": "echo p4"}, "p4\n"))
    res.record_model_turn(7, 4_000)

    # Pairs 5 & 6 (idx 9..12): trailing k_keep=2 pairs.
    for step in (5, 6):
        idx = len(contents)
        contents.extend(_tool_pair("bash", {"command": f"echo p{step}"}, "ok\n"))
        res.record_model_turn(idx, 500)

    pairs_c, thoughts_c = res.maybe_compact(contents, remaining_steps=50)
    assert pairs_c == 2  # pairs 2 & 4 rewritten to text
    assert thoughts_c == (4_000 + 6_000 + 4_000)  # includes 6,000 thoughts stripped from mid media turn!

    # Early media turn (idx 1) before first_idx=3 still has its thought part untouched.
    assert any(p.get("thought") for p in contents[1]["parts"])
    # Mid media turn (idx 5 >= first_idx=3) keeps its functionCall and image part, but its thought part is gone!
    assert not any(p.get("thought") for p in contents[5]["parts"])
    assert any("functionCall" in p for p in contents[5]["parts"])
    assert contents[6]["parts"][0]["functionResponse"]["parts"] == [img]


def test_watermark_residence_k1_fact_bearing_state_notes_and_compaction_recap():
    from ctx.residence import (
        _extract_state_notes_from_part,
        _inject_compaction_recap,
        _is_fact_bearing_state_note,
    )

    assert _is_fact_bearing_state_note("Check directory") is False
    assert _is_fact_bearing_state_note("Read file") is False
    assert _is_fact_bearing_state_note("Inspect workspace") is False
    assert _is_fact_bearing_state_note("assumed: fiscal year ends Dec 31") is True
    assert _is_fact_bearing_state_note("total_revenue = 148250.50") is True
    assert _is_fact_bearing_state_note("verified all 12 rows match schema") is True

    part_generic = {"functionCall": {"name": "bash", "args": {"command": "ls", "state_note": "Check directory"}}}
    part_fact = {"functionCall": {"name": "bash", "args": {"command": "ls", "state_note": "found 14 CSV files"}}}
    assert _extract_state_notes_from_part(part_generic) == []
    assert _extract_state_notes_from_part(part_fact) == ["found 14 CSV files"]

    res = WatermarkResidence(k_keep=1, thought_watermark=1_000)
    contents: list[dict] = [
        {
            "role": "user",
            "parts": [
                {
                    "text": (
                        "Analyze sales.\n"
                        "[deliverable-contract]\n"
                        "- outputs/report.json (json): keys=[total, rows]"
                    )
                }
            ],
        }
    ]
    # Pair 1: generic state_note ("Check directory") -> filtered out; output has [data-shape: ...] receipt.
    contents.extend(
        _tool_pair(
            "bash",
            {"command": "ls -la", "state_note": "Check directory"},
            "[data-shape: format=csv, rows=24, fields=[id, amount]]\nid,amount\n1,10.0\n",
        )
    )
    res.record_model_turn(1, 2_000)

    # Pair 2: fact-bearing state_note + assumed state_note + [section: ...] and key=value receipts.
    contents.extend(
        _tool_pair(
            "bash",
            {
                "command": "python3 calc.py",
                "state_note": "assumed: USD currency; total_amount=4820.50",
            },
            "[section: totals]\nnet_revenue = 4820.50\n[write-receipt: path=outputs/report.json bytes=128 sha256=abcd1234]\n",
        )
    )
    res.record_model_turn(3, 2_000)

    # Pair 3 (trailing k_keep=1)
    contents.extend(_tool_pair("bash", {"command": "echo done"}, "done\n"))
    res.record_model_turn(5, 200)

    pairs_c, _ = res.maybe_compact(contents, remaining_steps=40)
    assert pairs_c == 2
    assert res.state_notes_preserved == 1

    # Pair 1 summary does NOT contain generic "Check directory".
    m1_txt = contents[1]["parts"][0]["text"]
    assert "Check directory" not in m1_txt

    # Pair 2 (last_eligible_idx) carries [compaction-recap] with [verified-notes], [computed-receipts], and [deliverable-contract].
    m2_txt = contents[3]["parts"][0]["text"]
    assert "[compaction-recap]" in m2_txt
    assert "[verified-notes: assumed: USD currency; total_amount=4820.50]" in m2_txt
    assert "[computed-receipts:" in m2_txt
    assert "[data-shape: format=csv, rows=24, fields=[id, amount]]" in m2_txt
    assert "net_revenue = 4820.50" in m2_txt
    assert "[deliverable-contract]" in m2_txt

    # Direct helper _inject_compaction_recap prioritizes assumed: notes first.
    dummy_contents = [{"role": "model", "parts": [{"text": "[called] bash('ls')"}]}]
    recap_block = _inject_compaction_recap(
        dummy_contents,
        ["verified 10 items", "assumed: tax_rate=0.08"],
        ["write('outputs/a.json')"],
    )
    assert recap_block.index("assumed: tax_rate=0.08") < recap_block.index("verified 10 items")


def test_deliverables_wave3_fix1_active_sibling_numbered_slots_manifest_and_html_audit(tmp_path: Path):
    from ctx.deliverables import format_deliverable_contract, verify_declared_deliverables

    ws = tmp_path / "wave3_ws"
    out_dir = ws / "outputs"
    in_dir = ws / "workspace"
    img_dir = in_dir / "images"
    out_dir.mkdir(parents=True)
    img_dir.mkdir(parents=True)

    # 1. Conditional Non-Empty Sibling-Field Check (5/8 active rows, 7/8 active rows, and 8/8 clean).
    claim_md = (
        "Write `outputs/claim_review.json` with decisions (`approve`, `revise`, `remove`, `needs_more_evidence`).\n"
        "- `replacement_text` may be empty only when the decision is `remove`.\n"
    )
    rows_5_of_8 = [
        {"claim_id": f"CLM-00{i}", "decision": "revise", "replacement_text": f"Revised {i}"}
        for i in (1, 5, 6, 10, 12)
    ] + [
        {"claim_id": "CLM-002", "decision": "approve", "replacement_text": ""},
        {"claim_id": "CLM-003", "decision": "approve", "replacement_text": ""},
        {"claim_id": "CLM-007", "decision": "needs_more_evidence", "replacement_text": ""},
    ] + [
        {"claim_id": f"CLM-00{i}", "decision": "remove", "replacement_text": ""}
        for i in (4, 8, 9, 11)
    ]
    (out_dir / "claim_review.json").write_text(
        json.dumps({"claims": rows_5_of_8}),
        encoding="utf-8",
    )
    ok_5, issues_5 = verify_declared_deliverables(
        ["outputs/claim_review.json"],
        task_markdown=claim_md,
        workspace_root=ws,
    )
    assert ok_5 is False
    joined_5 = "\n".join(issues_5)
    assert "CLM-002, CLM-003, CLM-007" in joined_5
    assert "5/8 active records" in joined_5
    assert "may be empty only when the decision is `remove`" in joined_5

    # 7/8 active populated (only CLM-007 missing) -> flags ONLY CLM-007, not the 4 remove rows.
    for r in rows_5_of_8:
        if r["claim_id"] in ("CLM-002", "CLM-003"):
            r["replacement_text"] = "Keep approved wording"
    (out_dir / "claim_review.json").write_text(
        json.dumps({"claims": rows_5_of_8}),
        encoding="utf-8",
    )
    ok_7, issues_7 = verify_declared_deliverables(
        ["outputs/claim_review.json"],
        task_markdown=claim_md,
        workspace_root=ws,
    )
    assert ok_7 is False
    joined_7 = "\n".join(issues_7)
    assert "[CLM-007]" in joined_7
    assert "7/8 active records" in joined_7

    # 8/8 active populated + 4/4 remove rows empty -> 100% clean (no false positive on remove rows!).
    for r in rows_5_of_8:
        if r["claim_id"] == "CLM-007":
            r["replacement_text"] = "Pending lab verification wording"
    (out_dir / "claim_review.json").write_text(
        json.dumps({"claims": rows_5_of_8}),
        encoding="utf-8",
    )
    ok_8, issues_8 = verify_declared_deliverables(
        ["outputs/claim_review.json"],
        task_markdown=claim_md,
        workspace_root=ws,
    )
    assert ok_8 is True
    assert issues_8 == []

    # 2. Numbered-Slot & Workspace-Asset Check beyond 16th key in a 29-key dict.
    for idx_img in range(1, 6):
        (img_dir / f"projector_0{idx_img}.jpg").write_bytes(b"\xff\xd8\xff\xe0valid_jpeg")
    listing_payload: dict = {f"attr_{i}": f"val_{i}" for i in range(1, 25)}
    listing_payload.update(
        {
            "image_1": "images/projector_01.jpg",
            "image_2": "images/projector_02.jpg",
            "image_3": "",
            "image_4": "",
            "image_5": "",
        }
    )
    (out_dir / "listing.json").write_text(json.dumps(listing_payload), encoding="utf-8")
    audit_slot = audit_deliverables(ws, ["outputs/listing.json"])
    assert audit_slot.should_gate is True
    slot_msg = audit_slot.format_gate_message()
    assert "['image_3', 'image_4', 'image_5']" in slot_msg
    assert "2/5 populated" in slot_msg
    assert "5 candidate image files exist in workspace" in slot_msg

    # 3. Manifest Key Alias Check (Chinese prompt with navigation_sections and missing sections / backend_required).
    manifest_md = "生成 `outputs/implementation_manifest.json`，包含导航段落摘要（至少 6 个）以及是否需要后台（不需要）。"
    (out_dir / "implementation_manifest.json").write_text(
        json.dumps(
            {
                "site_title": "Hengyan Industrial",
                "navigation_sections": ["hero", "products", "specs", "certs", "faq", "contact"],
            }
        ),
        encoding="utf-8",
    )
    audit_man = audit_deliverables(ws, ["outputs/implementation_manifest.json"], task_markdown=manifest_md)
    assert audit_man.should_gate is True
    man_msg = audit_man.format_gate_message()
    assert "navigation_sections" in man_msg and "'sections' is missing" in man_msg
    assert "'backend_required' is missing" in man_msg

    # 4. HTML <section id> coverage check + Deck reconciliation check + format_deliverable_contract hints.
    sec_blocks = ['<section id="executive-overview"><p>Intro narrative only</p></section>']
    expected_10 = [
        "methodology",
        "scorecard",
        "category-summary",
        "brand-deep-dive",
        "Geo-signals",
        "seasonality",
        "coffee-snapshot",
        "appendix-table",
        "related-queries",
        "launch-calendar",
    ]
    for sid in expected_10:
        sec_blocks.append(f'<section id="{sid}"><table><tr><td>8.50</td></tr></table></section>')
    (in_dir / "analyst_report.html").write_text("\n".join(sec_blocks), encoding="utf-8")
    (out_dir / "audit_report.json").write_text(
        json.dumps(
            {
                "findings": [
                    {"section_id": sid, "status": "verified"}
                    for sid in expected_10
                    if sid != "coffee-snapshot"
                ]
            }
        ),
        encoding="utf-8",
    )
    audit_html = audit_deliverables(ws, ["outputs/audit_report.json"])
    assert audit_html.should_gate is True
    assert "['coffee-snapshot']" in audit_html.format_gate_message()

    (out_dir / "claim_audit.json").write_text(
        json.dumps(
            {
                "must_fix_claim_ids": ["pruning_shears_savings"],
                "claims": [
                    {
                        "claim_id": "inventory_coverage",
                        "status": "needs_rounding_note",
                        "deck_value": "74.0% (29/39)",
                        "correct_value": "74.4%",
                    },
                    {
                        "claim_id": "pruning_shears_savings",
                        "status": "incorrect",
                        "deck_value": "$600",
                        "correct_value": "$480.00",
                    },
                    {
                        "claim_id": "q1_revenue",
                        "status": "supported",
                        "deck_value": "$1.2M",
                        "correct_value": "$1.2M",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    audit_deck = audit_deliverables(ws, ["outputs/claim_audit.json"])
    deck_msg = audit_deck.format_gate_message()
    assert "needs_rounding_note" in deck_msg and "must_fix_claim_ids" in deck_msg
    assert "74.4% (29/39)" in deck_msg
    assert "pruning_shears_savings" in deck_msg

    contract_txt = format_deliverable_contract(
        "Reconcile supplier risk rollup and write `outputs/claim_audit.json`.",
        workspace_root=ws,
    )
    assert "audit-precision:" in contract_txt
    assert "74.4% (29/39)" in contract_txt


def test_deliverables_wave4_audit_checks(tmp_path: Path):
    ws = tmp_path / "wave4_ws"
    out_dir = ws / "outputs"
    in_dir = ws / "workspace"
    out_dir.mkdir(parents=True)
    in_dir.mkdir(parents=True)

    # (a) Audit-trail Old/New/delta/product completeness in deck_reconciliation.json (even when claim_id is free-text).
    (out_dir / "deck_reconciliation.json").write_text(
        json.dumps(
            {
                "must_fix_claim_ids": ["cost_win_monterrey_steel_nearshoring"],
                "claims": [
                    {
                        "claim_id": "cost_win_monterrey_steel_nearshoring",
                        "slide_object_id": "g_sr_005",
                        "status": "incorrect",
                        "deck_value": "$2.95/unit across 2,400 units",
                        "correct_value": "$2.95/unit across 2400 units",
                        "brief_reason": "Audit trail shows price drop on TOOL-PRUNE-01.",
                    },
                    {"claim_id": "c2", "status": "supported", "deck_value": "10", "correct_value": "10"},
                    {"claim_id": "c3", "status": "supported", "deck_value": "20", "correct_value": "20"},
                ],
            }
        ),
        encoding="utf-8",
    )
    a_msg = audit_deliverables(ws, ["outputs/deck_reconciliation.json"]).format_gate_message()
    assert "3.15" in a_msg and "0.20" in a_msg and "480" in a_msg

    # (b) Markdown intro-paragraph section-keyword shadowing + (e) Dangerous-goods overclaims.
    (out_dir / "listing_pack.md").write_text(
        "# Product SEO Pack\n"
        "This pack contains the optimized title, 5 bullet points, and 10 search terms.\n\n"
        "## 1. Title\n- Great Tape\n\n"
        "## 2. Bullet Points\n- Strong adhesion\n\n"
        "## 3. Search Terms\n- nano tape\n",
        encoding="utf-8",
    )
    b_msg = audit_deliverables(ws, ["outputs/listing_pack.md"]).format_gate_message()
    assert "bullet points" in b_msg.lower() and "shadow" in b_msg.lower()

    (out_dir / "operations_brief.md").write_text(
        "# Operations Brief\n"
        "Hold SKU-1, SKU-2, SKU-3; note that nothing is guaranteed compliant or safe for all air transport.\n",
        encoding="utf-8",
    )
    e_msg = audit_deliverables(ws, ["outputs/operations_brief.md"]).format_gate_message()
    assert "guaranteed compliant" in e_msg and "safe for all air" in e_msg

    # (c) JSON tier_summary vs sibling CSV pricing_matrix.csv best_promo_tier counts.
    (out_dir / "pricing_matrix.csv").write_text(
        "sku,best_promo_tier\n"
        + "\n".join(f"S{i},flash_sale" for i in range(8))
        + "\nS9,platform_voucher\nS10,no_promo\n",
        encoding="utf-8",
    )
    (out_dir / "promotion_plan.json").write_text(
        json.dumps(
            {
                "tier_summary": {
                    "flash_sale_count": 6,
                    "platform_voucher_count": 1,
                    "no_promo_count": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    c_msg = audit_deliverables(ws, ["outputs/promotion_plan.json"]).format_gate_message()
    assert "flash_sale_count=6" in c_msg and "is 8" in c_msg

    # (d) Variant-merger signal CSV all-cells & policy clause citation completeness.
    sig_dir = in_dir / "review_signals"
    sig_dir.mkdir(parents=True)
    (sig_dir / "image_ocr_and_packaging_flags.csv").write_text(
        "candidate_sku,image_flag,ocr_text,review_note\n"
        "TRAIL-1000-SIL,size_callout,1000 ML,front-face label reads '1000 ML'\n"
        "TRAIL-750-BAMBOO,cap_material_callout,BAMBOO LID,cap close-up shows bamboo wood texture\n",
        encoding="utf-8",
    )
    (out_dir / "variant_audit.json").write_text(
        json.dumps(
            {
                "candidate_evaluations": [
                    {
                        "candidate_sku": "TRAIL-1000-SIL",
                        "decision": "reject",
                        "evidence": [
                            {
                                "source_file": "review_signals/image_ocr_and_packaging_flags.csv",
                                "cite": "1000 ML bottle",
                            },
                            {
                                "source_file": "platform_policy/marketplace_variation_policy.pdf",
                                "cite": "Capacity differs from 750ml parent.",
                            },
                        ],
                    },
                    {
                        "candidate_sku": "TRAIL-750-BAMBOO",
                        "decision": "reject",
                        "evidence": [
                            {
                                "source_file": "review_signals/image_ocr_and_packaging_flags.csv",
                                "cite": "BAMBOO LID",
                            }
                        ],
                    },
                    {"candidate_sku": "TRAIL-750-BLK", "decision": "accept", "evidence": [{"source_file": "a", "cite": "b"}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    d_msg = audit_deliverables(ws, ["outputs/variant_audit.json"]).format_gate_message()
    assert "size_callout" in d_msg and "size clearly encoded" in d_msg
    assert "cap_material_callout" in d_msg and "bamboo wood texture" in d_msg

    # (f) Google Trends quality_audit.json + (j) rebalance.json.
    (out_dir / "quality_audit.json").write_text(
        json.dumps(
            {
                "partial_data_datasets": [{"dataset_id": "set_airfryer", "partial_data_week_count": 1}],
                "empty_query_datasets": ["edge_empty", "set_airfryer_2024"],
                "sub_floor_brand_presences": [{"slug": "airfryer", "brand": "cosori", "twelve_month_avg": 0.0}],
                "monopoly_datasets": [{"slug": "airfryer", "leader": "ninja", "leader_share": 1.0}],
            }
        ),
        encoding="utf-8",
    )
    f_msg = audit_deliverables(ws, ["outputs/quality_audit.json"]).format_gate_message()
    assert "set_airfryer_2024" in f_msg and "set_headset" in f_msg and "today 12-m" in f_msg

    (out_dir / "rebalance.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {"quarter": "Q2", "slot": "drill", "qoq_growth": -0.2731, "action": "swap_in:dewalt"},
                    {"quarter": "Q3", "slot": "drill", "qoq_growth": -0.0339, "action": "swap_in:dewalt"},
                    {"quarter": "Q2", "slot": "earbuds", "qoq_growth": -0.15, "action": "swap_in:beats studio buds"},
                ]
            }
        ),
        encoding="utf-8",
    )
    j_msg = audit_deliverables(ws, ["outputs/rebalance.json"]).format_gate_message()
    assert "-0.2527" in j_msg and "hold_watch" in j_msg and "jabra elite" in j_msg

    # (g) Alibaba ladderPrice & attr_diameter + (h) Cargo insurance settlement + (i) Travel tour handoff.
    (out_dir / "published_listing.json").write_text(
        json.dumps(
            {
                "attr_diameter": "DN50 (2 Inch)",
                "ladderPrice": [{"quantity": "10-49", "price": "124.01"}, {"quantity": ">=500", "price": "105.00"}],
            }
        ),
        encoding="utf-8",
    )
    g_msg = audit_deliverables(ws, ["outputs/published_listing.json"]).format_gate_message()
    assert "500+" in g_msg and 'DN50 (2")' in g_msg

    (out_dir / "insurance_settlement.json").write_text(
        json.dumps(
            {
                "settlements": [
                    {"customer_id": "LINNEA", "insurer": "PingAn", "claim_status": "paid_partial", "reason_short": "ICC(A) $500 deductible applied"},
                    {"customer_id": "JONAS", "insurer": "Allianz", "claim_status": "no_claim", "reason_short": "Allianz ICC(B) top-tier dry"},
                    {"customer_id": "OCEANTECH", "insurer": "BaoViet", "claim_status": "denied", "reason_short": "BaoViet ICC(C) water damage"},
                ]
            }
        ),
        encoding="utf-8",
    )
    h_msg = audit_deliverables(ws, ["outputs/insurance_settlement.json"]).format_gate_message()
    assert "26,400" in h_msg and "top tier" in h_msg and "not covered" in h_msg

    (out_dir / "audit_findings.json").write_text(
        json.dumps(
            {
                "findings": [
                    {"leg_id": "bangkok-amenity-claim", "error_type": "amenity_mismatch", "correct_value": "Novotel Bangkok Platinum Pratunam"},
                    {"leg_id": "l2", "error_type": "ok", "correct_value": "v2"},
                    {"leg_id": "l3", "error_type": "ok", "correct_value": "v3"},
                ]
            }
        ),
        encoding="utf-8",
    )
    i_msg = audit_deliverables(ws, ["outputs/audit_findings.json"]).format_gate_message()
    assert "Paradox Bangkok Sukhumvit" in i_msg


