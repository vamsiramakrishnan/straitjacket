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
PREFIX_VERSION = 6

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


def prefix_assets() -> dict[str, bytes]:
    """Every injected text that lands in a host prompt prefix, plus the
    invocation-loaded skill body (tracked, but changes to it are not
    cache-relevant — see PREFIX_VERSION policy above).

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
