"""Acceptance: reconstructing what an agent looked at, from a transcript alone.

The contract under test, in the order it matters:

1. **One instrument, every arm.** A native `Read`/`grep` trajectory and a ctx
   `get`/`def`/`search` trajectory over the *same* regions must score the same.
   If they do not, an A/B between them measures the extractor rather than the
   tools, and the result is decided before the first run.
2. **Coordinates beat arguments.** Where a result renders line numbers those
   are the evidence, so truncation is captured for free.
3. **Attribution is honest.** Whole-file guesses are marked `inferred`, paths
   outside the repository are refused, and a call that was never going to show
   source is `inert` rather than counted as a parser failure.
4. **Explored is not utilized.** Reads and edits are separate sets.
"""

import json

import pytest

from ctx.trajectory import (
    GOLD_SCHEMA,
    Region,
    extract,
    load_gold,
    score_regions,
    score_trajectory,
)

ROOT = "/repo"


def call(tool, inp, result=""):
    return {"tool": tool, "input": inp, "result": result, "is_error": False}


# ------------------------------------------------------- 1. arm symmetry


NATIVE = [
    call("Read", {"file_path": "/repo/src/app.py"},
         "   10→def handler():\n   11→    return 1\n   12→\n"),
    call("Grep", {"pattern": "handler"},
         "src/api.py:40:handler()\nsrc/api.py:41:  # call\n"),
]

CTX = [
    call("Bash", {"command": "ctx get repo:src/app.py --lines 10:12"},
         "L10: def handler():\nL11:     return 1\nL12: \n"),
    call("Bash", {"command": "ctx search repo:. handler"},
         "[ctx search repo:.]\nsrc/api.py:\n  L40: handler()\n  L41:   # call\n"),
]


def test_the_same_regions_score_the_same_with_and_without_ctx():
    """The load-bearing property. Everything else is detail."""
    a = extract(NATIVE, root=ROOT)
    b = extract(CTX, root=ROOT)
    assert {(r.path, r.start, r.end) for r in a.explored} == {
        (r.path, r.start, r.end) for r in b.explored
    }

    gold = [{"file": "src/app.py", "start": 10, "end": 12}]
    assert score_regions(a.explored, gold) == score_regions(b.explored, gold)


def test_a_ctx_arm_is_not_flattered_by_its_own_telemetry():
    """Both arms are parsed from rendered results, never from ctx state."""
    for traj in (extract(NATIVE, root=ROOT), extract(CTX, root=ROOT)):
        assert traj.by_basis.get("inferred", 0) == 0
        assert traj.parsed_share == 1.0


# ------------------------------------------- 2. coordinates beat arguments


def test_a_truncated_read_observes_only_what_was_rendered():
    traj = extract(
        [call("Read", {"file_path": "/repo/a.py", "offset": 1, "limit": 2000},
              "   1→x\n   2→y\n")],
        root=ROOT,
    )
    assert [(r.start, r.end) for r in traj.explored] == [(1, 2)]
    assert traj.explored[0].basis == "parsed"


def test_a_read_with_no_coordinates_falls_back_and_says_so():
    traj = extract(
        [call("Read", {"file_path": "/repo/a.py", "offset": 5, "limit": 10}, "plain text\n")],
        root=ROOT,
    )
    assert [(r.start, r.end, r.basis) for r in traj.explored] == [(5, 14, "inferred")]


def test_single_file_grep_is_attributed_from_the_command():
    """`grep -n PAT FILE` renders bare "123:text" with no path. Missing this
    silently under-counts the native arm, which is the arm ctx is measured
    against."""
    traj = extract(
        [call("Bash", {"command": 'grep -n "def x" -A 2 src/mod.py'},
              "40:def x():\n41-    pass\n")],
        root=ROOT,
    )
    assert [(r.path, r.start, r.end) for r in traj.explored] == [("src/mod.py", 40, 41)]


def test_a_ctx_def_credits_the_body_shown_not_the_span_claimed():
    """`ctx def`'s header names the definition's full extent, but `cmd_def`
    renders only the first ten lines once the body exceeds `max_inline_lines`.

    Crediting the header would hand the ctx arm recall over lines the model
    never saw, and deflate its cost per gold line — free marks for the arm
    this module exists to keep honest. The lines actually rendered are the
    evidence, exactly as for `Read` and `grep`.
    """
    header = (
        "[ctx def repo:src/m.py:foo · engine ast]\n"
        "definition: repo:src/m.py L240:336@c27214bc (function)\n"
        "body: 97 lines — showing first 10 · full body: ctx get repo:src/m.py --span ab\n"
    )
    shown = "".join(f"L{n}: line {n}\n" for n in range(240, 250))
    traj = extract([call("Bash", {"command": "ctx def repo:src/m.py:foo"}, header + shown)],
                   root=ROOT)
    assert [(r.path, r.start, r.end) for r in traj.explored] == [("src/m.py", 240, 249)]


