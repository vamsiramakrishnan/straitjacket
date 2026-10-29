#!/usr/bin/env python3
"""straitjacket on headroom's own published benchmark.

headroom's landing-page numbers (21-57% savings, 41% total) come from one
seeded, offline script, ``benchmarks/index_proof_table.py`` in
https://github.com/headroomlabs-ai/headroom: four synthetic scenarios from
their generators, each tool payload JSON-dumped, tokens counted with their
tokenizer before and after their ``compress()``. No model, no task, no
quality check — a statement about token counts.

This script runs the same corpus (same generators, same seed, same
tokenizer, same before-count) through two things:

* ``headroom`` — their ``compress()`` in both configurations their script
  reports (default, and ``protect_recent=0`` which is the table they publish).
* ``sj`` — what a Claude Code session sees under straitjacket's PostToolUse
  emission gate: a tool result at or under the byte budget (16 KB by default)
  passes through byte-identical; one over it is stored losslessly and replaced
  by the bounded digest with a ``run:`` handle (`ctx.digest.digest_output`,
  the function the gate calls).

and then asks the question their table does not: **what survived?** Each
generator plants things an agent would need (ERROR log entries, anomalous
database rows, open bug issues, the real auth paths, the repositories in a
code search). For every mode the script counts how many of those needles are
still visible in the text the model receives, and for sj it also prices the
one retrieval that reaches a needle the digest does not show
(``ctx search run:<id>#stdout <needle>``), because a handle is only worth
something if following it is cheap.

    python3 evals/headroom_proof_table.py --headroom-repo /path/to/headroom

Needs an environment with both packages importable (headroom-ai and ctx) and
tiktoken, which headroom's tokenizer uses. Deterministic for a seed.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
MODEL = "gpt-5.6"          # what index_proof_table.py measures with
DEFAULT_SEED = 20260902    # the seed their published table names
GATE_BYTES = 16384         # ctx's default emission-gate budget


# ------------------------------------------------------------ needles
def needles_for(tool: dict[str, Any]) -> tuple[str, list[str]]:
    """(what the needles are, the needle strings) for one generated tool
    payload — the items the scenario's own question is about."""
    kind = tool.get("tool")
    r = tool["result"]
    if kind == "search_logs":
        ids = [e["trace_id"] for e in r["entries"] if e["level"] == "ERROR"]
        return "trace ids of the ERROR entries", ids
    if kind == "database_query":
        ids = [row["user_id"] for row in r["rows"]
               if row["total_revenue"] >= 50000 or row["status"] == "suspended"]
        return "anomalous rows (revenue ≥ 50k or suspended)", ids
    if kind == "github_list_issues":
        ids = [str(i["number"]) for i in r["items"] if i["state"] == "open" and "bug" in i["labels"]]
        return "open issues labelled bug", ids
    if kind == "search_files":
        paths = [m["path"] for m in r["matches"] if not m["path"].startswith("src/modules/module_")]
        return "the real (non-template) file paths", paths
    if kind == "github_search_code":
        repos = sorted({i["repository"]["full_name"] for i in r["items"]})
        return "distinct repositories in the results", repos
    if kind == "filesystem_tree":
        names = [c["name"] for c in r["children"] if c["type"] == "directory"]
        return "top-level directories", names
    return "n/a", []


def visible(text: str, needles: list[str]) -> int:
    return sum(1 for n in needles if n in text)


# ------------------------------------------------------------ modes
def headroom_after(tools: list[dict], config) -> tuple[list[str], list[str]]:
    """Per-tool text after headroom's compress(), in their message shape,
    and the transforms it reports applying."""
    from headroom import compress

    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Analyse the tool output and answer."},
        *[{"role": "tool", "tool_call_id": f"call_{i}", "content": json.dumps(t["result"])}
          for i, t in enumerate(tools)],
    ]
    result = compress(msgs, model=MODEL, config=config)
    out = [m.get("content") for m in result.messages
           if m.get("role") == "tool" and isinstance(m.get("content"), str)]
    return out, sorted(set(result.transforms_applied))


