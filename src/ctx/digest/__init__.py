"""Deterministic digest profile registry (SPEC §8, §9).

Detection is deterministic and explainable: profiles are probed in a fixed
order and each detection records why it matched. A profile may decline and
fall back to ``text/v1``.
"""

from __future__ import annotations

import re
import functools
import hashlib
from typing import Any

from ctx import POLICY_VERSION
from ctx.digest.base import DigestContext, Profile
from ctx.digest.binprof import BinaryProfile
from ctx.digest.jsonprof import JsonLinesProfile, JsonProfile
from ctx.digest.lintprof import LintProfile
from ctx.digest.searchprof import SearchProfile
from ctx.digest.logprof import LogTemplateProfile
from ctx.digest.moreprofs import (
    BuildProfile,
    CargoTestProfile,
    GitDiffProfile,
    GoTestProfile,
    JestProfile,
    UnittestProfile,
)
from ctx.digest.pytestprof import PytestProfile
from ctx.digest.tableprof import TableProfile
from ctx.digest.text import TextProfile
from ctx.execution import focus_hash, update_manifest_digest
from ctx.store import Store
from ctx.textutil import decode_stream, sanitize_for_model, short_id
from ctx.workspace import Workspace

# Fixed probe order — first match wins; text/v1 always matches last.
_PROFILES: tuple[Profile, ...] = (
    BinaryProfile(),  # magic-byte binary (image/pdf/…) before any text profile
    PytestProfile(),
    GoTestProfile(),
    CargoTestProfile(),  # shape-anchored on 'test result:' — compile-error
    #                      runs decline here and fall to Lint/Build below
    UnittestProfile(),  # 'Ran N tests' + OK/FAILED — vanilla unittest and
    #                     Django runtests.py (SWE-bench mine, 2026-07-19)
    JestProfile(),
    GitDiffProfile(),
    LintProfile(),  # before Build/LogTemplate: both would misclaim lint shapes
    SearchProfile(),  # grep/rg file:line:content — AFTER Lint: diagnostics
    #                   (which share the shape but carry severity tokens) win first
    BuildProfile(),
    JsonLinesProfile(),
    JsonProfile(),
    TableProfile(),  # caps-header aligned tables (docker/kubectl family);
    #                  strict header rule keeps logs and prose out
    LogTemplateProfile(),
    TextProfile(),
)


def detect_profile(ctx: DigestContext) -> tuple[Profile, str]:
    for profile in _PROFILES:
        reason = profile.detect(ctx)
        if reason:
            return profile, reason
    return _PROFILES[-1], "fallback"  # pragma: no cover - text always matches


