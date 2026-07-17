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
from ctx.digest.pytestprof import PytestProfile
from ctx.digest.text import TextProfile
from ctx.execution import focus_hash, update_manifest_digest
from ctx.store import Store
from ctx.textutil import sanitize_for_model
from ctx.workspace import Workspace

# Fixed probe order — first match wins; text/v1 always matches last.
_PROFILES: tuple[Profile, ...] = (
    PytestProfile(),
    JsonLinesProfile(),
    JsonProfile(),
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
) -> tuple[str, dict[str, Any]]:
    """Produce the bounded deterministic digest for a captured invocation and
    republish the manifest with its final digest identity.

    Returns (digest_text, final_manifest).
    """
    ctx = DigestContext.load(store, ws, manifest, focus=focus)
    profile, reason = detect_profile(ctx)

    body = profile.render(ctx)
    body, redactions = sanitize_for_model(body, ws.config.redaction.patterns)
    if redactions:
        body += "\nredaction: applied [" + ", ".join(redactions) + "]"

    digest_meta = {
        "profile": profile.version,
        "policy": POLICY_VERSION,
        "focusHash": focus_hash(focus),
        "bytesHash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    final_id, final_manifest = update_manifest_digest(store, manifest, digest_meta)
    short = final_id[:12]

    header = f"[ctx run:{short} profile={profile.version}]"
    digest = header + "\n" + body.replace("run:PENDING", f"run:{short}")
    return digest, final_manifest
