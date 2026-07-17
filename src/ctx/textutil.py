"""Deterministic text handling: token estimation, ANSI/control stripping,
secret redaction, and bounded emission (SPEC §8, §12.4, §16)."""

from __future__ import annotations

import hashlib
import re

# CSI, OSC, and other ESC-introduced sequences, then bare control chars
# (tool output is untrusted; terminal control sequences never reach the model).
_ANSI_RE = re.compile(
    r"""
    \x1b\[[0-9;?]*[ -/]*[@-~]      # CSI sequences
  | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?  # OSC sequences
  | \x1b[@-_]                      # other escape sequences
    """,
    re.VERBOSE,
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Deterministic redaction patterns (SPEC §12.4). Name -> compiled regex.
# Curated from the rulesets battle-tested by gitleaks/detect-secrets; kept
# vendored (no dependency) so redaction stays deterministic and offline.
REDACTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    "aws-secret-key": re.compile(
        r"(?i)\baws_?secret_?access_?key\b\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{30,}"
    ),
    "private-key": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    "github-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{60,})\b"
    ),
    "gitlab-token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "slack-token": re.compile(
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|https://hooks\.slack\.com/services/T[A-Za-z0-9_/]{20,}"
    ),
    "stripe-key": re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}\b"),
    "twilio-key": re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
    "sendgrid-key": re.compile(r"\bSG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    "npm-token": re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    "pypi-token": re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}\b"),
    "huggingface-token": re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "jwt": re.compile(
        r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    ),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "generic-api-token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
}


try:  # optional fast path (Rust); stdlib retry keeps acceptance identical
    import orjson as _orjson
except ImportError:  # pragma: no cover - environment-dependent
    _orjson = None


def loads_fast(text: str | bytes):
    """json.loads with an opportunistic orjson fast path. orjson is ~6-10x
    faster on large documents but stricter (rejects NaN/Infinity); any
    orjson failure retries with stdlib json so semantics never narrow."""
    import json as _json

    if _orjson is not None:
        try:
            return _orjson.loads(text)
        except Exception:
            pass
    return _json.loads(text)


def estimate_tokens(n_bytes: int) -> int:
    """Cheap deterministic token estimate: ~4 bytes per token."""
    return max(1, n_bytes // 4) if n_bytes else 0


def strip_control(text: str) -> str:
    """Remove ANSI escape sequences and non-printable control characters,
    preserving newlines and tabs."""
    text = _ANSI_RE.sub("", text)
    return _CTRL_RE.sub("", text)


def redact(text: str, pattern_names: tuple[str, ...]) -> tuple[str, list[str]]:
    """Replace secrets with a deterministic marker carrying a short hash of
    the secret (declares redaction without revealing it). Returns
    (redacted_text, sorted list of pattern names that fired)."""
    fired: set[str] = set()

    for name in pattern_names:
        rx = REDACTION_PATTERNS.get(name)
        if rx is None:
            continue

        def _sub(m: re.Match[str], _name: str = name) -> str:
            fired.add(_name)
            tag = hashlib.sha256(m.group(0).encode("utf-8", "replace")).hexdigest()[:8]
            return f"[ctx:redacted:{_name}:{tag}]"

        text = rx.sub(_sub, text)
    return text, sorted(fired)


def sanitize_for_model(text: str, pattern_names: tuple[str, ...]) -> tuple[str, list[str]]:
    """Full model-visible pipeline: control stripping then redaction."""
    return redact(strip_control(text), pattern_names)


def fmt_int(n: int) -> str:
    """Locale-independent thousands formatting (1,204)."""
    return f"{n:,}"


def fmt_bytes(n: int) -> str:
    """Deterministic binary-size formatting."""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{n} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def decode_stream(data: bytes) -> tuple[str, str | None, str]:
    """Decode captured bytes for model-visible views without ever emitting
    arbitrary binary bytes (SPEC §6.2). Returns (text, encoding, media_type).
    Binary content yields empty text and media type application/octet-stream."""
    if b"\x00" in data[:8192]:
        return "", None, "application/octet-stream"
    try:
        return data.decode("utf-8"), "utf-8", "text/plain"
    except UnicodeDecodeError:
        # Deterministic lossy view; raw blob remains byte-exact in the store.
        return data.decode("utf-8", "replace"), "utf-8", "text/plain; lossy"


def bounded(text: str, budget_tokens: int, continuation: str | None = None) -> str:
    """Enforce an output token budget. Oversized text is cut at a line
    boundary with explicit truncation metadata (never silent flooding)."""
    budget_bytes = budget_tokens * 4
    raw = text.encode("utf-8")
    if len(raw) <= budget_bytes:
        if continuation:
            return text + f"\nnext: {continuation}"
        return text
    cut = raw[:budget_bytes].decode("utf-8", "ignore")
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    total_est = estimate_tokens(len(raw))
    note = f"\n[ctx:truncated shown≈{budget_tokens} of ≈{total_est} est tokens]"
    if continuation:
        note += f"\nnext: {continuation}"
    return cut + note