def test_a_complete_ctx_def_body_credits_all_of_it():
    """The correction must not under-count the ordinary case: when the body
    fits, every line of it was rendered and every line counts."""
    header = (
        "[ctx def repo:src/m.py:foo · engine ast]\n"
        "definition: repo:src/m.py L10:12@c27214bc (function)\n"
        "body (complete):\n"
    )
    shown = "L10: def foo():\nL11:     return 1\nL12:\n"
    traj = extract([call("Bash", {"command": "ctx def repo:src/m.py:foo"}, header + shown)],
                   root=ROOT)
    assert [(r.path, r.start, r.end) for r in traj.explored] == [("src/m.py", 10, 12)]


def test_scattered_grep_hits_collapse_into_spans():
    traj = extract(
        [call("Grep", {}, "a.py:1:x\na.py:2:y\na.py:3:z\na.py:90:q\n")], root=ROOT
    )
    assert sorted((r.start, r.end) for r in traj.explored) == [(1, 3), (90, 90)]


# ------------------------------------------------- 3. attribution honesty


def test_a_path_outside_the_repository_is_refused():
    """A scratch log can never match a repo-relative annotation, but it can
    contribute thousands of 'observed' lines and drown the trajectory."""
    traj = extract(
        [call("Bash", {"command": "cat /tmp/build.log"}, "\n".join("x" * 3 for _ in range(50)))],
        root=ROOT,
    )
    assert traj.explored == []


def test_a_heredoc_write_is_not_a_read():
    traj = extract(
        [call("Bash", {"command": "cat >> src/a.py <<'EOF'\nprint(1)\nEOF"}, "")], root=ROOT
    )
    assert traj.explored == []
    assert traj.unattributed_calls == 0, "a write is inert, not a parser failure"


def test_a_pipe_stage_is_not_a_file_read():
    """`python … | tail -30` reads stdin. Counting it as a failed read is how
    the defect counter stops meaning anything."""
    traj = extract(
        [call("Bash", {"command": "python -c 'print(1)' | tail -30"}, "1\n")], root=ROOT
    )
    assert traj.unattributed_calls == 0 and traj.inert_calls == 1


def test_a_command_that_reads_nothing_is_inert_not_unattributed():
    traj = extract([call("Bash", {"command": "git push -u origin main"}, "done\n")], root=ROOT)
    assert traj.inert_calls == 1 and traj.unattributed_calls == 0


def test_a_read_that_cannot_be_placed_is_counted_as_a_defect():
    """`sed -n '/a/,/b/p' file` renders no coordinates and its range is a
    pattern, so it cannot be placed without reading the repo — which this
    module must not do. Counted, never guessed at."""
    traj = extract(
        [call("Bash", {"command": "sed -n '/^def a/,/^def b/p' src/m.py"}, "def a():\n")],
        root=ROOT,
    )
    assert traj.explored == [] and traj.unattributed_calls == 1


def test_an_enormous_whole_file_guess_is_refused():
    huge = "\n".join(str(i) for i in range(5000))
    traj = extract([call("Bash", {"command": "cat big.py"}, huge)], root=ROOT)
    assert traj.explored == []


def test_a_directory_listing_is_not_context():
    traj = extract([call("Glob", {"pattern": "**/*.py"}, "a.py\nb.py\n")], root=ROOT)
    assert traj.explored == [] and traj.inert_calls == 1


# ------------------------------------------- 4. explored versus utilized


def test_reads_and_edits_are_separate_sets():
    traj = extract(
        [
            call("Read", {"file_path": "/repo/a.py"}, "   1→x\n"),
            call("Read", {"file_path": "/repo/b.py"}, "   1→y\n"),
            call("Edit", {"file_path": "/repo/a.py", "old_string": "x"}, "ok"),
        ],
        root=ROOT,
    )
    scored = score_trajectory(traj, [{"file": "a.py", "start": 1, "end": 1}])
    assert scored["trajectory"]["explored_files"] == 2
    assert scored["trajectory"]["edited_files"] == 1
    assert scored["trajectory"]["explored_not_edited"] == 1


# ------------------------------------------------------------- scoring


def test_scoring_rewards_reaching_gold_and_punishes_dragnet():
    gold = [{"file": "a.py", "start": 10, "end": 20}]
    exact = [Region("a.py", 10, 20, "read", "parsed")]
    dragnet = exact + [Region(f"n{i}.py", 1, 200, "read", "inferred") for i in range(9)]

    tight, wide = score_regions(exact, gold), score_regions(dragnet, gold)
    assert tight["line"]["recall"] == wide["line"]["recall"] == 1.0
    assert wide["line"]["precision"] < tight["line"]["precision"]
    assert wide["file"]["precision"] < tight["file"]["precision"]


