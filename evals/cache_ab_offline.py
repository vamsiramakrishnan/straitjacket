"""Non-billable measurements for the matched cache/context experiment.

This module invokes only local ctx commands. Token counts use the pinned shared
comparison tokenizer; they are not Anthropic usage or evidence of cache hits.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.metadata
import json
import re
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from evals.field_needle import (
    LOUD_NEEDLE_MARK, QUIET_NEEDLE_LINE, QUIET_NEEDLE_MARK,
    SEED, TOTAL_LINES, build_corpus, run_caveman,
)

TOKENIZER_VERSION = "0.11.0"
TOKENIZER_ENCODING = "o200k_base"
CORPUS_SHA256 = "3ea53b7bc88fb21f7c86189e39938e77444fff9cc1c5f8d1814a76275c60ce1d"
_RUN_HANDLE = re.compile(r"run:[0-9a-f]{12}\b")
_CANONICAL_RUN_HANDLE = "run:8956c843e556"


@functools.lru_cache(maxsize=1)
def _encoder():
    import tiktoken

    if importlib.metadata.version("tiktoken") != TOKENIZER_VERSION:
        raise RuntimeError(f"offline comparison requires tiktoken=={TOKENIZER_VERSION}")
    return tiktoken.get_encoding(TOKENIZER_ENCODING)


def count_tokens(text: str) -> int:
    return len(_encoder().encode(text, disallowed_special=()))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ctx(root: Path, state: Path, args: list[str]) -> dict:
    from evals.cache_ab_tools import child_env, ctx_command

    start = time.perf_counter()
    result = subprocess.run(
        ctx_command(["--workspace", str(root), *args]), cwd=root,
        env=child_env(state), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=False,
    )
    return {"argv": ["ctx", *args], "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8"),
            "stderr": result.stderr.decode("utf-8"),
            "elapsed_s": time.perf_counter() - start}


def _prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # All probes force the local picker, independently of account settings.
    (root / "ctx.toml").write_text(
        'version = 1\n[picker]\nmode = "local"\n', encoding="utf-8")


def _retrieved_lines(text: str) -> dict[int, str]:
    return {int(n): body for n, body in re.findall(r"^L(\d+): (.*)$", text, re.M)}


def _stored_stdout(state: Path, short_id: str) -> bytes:
    matches = list(state.rglob(f"manifests/{short_id}*.json"))
    if len(matches) != 1:
        raise RuntimeError(f"expected one capture manifest, got {len(matches)}")
    manifest = json.loads(matches[0].read_text(encoding="utf-8"))
    stream = manifest["streams"]["stdout"]
    digest = stream["blob"].removeprefix("sha256:")
    return (matches[0].parent.parent / "blobs" / "sha256" / digest[:2] / digest[2:]).read_bytes()


def field_probe(out: Path, repeats: int = 3) -> dict:
    """Run each capture once in a fresh workspace, avoiding ctx densification."""
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    raw = build_corpus()
    raw_bytes = raw.encode("utf-8")
    if sha256(raw_bytes) != CORPUS_SHA256:
        raise RuntimeError("the existing field fixture changed; review the experiment")
    lines = raw.splitlines()
    rows = []
    for repeat in range(repeats):
        with tempfile.TemporaryDirectory(prefix="cab-") as td:
            base = Path(td)
            root, state = base / "ws", base / "state"
            _prepare(root)
            (root / "corpus.log").write_bytes(raw_bytes)
            capture = _ctx(root, state, ["run", "--shell", "cat corpus.log"])
            if capture["exit_code"] != 0:
                raise RuntimeError(f"field capture failed: {capture}")
            digest = capture["stdout"]
            match = _RUN_HANDLE.search(digest)
            if match is None:
                raise RuntimeError("capture did not return a run handle")
            handle = match.group()
            stored = _stored_stdout(state, handle.removeprefix("run:"))
            retrieval = _ctx(root, state, ["get", handle, "--lines", "14228:14248"])
            omitted = _ctx(root, state, ["get", handle, "--lines", "123:123"])
            expected = {n: lines[n - 1] for n in range(14228, 14249)}
            canonical = _RUN_HANDLE.sub(_CANONICAL_RUN_HANDLE, digest)
            row = {
                "repeat": repeat, "capture": capture, "retrieval": retrieval,
                "omitted_line_retrieval": omitted,
                "actual_visible_tokens": count_tokens(digest),
                "canonical_visible_tokens": count_tokens(canonical),
                "canonical_digest_sha256": sha256(canonical.encode()),
                "quiet_needle_survived": QUIET_NEEDLE_MARK in digest,
                "loud_needle_survived": LOUD_NEEDLE_MARK in digest,
                "stored_bytes": len(stored), "stored_sha256": sha256(stored),
                "raw_round_trip_exact": stored == raw_bytes,
                "retrieval_exact": retrieval["exit_code"] == 0 and
                    _retrieved_lines(retrieval["stdout"]) == expected,
                "omitted_line_absent_from_digest": lines[122] not in digest,
                "omitted_line_retrieval_exact": omitted["exit_code"] == 0 and
                    _retrieved_lines(omitted["stdout"]) == {123: lines[122]},
            }
            rows.append(row)
            _write_json(out / f"field-{repeat:02d}.json", row)
    checks = ("quiet_needle_survived", "loud_needle_survived", "raw_round_trip_exact",
              "retrieval_exact", "omitted_line_absent_from_digest", "omitted_line_retrieval_exact")
    return {
        "fixture": "evals.field_needle.build_corpus", "seed": SEED,
        "lines": TOTAL_LINES, "bytes": len(raw_bytes), "sha256": sha256(raw_bytes),
        "raw_tokens": count_tokens(raw), "repeats": rows,
        "median_capture_s": statistics.median(r["capture"]["elapsed_s"] for r in rows),
        "median_retrieval_s": statistics.median(r["retrieval"]["elapsed_s"] for r in rows),
        "head_tail_control": run_caveman(raw, count_tokens),
        "all_integrity_checks_passed": all(row[key] for row in rows for key in checks),
        "canonicalization": "only run:<12 hex> -> run:8956c843e556 for additional comparison count",
        "scope": "local wall-clock latency, comparison output tokens, literal evidence survival; no model",
    }


def compression_probe() -> dict:
    """Whole-paragraph compression must refuse an unsatisfiable protected span."""
    with tempfile.TemporaryDirectory(prefix="cab-") as td:
        base = Path(td)
        root, state = base / "ws", base / "state"
        _prepare(root)
        raw = build_corpus()
        (root / "corpus.log").write_text(raw, encoding="utf-8")
        result = _ctx(root, state, ["compress", "corpus.log", "--query", QUIET_NEEDLE_MARK,
                      "--budget-bytes", "2048", "--require-line", str(QUIET_NEEDLE_LINE)])
        parsed = json.loads(result["stdout"])
        return {"command": result, "result": parsed,
                "expected_refusal": result["exit_code"] == 3 and
                    parsed["status"] == "needs_retrieval" and
                    parsed["required_bytes"] == len(raw.encode()) and parsed["text"] == "",
                "interpretation": "The log is one paragraph. ctx compress protects that paragraph; "
                    "ctx run's logtemplate digest is a different transformation."}


def prefix_probe() -> dict:
    """Exercise PR #59's visible-byte hash/redaction/serialization regression."""
    with tempfile.TemporaryDirectory(prefix="cab-") as td:
        base = Path(td)
        root, state = base / "ws", base / "state"
        _prepare(root)
        secret = "AKIAIOSFODNN7EXAMPLE"  # Public AWS documentation example, not a credential.
        (root / "reference.txt").write_text(f'Référence "{secret}"\n', encoding="utf-8")
        (root / "evidence.txt").write_text(
            f"label=\x1b[31mred\x1b[0m\nkey={secret}\n", encoding="utf-8")
        rows = []
        for query in ("label", "key"):
            result = _ctx(root, state, ["compress", "evidence.txt", "--query", f"{query}: {secret}",
                                       "--cache-prefix", "reference.txt"])
            parsed = json.loads(result["stdout"])
            wrap = parsed["cache_wrap"]
            prefix = json.loads(wrap["prefix"])
            suffix = json.loads(wrap["suffix"])
            rows.append({"query_topic": query, "exit_code": result["exit_code"],
                         "visible_result": parsed,
                         "hash_matches_visible_utf8": wrap["prefix_sha256"] == sha256(wrap["prefix"].encode()),
                         "suffix_matches_visible_evidence": suffix["evidence"] == parsed["text"],
                         "prompt_is_exact_concatenation": wrap["prompt"] == wrap["prefix"] + wrap["suffix"],
                         "unicode_preserved": "Référence" in prefix["reference_context"],
                         "example_key_redacted": secret not in result["stdout"] and
                             "[ctx:redacted:aws-access-key:" in prefix["reference_context"],
                         "controls_stripped": "label=red" in suffix["evidence"],
                         "dynamic_query_preserved": suffix["query"].startswith(query + ": ")})
    prefixes = [row["visible_result"]["cache_wrap"]["prefix"] for row in rows]
    suffixes = [row["visible_result"]["cache_wrap"]["suffix"] for row in rows]
    checks = ("hash_matches_visible_utf8", "suffix_matches_visible_evidence", "prompt_is_exact_concatenation",
              "unicode_preserved", "example_key_redacted", "controls_stripped", "dynamic_query_preserved")
    return {"rows": rows, "prefix_stable": prefixes[0] == prefixes[1],
            "suffix_changes": suffixes[0] != suffixes[1],
            "all_serialization_checks_passed": all(row["exit_code"] == 0 for row in rows)
                and all(row[key] for row in rows for key in checks),
            "provider_cache_hit_proven": None}