@functools.lru_cache(maxsize=1)
def extractor_epoch() -> str:
    """Identity of the FACT-tier extraction the registry can currently do.

    Derived from the registry, never hand-maintained. The fact cache keys a
    derived run census on the manifest id alone, and a manifest id does not
    move when this file gains an extractor -- so every store that had already
    derived a unittest / Go / Cargo / Jest run kept serving the old
    pytest-only census after the upgrade that taught those profiles to
    extract, and `ctx q 'fails last'` went on answering "no failures" about a
    run with failures in it. The cached fingerprint carries this epoch beside
    the manifest id, so teaching a profile to extract, or bumping a profile
    version, invalidates exactly the derivations whose answer could change.

    A hand-bumped constant would have the same shape and the same failure:
    the bump is remembered on the day the extractor is written and forgotten
    every day after. Deriving it means the invalidation cannot be forgotten.
    """
    parts = sorted(
        f"{p.version}:{int(type(p).extract is not Profile.extract)}"
        for p in _PROFILES
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


# Lines a profile emits that are pure bookkeeping: provenance, byte counts, and
# accounting about what it chose to omit. Anything NOT matching is evidence the
# profile derived — a failure census, a span, a schema, a heavy-hitter table —
# and evidence is worth its bytes however small the output was.
_ACCOUNTING_LINE = re.compile(
    r"""^\s*(
        cwd: | command: | exit[: ] | signal[: ] | timed\ out
      | stdout: | stderr: | summary: | coverage:
      | parsed: | shown: | tests: | omitted
      | next: | ctx\             # retrieval affordances, not findings
      | ---\ stderr\ ---
    )""",
    re.VERBOSE,
)


def _only_accounting(body: str) -> bool:
    """True when the profile added no evidence — only bookkeeping."""
    for line in body.splitlines():
        if line.strip() and not _ACCOUNTING_LINE.match(line):
            return False
    return True


def _pass_through_if_digest_earned_nothing(ctx: Any, body: str, ws: Workspace) -> str:
    """Emit the output plainly when the digest around it earned no bytes.

    A profile's scaffolding pays for itself on a flood and not at all on two
    lines. Measured: a passing 98-byte pytest run rendered a 248-byte digest
    (2.5x) whose `coverage:` block spent five lines accounting for the omission
    of one line out of two — and the actual result line ("1 passed") was the
    thing omitted. Replaying this repo's own sessions showed short ones coming
    out worse under the harness than without it.

    The test is *evidence*, not size. An earlier attempt compared byte counts
    and suppressed pytest failure spans and JSON schema summaries, which are
    worth far more than their length. So pass through only when every line the
    profile produced is bookkeeping (:data:`_ACCOUNTING_LINE`) — any derived
    finding blocks it — and only when the whole output fits inline anyway. The
    run handle still addresses the stored capture, so nothing becomes
    unretrievable. Fail-open: any problem keeps the profile's rendering."""
    try:
        content = sum(v.bytes for v in (ctx.stdout, ctx.stderr) if v.bytes)
        if not content or content > ws.config.budgets.max_inline_bytes:
            return body  # large enough that the digest is doing real work
        if not _only_accounting(body):
            return body  # the profile found something; keep it
        r = ctx.manifest["result"]
        status = (
            f"exit {r['exitCode']}" if r.get("exitCode") is not None
            else f"signal {r.get('signal')}"
        )
        if r.get("timedOut"):
            status += " · timed out"
        out = [f"{status} · output (complete):"]
        for view in (ctx.stdout, ctx.stderr):
            if view.bytes:
                if view is ctx.stderr and ctx.stdout.bytes:
                    out.append("--- stderr ---")
                out.extend(view.text_lines)
        plain = "\n".join(out)
        return plain if len(plain.encode("utf-8")) < len(body.encode("utf-8")) else body
    except Exception:
        return body


def render_run_digest(
    store: Store,
    ws: Workspace,
    manifest: dict[str, Any],
    *,
    focus: str | None = None,
    op: str = "run",
    dense: bool = False,
    plan: Any = None,
    contained: bool = True,
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
    body = _pass_through_if_digest_earned_nothing(ctx, body, ws)
    body, redactions = sanitize_for_model(body, ws.config.redaction)
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
    short = short_id(final_id)

    header = f"[ctx run:{short} profile={profile_version}]"
    digest = header + "\n" + body.replace("run:PENDING", f"run:{short}")

    from ctx.retrieval import record_telemetry

    raw = sum(int(s["bytes"]) for s in manifest["streams"].values())
    # `contained=False` means the host had no way to substitute this digest for
    # the raw result, so the raw bytes reached the transcript anyway. The
    # artifact is still stored and addressable, but claiming the digest's size
    # as "emitted" would book a saving that never happened — so the event is
    # recorded at raw->raw, a real event with an honest zero gain.
    emitted = len(digest.encode("utf-8")) if contained else raw
    record_telemetry(store, op, raw, emitted)

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
    argv: list[str] | None = None,
    contained: bool = True,
) -> tuple[str, str]:
    """Digest an already-produced tool result (not a shell capture).

    ``contained`` is False when the calling host has no output-substitution
    field, so this digest is stored and addressable but never replaces the raw
    result in the transcript. It only affects telemetry honesty (see
    :func:`render_run_digest`), never the digest bytes.

    The universal emission gate (``ctx.hook._emission_gate``) calls this when a
    PostToolUse tool result exceeds the byte budget: it persists the raw bytes
    losslessly, synthesizes a ``ctx.invocation/v1`` manifest, and reuses
    :func:`render_run_digest` to produce a bounded digest carrying a working
    ``ctx get run:<short>#stdout`` retrieval ref.

    By default the manifest ``argv`` is ``[tool_name]`` only — never the
    tool's arguments — so the command-anchored profiles (git-diff, pytest,
    search, build) decline and the result is classified purely on its
    *shape*. That is the if-ladder cure: a new tool needs no new code
    because dispatch is by output shape. A caller that *knows* the true
    command (session replay reconstructing a steered `ctx run`) may pass
    ``argv`` explicitly to restore command-anchored detection; identity is
    then a pure function of (bytes, argv).

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
        "argv": list(argv) if argv else [tool_name],
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

    digest, final = render_run_digest(store, ws, manifest, focus=None,
                                      contained=contained)
    short = short_id(final.get("id", ""))

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