def sj_after(tools: list[dict], ws, store, *, gate_bytes: int) -> list[tuple[str, str | None]]:
    """Per-tool (text the model sees, run id or None) under the emission gate."""
    from ctx.digest import digest_output

    out = []
    for t in tools:
        payload = json.dumps(t["result"])
        if len(payload.encode("utf-8")) <= gate_bytes:
            out.append((payload, None))
            continue
        text, short = digest_output(store, ws, "Bash", payload)
        out.append((text, short))
    return out


def sj_retrieve(ws, store, short: str, needle: str) -> str:
    from ctx.retrieval import search

    return search(store, ws, f"run:{short}#stdout", [needle], fixed=True, max_matches=5)


# ------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headroom-repo", default=os.environ.get("HEADROOM_REPO", "/home/user/headroomlabs-ai/headroom"))
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--gate-bytes", type=int, default=GATE_BYTES)
    ap.add_argument("--out", default=str(HERE / "agentbench" / "results" / "headroom_proof_table.json"))
    ns = ap.parse_args()

    bench = pathlib.Path(ns.headroom_repo) / "benchmarks"
    if not (bench / "index_proof_table.py").is_file():
        raise SystemExit(f"headroom checkout not found at {ns.headroom_repo} (clone headroomlabs-ai/headroom)")
    sys.path.insert(0, str(bench))
    from real_world_agent_benchmark import (  # noqa: E402
        create_codebase_exploration_scenario,
        create_issue_triage_scenario,
        create_sre_debugging_scenario,
        generate_github_code_search,
        seed_everything,
    )
    from headroom import CompressConfig  # noqa: E402
    from headroom.providers.openai_compatible import OpenAICompatibleTokenCounter  # noqa: E402

    tok = OpenAICompatibleTokenCounter(model=MODEL)
    count = tok.count_text

    def build():
        # Same order as index_proof_table.py: the generators share one RNG.
        seed_everything(ns.seed)
        return [
            ("Code search (100 results)",
             [generate_github_code_search("JWT authentication middleware", num_results=100)]),
            ("SRE incident debugging", create_sre_debugging_scenario().tools),
            ("Codebase exploration", create_codebase_exploration_scenario().tools),
            ("GitHub issue triage", create_issue_triage_scenario().tools),
        ]

    # ctx: a throwaway workspace and store, so nothing here touches a real one.
    # A fixed repo_key pins the workspace id, and with it the run ids in the
    # digests, so the token counts are byte-stable across runs and machines.
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sj-headroom-"))
    (tmp / "ws").mkdir()
    (tmp / "ws" / "ctx.toml").write_text('version = 1\nrepo_key = "sj-headroom-proof-table"\n', encoding="utf-8")
    os.environ["CTX_STATE_HOME"] = str(tmp / "state")
    from ctx.store import Store  # noqa: E402
    from ctx.workspace import resolve_workspace  # noqa: E402

    ws = resolve_workspace(str(tmp / "ws"))
    store = Store(ws.workspace_id)

    rows: list[dict[str, Any]] = []
    for label, tools in build():
        payloads = [json.dumps(t["result"]) for t in tools]
        before = sum(count(p) for p in payloads)
        row: dict[str, Any] = {"scenario": label, "tools": [t["tool"] for t in tools], "before": before, "modes": {}}
        needle_sets = [needles_for(t) for t in tools]
        row["needles"] = [{"tool": t["tool"], "what": w, "n": len(ids)} for t, (w, ids) in zip(tools, needle_sets)]

        # headroom, both configurations their script prints
        for cname, cfg in (("headroom default (protect_recent=4)", CompressConfig()),
                           ("headroom full (protect_recent=0)", CompressConfig(protect_recent=0))):
            seed_everything(ns.seed)  # compress() may draw randomness; keep the corpus fixed regardless
            texts, transforms = headroom_after(tools, cfg)
            after = sum(count(t) for t in texts)
            vis = [visible(t, ids) for t, (_w, ids) in zip(texts, needle_sets)]
            row["modes"][cname] = {
                "after": after, "savings_pct": round((before - after) / before * 100, 1),
                "needles_visible": sum(vis), "needles_total": sum(len(ids) for _w, ids in needle_sets),
                "per_tool_after": [count(t) for t in texts], "transforms": transforms,
            }

        # sj: the emission gate, then one retrieval per hidden needle sample
        seen = sj_after(tools, ws, store, gate_bytes=ns.gate_bytes)
        after = sum(count(t) for t, _ in seen)
        vis = [visible(t, ids) for (t, _), (_w, ids) in zip(seen, needle_sets)]
        retrievals = []
        for (text, short), (_w, ids) in zip(seen, needle_sets):
            if short is None or not ids:
                continue
            hidden = [n for n in ids if n not in text][:3]
            for n in hidden:
                hit = sj_retrieve(ws, store, short, n)
                retrievals.append({"needle": n, "found": n in hit, "tokens": count(hit)})
        row["modes"]["sj emission gate (16 KB, digest + handle)"] = {
            "after": after, "savings_pct": round((before - after) / before * 100, 1),
            "needles_visible": sum(vis), "needles_total": sum(len(ids) for _w, ids in needle_sets),
            "per_tool_after": [count(t) for t, _ in seen],
            "gated": sum(1 for _, s in seen if s), "passed_through": sum(1 for _, s in seen if not s),
            "retrievals": retrievals,
            "retrieval_found": sum(1 for r in retrievals if r["found"]),
            "retrieval_tokens_median": sorted(r["tokens"] for r in retrievals)[len(retrievals) // 2] if retrievals else None,
        }
        rows.append(row)

    store.close()
    payload = {"schema": "sj.headroom_proof_table/v1", "seed": ns.seed, "model": MODEL,
               "tokenizer": "tiktoken via headroom OpenAICompatibleTokenCounter",
               "gate_bytes": ns.gate_bytes, "headroom_repo": str(pathlib.Path(ns.headroom_repo)),
               "rows": rows}
    pathlib.Path(ns.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(ns.out).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")

    modes = list(rows[0]["modes"])
    print(f"seed={ns.seed} model={MODEL} gate={ns.gate_bytes} B\n")
    print("| scenario | before | " + " | ".join(f"{m}: after (saved) · needles" for m in modes) + " |")
    print("|---|---:|" + "---:|" * len(modes))
    tb = sum(r["before"] for r in rows)
    for r in rows:
        cells = []
        for m in modes:
            d = r["modes"][m]
            cells.append(f"{d['after']:,} ({d['savings_pct']:.0f}%) · {d['needles_visible']}/{d['needles_total']}")
        print(f"| {r['scenario']} | {r['before']:,} | " + " | ".join(cells) + " |")
    cells = []
    for m in modes:
        ta = sum(r["modes"][m]["after"] for r in rows)
        nv = sum(r["modes"][m]["needles_visible"] for r in rows)
        nt = sum(r["modes"][m]["needles_total"] for r in rows)
        cells.append(f"{ta:,} ({(tb - ta) / tb * 100:.0f}%) · {nv}/{nt}")
    print(f"| **total** | {tb:,} | " + " | ".join(cells) + " |")
    allr = [x for r in rows for x in r["modes"][modes[-1]]["retrievals"]]
    if allr:
        print(f"\nsj retrieval of a hidden needle: {sum(1 for x in allr if x['found'])}/{len(allr)} found, "
              f"median {sorted(x['tokens'] for x in allr)[len(allr)//2]:,} tokens per `ctx search run:… <needle>`")
    print(f"wrote {ns.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