def referee_calibration() -> list[dict]:
    from evals.cache_ab_tasks import apply_gold, build_fixture, grade
    from evals.coding_suite import REGISTRY

    rows = []
    with tempfile.TemporaryDirectory(prefix="cab-") as td:
        for index, task in enumerate(REGISTRY):
            fixture = build_fixture(task.name, Path(td) / f"{index:02d}")
            initial = grade(fixture)
            apply_gold(fixture)
            repaired = grade(fixture)
            rows.append({"task": task.name, "initial": initial, "known_repair": repaired,
                         "calibration_passed": not initial["accepted"] and repaired["accepted"],
                         "model_success": None})
    return rows


def run_offline(out: Path, *, field_repeats: int = 3, include_replay: bool = True) -> dict:
    """Write a receipt. Refuse an existing receipt to avoid replacing evidence."""
    out = Path(out)
    if (out / "receipt.json").exists():
        raise FileExistsError(f"receipt already exists: {out}")
    out.mkdir(parents=True, exist_ok=True)
    # Import/validate the optional tokenizer before executing any experiment.
    count_tokens("")
    receipt = {
        "schema": "straitjacket.cache-ab.offline/v1",
        "tokenizer": {"package": "tiktoken", "version": TOKENIZER_VERSION, "encoding": TOKENIZER_ENCODING},
        "provider_calls": 0,
        "provider_usage": {"input_tokens": None, "output_tokens": None,
                           "cache_read_input_tokens": None, "cache_creation_input_tokens": None},
        "provider_latency_s": None, "model_task_success": None, "provider_cost_usd": None,
        "field": field_probe(out / "field", field_repeats),
        "prefix": prefix_probe(), "compression": compression_probe(),
        "referee_calibration": referee_calibration(),
    }
    if include_replay:
        from evals.cache_ab_replay import run_replay

        receipt["scripted_replay"] = run_replay(out / "replay")
    receipt["all_checks_passed"] = (
        receipt["field"]["all_integrity_checks_passed"] and
        receipt["prefix"]["all_serialization_checks_passed"] and
        receipt["prefix"]["prefix_stable"] and receipt["prefix"]["suffix_changes"] and
        receipt["compression"]["expected_refusal"] and
        all(row["calibration_passed"] for row in receipt["referee_calibration"])
    )
    _write_json(out / "receipt.json", receipt)
    return receipt
