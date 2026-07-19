"""Deterministic digest profile registry (SPEC §8, §9).

Detection is deterministic and explainable: profiles are probed in a fixed
order and each detection records why it matched. A profile may decline and
fall back to ``text/v1``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ctx import POLICY_VERSION
from ctx.digest.base import DigestContext, Profile
from ctx.digest.jsonprof import JsonLinesProfile, JsonProfile
from ctx.digest.lintprof import LintProfile
from ctx.digest.searchprof import SearchProfile
from ctx.digest.logprof import LogTemplateProfile
from ctx.digest.moreprofs import BuildProfile, GitDiffProfile, GoTestProfile, JestProfile
from ctx.digest.pytestprof import PytestProfile
from ctx.digest.text import TextProfile
from ctx.execution import focus_hash, update_manifest_digest
from ctx.store import Store
from ctx.textutil import decode_stream, sanitize_for_model
from ctx.workspace import Workspace

# Fixed probe order — first match wins; text/v1 always matches last.
_PROFILES: tuple[Profile, ...] = (
    PytestProfile(),
    GoTestProfile(),
    JestProfile(),
    GitDiffProfile(),
    LintProfile(),  # before Build/LogTemplate: both would misclaim lint shapes
    SearchProfile(),  # grep/rg file:line:content — AFTER Lint: diagnostics
    #                   (which share the shape but carry severity tokens) win first
    BuildProfile(),
    JsonLinesProfile(),
    JsonProfile(),
    LogTemplateProfile(),
    TextProfile(),
)


def detect_profile(ctx: DigestContext) -> tuple[Profile, str]:
    for profile in _PROFILES:
        reason = profile.detect(ctx)
        if reason:
            return profile, reason
    return _PROFILES[-1], "fallback"  # pragma: no cover - text always matches


def render_run_digest(
    store: Store,
    ws: Workspace,
    manifest: dict[str, Any],
    *,
    focus: str | None = None,
    op: str = "run",
    dense: bool = False,
    plan: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Produce the bounded deterministic digest for a captured invocation and
    republish the manifest with its final digest identity.

    ``op`` names the verb for telemetry attribution only (`ctx gain` by-verb
    rows); it never participates in digest bytes or content identity.

    ``dense`` is the reflex arc's densify-on-starvation switch (docs/REFLEX.md
    layer 3): profiles may render the full census instead of first-failure
    detail. The flag itself is never written into digest meta — identity
    remains a pure function of the rendered bytes, and the caller declares
    the densified rendering in the *printed* header only.

    ``plan`` is the resolver's DeliveryPlan (docs/EDC.md §5.4), duck-typed —
    the caller (cli) resolves it once and hands it through; profiles that
    honor plans (pytest/v2) obey its mode/budget knobs, others ignore it.
    Like ``dense``, the plan selects among deterministic renderings; digest
    identity remains a pure function of the rendered bytes.

    Returns (digest_text, final_manifest).
    """
    ctx = DigestContext.load(store, ws, manifest, focus=focus)
    ctx.dense = bool(dense)
    ctx.plan = plan
    profile, reason = detect_profile(ctx)

    body = profile.render(ctx)
    body, redactions = sanitize_for_model(body, ws.config.redaction.patterns)
    if redactions:
        body += "\nredaction: applied [" + ", ".join(redactions) + "]"

    # Per-outcome profile versioning (EDC phase 3): a profile may declare
    # the version of the rendering it actually produced (pytest/v2 for
    # failure evidence, pytest/v1 for the byte-identical pass path).
    profile_version = ctx.meta_profile_version or profile.version

    digest_meta = {
        "profile": profile_version,
        "policy": POLICY_VERSION,
        "focusHash": focus_hash(focus),
        "bytesHash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    final_id, final_manifest = update_manifest_digest(store, manifest, digest_meta)
    short = final_id[:12]

    header = f"[ctx run:{short} profile={profile_version}]"
    digest = header + "\n" + body.replace("run:PENDING", f"run:{short}")

    from ctx.retrieval import record_telemetry

    raw = sum(int(s["bytes"]) for s in manifest["streams"].values())
    record_telemetry(store, op, raw, len(digest.encode("utf-8")))

    # Graduated engagement (mechanism C): an output too large to inline is
    # the measured proof the task outgrew "small" — graduate the session.
    if raw > ws.config.budgets.result_tokens * 3 and ws.config.engagement.mode == "auto":
        from ctx.engagement import note_truncation

        note_truncation(ws.root)
    return digest, final_manifest


def digest_output(
    store: Store,
    ws: Workspace,
    tool_name: str,
    stdout: str,
    stderr: str = "",
    *,
    is_error: bool = False,
) -> tuple[str, str]:
    """Digest an already-produced tool result (not a shell capture).

    The universal emission gate (``ctx.hook._emission_gate``) calls this when a
    PostToolUse tool result exceeds the byte budget: it persists the raw bytes
    losslessly, synthesizes a ``ctx.invocation/v1`` manifest, and reuses
    :func:`render_run_digest` to produce a bounded digest carrying a working
    ``ctx get run:<short>#stdout`` retrieval ref.

    ``argv`` is ``[tool_name]`` only — never the tool's arguments — so the
    command-anchored profiles (git-diff, pytest, search, build) decline and the
    result is classified purely on its *shape*. That is the if-ladder cure: a
    new tool needs no new code because dispatch is by output shape.

    Returns ``(bounded_digest_text, short_run_id)``.
    """
    out_b = stdout.encode("utf-8")
    err_b = stderr.encode("utf-8")
    out_hash = store.put_blob(out_b)
    err_hash = store.put_blob(err_b)

    def _stream(blob_hash: str, data: bytes) -> dict[str, Any]:
        size = len(data)
        _, encoding, media_type = decode_stream(data[:8192] if size else b"")
        lines = data.count(b"\n") + (0 if (not data or data.endswith(b"\n")) else 1)
        return {
            "blob": f"sha256:{blob_hash}",
            "bytes": size,
            "lines": lines,
            "mediaType": media_type if size else "text/plain",
            "encoding": encoding if size else "utf-8",
        }

    manifest: dict[str, Any] = {
        "schema": "ctx.invocation/v1",
        "workspaceId": ws.workspace_id,
        "cwd": ".",
        "argv": [tool_name],
        "shell": False,
        "result": {"exitCode": 0, "signal": None, "timedOut": False},
        "streams": {
            "stdout": _stream(out_hash, out_b),
            "stderr": _stream(err_hash, err_b),
        },
        # Source nulled: the identity of a hook-captured result is a pure
        # function of (bytes, tool_name), so the same payload always mints the
        # same run id — no git head / worktree hash / timestamps.
        "source": {"gitHead": None, "worktreeHash": None},
        "digest": {
            "profile": "text/v1",
            "policy": POLICY_VERSION,
            "focusHash": focus_hash(None),
            "bytesHash": "sha256:" + "0" * 64,
        },
    }

    digest, final = render_run_digest(store, ws, manifest, focus=None)
    short = str(final.get("id", "")).removeprefix("sha256:")[:12]

    from ctx.engagement import filter_digest, suggestion_cap
    from ctx.textutil import bounded

    budget = (
        ws.config.budgets.result_tokens
        if "output (complete):" in digest
        else ws.config.budgets.digest_tokens
    )
    if is_error:
        budget = int(budget * ws.config.budgets.failure_budget_factor)
    eng = ws.config.engagement
    cap = suggestion_cap(ws.root, mode=eng.mode, lean_models=eng.lean_models)
    return bounded(filter_digest(digest, cap), budget), short
