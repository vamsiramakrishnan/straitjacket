"""Prefix-stability contract: every byte the harness injects into a model's
prompt prefix, locked behind a committed manifest.

Measured motivation (evals/matrix-2026-07-18.md): a 9-token change to the
wrap discipline prompt cold-invalidated every cached prefix, costing ~$0.21
per model per user in one-time cache rewrite and producing a phantom 2.9x
cost regression in benchmarks. Prefix text is therefore a cache-keyed asset:
changing it is allowed, but only as an explicit, versioned decision.

``PREFIX_VERSION`` must be bumped whenever any asset hash changes; the
golden-hash test fails otherwise. Regenerate the manifest with:

    python3 -c "from ctx.prefixassets import write_manifest; write_manifest()"

Layout rule enforced by construction: assets here are byte-stable
(no timestamps, paths, or session state) — anything session-variable
belongs in tool results or the message suffix, never the shared prefix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Bump ONLY when prefix-resident bytes change (the changelog entry must
# note that users pay one cold cache write per model). Invocation-loaded
# tiers (skill BODY, loaded on trigger) are tracked separately below and
# may change without a bump — that is the progressive-disclosure split.
# v2: solution ladder adopted into the wrap discipline prompt after a
# measured A/B win (evals/rtk-corpus eval doc).
# v3: backward planning adopted after a held-out A/B win on every axis
# (Tura wave): -17% cost, -16% turns, -18% output, more tests.
# v5: `ctx eval` renamed to `ctx py` and `ctx investigate` folded into
# `ctx plan run`; both names appear in prefix-resident teaching text.
# v6: MCP tool description now glosses all 14 declared ops (callers, callees,
# impact, diff, investigate were callable but absent from the prose catalogue,
# so a model could not discover them).
PREFIX_VERSION = 11

_MANIFEST_NAME = "prefix-manifest.json"


def manifest_path() -> Path:
    return Path(__file__).resolve().parent / _MANIFEST_NAME


def _split_skill(raw: bytes) -> tuple[bytes, bytes]:
    """(frontmatter, body) — the frontmatter description is prefix-resident;
    the body loads on invocation (progressive disclosure tiers)."""
    text = raw.decode("utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head = text[: end + 4]
            return head.encode("utf-8"), text[end + 4 :].encode("utf-8")
    return raw, b""


#: Which tracked assets are ACTUALLY resident in every session's prompt, as
#: opposed to loaded on demand. This was documented in prose and the prose was
#: misread — summing every tracked asset gives ~3,800 tokens and reads as "the
#: harness costs 3,800 tokens a session", when the true standing cost is ~708
#: and the rest is paid only when the skill triggers or a subagent spawns.
#:
#: Machine-readable because a docstring cannot be asserted. `resident_bytes()`
#: and `tests/test_prefix_budget.py` both read this, so the published number
#: and the classification can never drift apart, and the next person to ask
#: "what does this cost me per session" gets an answer they cannot misread.
RESIDENT_ASSETS = frozenset({
    "wrap.output_discipline",          # appended system prompt (wrap print mode)
    "mcp.tool_description",            # tool definitions are prefix content
    "skill.ctx-harness.frontmatter",   # the description is listed in the prompt
})

#: Tracked, cache-keyed, but NOT resident: paid only at the moment named.
DEFERRED_ASSETS = {
    "skill.ctx-harness.body": "loaded only when the skill triggers",
    "agent.ctx-explorer": (
        "only the frontmatter description enters the parent prompt; the body "
        "is paid inside the subagent, and only if one spawns"
    ),
}


def prefix_assets() -> dict[str, bytes]:
    """Every injected text that lands in a host prompt prefix, plus the
    invocation-loaded skill body (tracked, but changes to it are not
    cache-relevant — see PREFIX_VERSION policy above).

    Tracked here is NOT the same as resident — see ``RESIDENT_ASSETS``.

    - wrap discipline prompt (appended system prompt, wrap print mode)
    - explorer agent definition (agent description enters the system prompt)
    - MCP tool description (tool definitions are prefix content)
    - skill frontmatter (its description is listed in the prompt)
    - skill body (loaded only when the skill triggers)
    """
    from ctx.installer import _template_dir
    from ctx.mcp import TOOL_SCHEMA
    from ctx.wrap import _OUTPUT_DISCIPLINE

    template = _template_dir()
    skill_head, skill_body = _split_skill(
        (template / "skills" / "ctx-harness" / "SKILL.md").read_bytes()
    )
    assets: dict[str, bytes] = {
        "wrap.output_discipline": _OUTPUT_DISCIPLINE.encode("utf-8"),
        "mcp.tool_description": str(TOOL_SCHEMA["description"]).encode("utf-8"),
        "agent.ctx-explorer": (template / "agents" / "ctx-explorer.md").read_bytes(),
        "skill.ctx-harness.frontmatter": skill_head,
        "skill.ctx-harness.body": skill_body,
    }
    return assets


def compute_manifest() -> dict:
    return {
        "schema": "ctx.prefix/v1",
        "prefix_version": PREFIX_VERSION,
        "assets": {
            name: "sha256:" + hashlib.sha256(data).hexdigest()
            for name, data in sorted(prefix_assets().items())
        },
    }


def load_manifest() -> dict | None:
    try:
        return json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_manifest() -> Path:
    path = manifest_path()
    path.write_text(json.dumps(compute_manifest(), indent=2) + "\n", encoding="utf-8")
    return path


def check() -> list[str]:
    """Return human-readable violations (empty = prefix contract holds)."""
    stored = load_manifest()
    if stored is None:
        return [f"prefix manifest missing/unreadable at {manifest_path()}"]
    current = compute_manifest()
    problems: list[str] = []
    if stored.get("prefix_version") != current["prefix_version"]:
        problems.append(
            "PREFIX_VERSION mismatch: code says "
            f"{current['prefix_version']}, manifest says {stored.get('prefix_version')}"
        )
    stored_assets = stored.get("assets") or {}
    for name, digest in current["assets"].items():
        if name not in stored_assets:
            problems.append(f"new prefix asset not in manifest: {name}")
        elif stored_assets[name] != digest:
            problems.append(f"prefix asset changed: {name}")
    for name in stored_assets:
        if name not in current["assets"]:
            problems.append(f"manifest lists removed asset: {name}")
    return problems


def resident_bytes() -> dict[str, int]:
    """Byte cost of what is actually in every session's prompt.

    The explorer agent contributes only its frontmatter ``description`` — the
    host lists that so the parent can choose the agent; the body travels with
    the subagent. Counting the whole file overstates the standing cost by
    ~530 tokens, which is most of how the ~3,800 misreading happened.
    """
    import re

    assets = prefix_assets()
    out = {name: len(assets[name]) for name in sorted(RESIDENT_ASSETS)}
    agent = assets.get("agent.ctx-explorer", b"").decode("utf-8", "replace")
    head = re.search(r"^---\n(.*?)\n---\n", agent, re.S)
    desc = re.search(r"description:\s*>-\n((?:[ \t]{2,}.*\n)+)", head.group(1)) if head else None
    if desc:
        out["agent.ctx-explorer(description)"] = len(desc.group(1).encode("utf-8"))
    return out


def budget_report() -> str:
    """Human-readable split of resident vs deferred, for `ctx doctor` and docs."""
    lines = ["prefix budget — what the harness costs a session", ""]
    resident = resident_bytes()
    total = sum(resident.values())
    lines.append("RESIDENT (every session):")
    for name, n in sorted(resident.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {name:38} {n:>6,} B  ~{n // 4:>5,} tok")
    lines.append(f"  {'TOTAL':38} {total:>6,} B  ~{total // 4:>5,} tok")
    lines.append("")
    lines.append("DEFERRED (paid only when named):")
    assets = prefix_assets()
    for name, when in DEFERRED_ASSETS.items():
        n = len(assets.get(name, b""))
        lines.append(f"  {name:38} {n:>6,} B  ~{n // 4:>5,} tok  — {when}")
    return "\n".join(lines)