def test_a_block_needs_real_coverage_not_a_single_line():
    gold = [{"file": "a.py", "start": 1, "end": 100}]
    grazed = [Region("a.py", 50, 50, "grep", "parsed")]
    assert score_regions(grazed, gold)["block"]["recall"] == 0.0
    covered = [Region("a.py", 1, 60, "read", "parsed")]
    assert score_regions(covered, gold)["block"]["recall"] == 1.0


def test_density_and_regret_price_the_trajectory():
    traj = extract(
        [call("Read", {"file_path": "/repo/a.py"}, "".join(f"   {i}→x\n" for i in range(1, 11)))],
        root=ROOT,
    )
    scored = score_trajectory(traj, [{"file": "a.py", "start": 1, "end": 10}])
    assert scored["cost"]["evidence_density"] > 0
    assert scored["cost"]["gold_regret"] == (
        scored["cost"]["visible_tokens"] - scored["cost"]["gold_tokens"]
    )


def test_the_metric_has_one_definition(tmp_path):
    """`evals/contextbench.py` delegates to this module rather than keeping a
    second copy, so a tuned threshold cannot mean two things at once."""
    import sys

    sys.path.insert(0, str(tmp_path.parent))
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "evals" / "contextbench.py").read_text()
    assert "from ctx.trajectory import Region, score_regions" in src
    assert "def _prf(" not in src, "a second copy of the metric reappeared"


# ------------------------------------------------------------- gold files


def test_gold_round_trips(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({
        "schema": GOLD_SCHEMA, "instance_id": "i", "root": ROOT,
        "blocks": [{"file": "a.py", "start": 1, "end": 5}],
    }))
    assert load_gold(str(p))["blocks"] == [{"file": "a.py", "start": 1, "end": 5}]


def test_a_gold_file_of_the_wrong_schema_is_refused(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"schema": "something/v9", "blocks": []}))
    with pytest.raises(ValueError):
        load_gold(str(p))


def test_gold_with_no_usable_blocks_is_refused(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"schema": GOLD_SCHEMA, "blocks": [{"file": "a", "start": 9, "end": 2}]}))
    with pytest.raises(ValueError):
        load_gold(str(p))


def test_block_precision_cannot_exceed_one():
    """Numerator and denominator must count the same kind of thing.

    Block recall counts gold blocks; block precision counted them too, over a
    denominator of retrieved *regions*. One broad read covering two gold
    blocks therefore scored 2/1 = 2.0, with an F1 above one, and the value
    propagated into every ContextBench aggregate that averaged it.
    """
    gold = [
        {"file": "a.py", "start": 10, "end": 20},
        {"file": "a.py", "start": 30, "end": 40},
    ]
    one_broad_region = [Region("a.py", 1, 100, "read", "parsed")]
    scored = score_regions(one_broad_region, gold)

    assert scored["block"]["recall"] == 1.0, "both gold blocks are covered"
    assert 0.0 <= scored["block"]["precision"] <= 1.0
    assert 0.0 <= scored["block"]["f1"] <= 1.0


def test_block_precision_still_punishes_regions_that_reach_nothing():
    gold = [{"file": "a.py", "start": 10, "end": 20}]
    tight = [Region("a.py", 10, 20, "read", "parsed")]
    padded = tight + [Region(f"n{i}.py", 1, 50, "read", "parsed") for i in range(4)]
    assert score_regions(tight, gold)["block"]["precision"] == 1.0
    assert score_regions(padded, gold)["block"]["precision"] < 1.0


def test_an_emitted_gold_file_carries_no_root_and_must_be_given_one(tmp_path):
    """`--emit-gold` writes `root: ""` because the corpus cannot know where a
    tree was checked out. Left that way, `_relativize` refuses every absolute
    path — so a native arm's `Read {file_path: /abs/...}` scores as nothing
    while ctx's `repo:`-relative output scores fine, and the A/B measures the
    instrument rather than the tools. `ctx replay --gold` fills it from the
    cwd, or from `--gold-root`."""
    native = [call("Read", {"file_path": "/checkout/src/app.py"}, "   10→x\n   11→y\n")]

    unrooted = extract(native, root="")
    assert unrooted.explored == [], "an empty root refuses absolute paths"

    rooted = extract(native, root="/checkout")
    assert [(r.path, r.start, r.end) for r in rooted.explored] == [("src/app.py", 10, 11)]

    gold = [{"file": "src/app.py", "start": 10, "end": 11}]
    assert score_regions(rooted.explored, gold)["line"]["recall"] == 1.0
    assert score_regions(unrooted.explored, gold)["line"]["recall"] == 0.0
