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


def short_path(path: str) -> str:
    """A path narrowed to its last two components for a census line
    (``src/ctx/digest/lintprof.py`` → ``digest/lintprof.py``).

    One definition. This was two byte-identical nested functions in
    ``digest.lintprof`` and ``digest.searchprof``, both named ``_short`` —
    a name that in ``ctx.facts`` means "shorten a content HASH". Same name,
    unrelated jobs, and neither is substitutable for the other.
    """
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


class JsonPointerError(Exception):
    """A pointer that is malformed or does not resolve against the document."""


def json_pointer(doc, pointer: str):
    """Evaluate an RFC 6901 JSON pointer. One definition for the whole
    harness (``ctx q records --pointer`` and ``ctx get --json-pointer``).

    The edge case implementations get wrong, and this one previously got
    wrong in one of its two copies: ``""`` is the WHOLE DOCUMENT, and
    ``"/"`` is the member whose key is the empty string (RFC 6901 §5).
    They are different pointers. Consequently only the *first* slash is the
    root marker — ``"//"`` is two empty-string keys deep, so leading
    slashes must never be stripped in bulk.

    Escapes decode ``~1`` → ``/`` before ``~0`` → ``~`` (§4), so ``~01``
    means the literal ``~1`` and not ``/``.

    Array indices are the ABNF ``0 / [1-9][0-9]*`` — no leading zeros, no
    sign. ``-`` names the element after the last, which by §4 has no value:
    an evaluation error here, not an append.
    """
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise JsonPointerError(
            f"pointer must be empty or start with '/': {pointer!r}"
        )
    node = doc
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            if not (key.isdigit() and (key == "0" or not key.startswith("0"))):
                raise JsonPointerError(
                    f"not an array index: {token!r} in {pointer!r}"
                )
            idx = int(key)
            if idx >= len(node):
                raise JsonPointerError(f"index out of range: {token!r} in {pointer!r}")
            node = node[idx]
        elif isinstance(node, dict):
            if key not in node:
                raise JsonPointerError(f"no member {key!r} in {pointer!r}")
            node = node[key]
        else:
            raise JsonPointerError(
                f"cannot descend into a scalar at {token!r} in {pointer!r}"
            )
    return node


def estimate_tokens(n_bytes: int) -> int:
    """Cheap deterministic token estimate: ~4 bytes per token."""
    return max(1, n_bytes // 4) if n_bytes else 0


def fmt_tokens_coarse(tok: int) -> str:
    """FORECAST formatting (docs/PRICED-CONTEXT.md, P3) — the price tag on
    something not yet read: guard reasons, repo-map entries, stats estimates.
    A forecast only needs precision enough to cross a decision threshold, so
    buckets get coarser with size. Deterministic; never emits false precision
    like '8,432' *for a forecast*.

    This is one of three deliberate token renderings; they do different jobs
    and the docstring used to claim a rule that only covers this one:

    * ``fmt_tokens_coarse`` — a forecast (``~8k``). Bucketed, because the
      number is an estimate of something that has not happened.
    * ``fmt_int(estimate_tokens(n))`` — a MEASUREMENT of an artifact already
      captured (the digest header's ``est 4,072 tokens``). Exact by design:
      it is a pure function of an exact byte count, sits beside the
      ``15.9 KiB`` it must reconcile with, and belongs to a
      content-addressed digest that two runs of the same bytes must render
      identically. Bucketing it would break both properties.
    * ``fmt_tokens_compact`` — a WIDTH-CONSTRAINED glance (``2K``, ``1.5M``)
      for the status line, where the whole session must fit in one segment.
    """
    if tok < 1000:
        return f"~{max(1, round(tok / 50) * 50)}"
    if tok < 10_000:
        return f"~{max(1, round(tok / 1000))}k"
    if tok < 100_000:
        return f"~{5 * max(2, round(tok / 5000))}k"
    return f"~{25 * round(tok / 25_000)}k"


def fmt_tokens_compact(n: int) -> str:
    """Width-constrained token magnitude for a status line: ``2K``, ``1.5M``.

    Lifted out of ctx.statusline, where it was a private third rendering of
    the same quantity. It stays a distinct formatter rather than folding into
    ``fmt_tokens_coarse`` because the jobs differ at the top end: a whole
    session is millions of tokens, and coarse's largest bucket would render
    that as ``~1500k`` — wider and harder to read in the one segment a status
    line gets. Named and shared here so there are two documented renderings,
    not three ad-hoc ones."""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


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


def bounded(
    text: str,
    budget_tokens: int,
    continuation: str | None = None,
    *,
    truncation_continuation: str | None = None,
) -> str:
    """Enforce an output token budget. Oversized text is cut at a line
    boundary with explicit truncation metadata (never silent flooding).

    ``continuation`` is the caller's own "here is the rest" address and is
    appended whether or not anything was cut. ``truncation_continuation`` is
    the fallback handle appended ONLY when this function actually cut
    something: the clamp cuts from the bottom, and the ``next:`` affordance
    block is last in every digest profile, so the one moment a reader most
    needs a retrieval address is the moment the clamp deletes it. Callers
    that can name a handle pass it here; nothing is added to an untruncated
    digest, so an emission that fits stays byte-identical."""
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
    tail = continuation or truncation_continuation
    if tail:
        note += f"\nnext: {tail}"
    return cut + note
