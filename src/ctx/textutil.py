"""Deterministic text handling: token estimation, ANSI/control stripping,
secret redaction, and bounded emission (SPEC §8, §12.4, §16)."""

from __future__ import annotations

from ctx import bounds as _bounds

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


#: How wide ONE line of quoted foreign text may be inside a bounded row.
#:
#: Every digest profile, census renderer and match-listing verb quotes lines
#: it did not write — a captured stdout line, a matched source line, a
#: linter's message, a mined log template, a test node id — one per output
#: row. This is the width at which such a line is cut so the row stays a row.
#: It was the bare literal ``160`` at 24 sites plus four private
#: ``_LINE_CAP = 160`` constants (``ctx.query``, ``ctx.codeverbs``,
#: ``ctx.astgrep``, ``ctx.semgrep_engine``).
#:
#: It is NOT the only clip width in the harness, and the others are not the
#: same decision — do not fold them in here:
#:
#: * ``_retrieval.search._LINE_CHARS`` (200) — a search HIT line. A search
#:   result is the thing the user asked to see, so it gets more room than a
#:   line a digest volunteered; and the search renderer extracts it bounded
#:   rather than slicing a materialized line (giant-line cost).
#: * ``jobs._CLIP_COLS`` (200) — a live spool line, and it appends ``…``
#:   rather than cutting silently. Different mechanism, not just a width.
#: * ``facts._LINE_CAP`` (200) — a bound on a whole assembled census ROW,
#:   not on one quoted line inside it.
#: * The scattered ``[:120]`` / ``[:180]`` clips — a one-line summary field
#:   and a raw occurrence line respectively, each local to one renderer.
EVIDENCE_LINE_CHARS = 160

#: Width of the house short id. Twelve hex characters of a sha256 is the
#: display and addressing form for every content-addressed handle the model
#: sees (``run:``, ``blob:``, ``snapshot:``, ``checkpoint:``, ``plan:``), and
#: the width ``ctx.store``'s prefix resolver is tuned against.
SHORT_ID_CHARS = 12


def short_id(h: object) -> str:
    """The house short id for a content HASH: drop a ``sha256:`` prefix, keep
    the first :data:`SHORT_ID_CHARS` hex characters.

    One definition for an idiom that was retyped at ~20 call sites, in two
    spellings (``str(x).removeprefix("sha256:")[:12]`` where the value came
    from a manifest, bare ``x[:12]`` where it came from the store already
    stripped). Tolerates either form and already-short input; empty in,
    empty out.

    NOT the same job as :func:`short_path`, which shortens a filesystem path
    — the two shared the name ``_short`` until R13 split them.

    Deliberately NOT used for two look-alikes at the same width:

    * **Minting** an id (``hashlib.sha256(...).hexdigest()[:12]`` in
      ``ctx.reflex``'s intervention id, ``ctx.resolver``'s plan id,
      ``ctx.policy``'s epoch id). Those widths are part of a stored
      identity's collision budget; the display width is a readability
      choice. Someone widening one must not silently widen the other.
    * **Git** object names (``ws.git.head[:12]``). A different namespace with
      its own conventions (git's own abbreviation length is adaptive).
    """
    return str(h or "").removeprefix("sha256:")[:SHORT_ID_CHARS]


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


#: How bytes that are not valid UTF-8 survive a str-typed pipeline.
#:
#: `ctx get --bytes A:B` is documented as the exact-bytes escape hatch -- the
#: thing `ctx get` itself tells you to use for binary content -- and it
#: decoded with errors="replace", turning every invalid byte into a 3-byte
#: U+FFFD. The result was neither byte-exact nor even the same LENGTH as what
#: was captured: silent, irreversible loss through the tool's own exactness
#: interface, while the blob on disk stayed perfect.
#:
#: surrogateescape is the stdlib's answer: undecodable bytes become lone
#: surrogates that encode back to exactly the original bytes. The pipeline
#: stays str-typed and every string operation on the way out still works;
#: only the two ends -- the decode and the final write -- have to agree, so
#: they both go through here.
BYTE_EXACT_ERRORS = "surrogateescape"


def decode_exact(data: bytes) -> str:
    """Decode bytes losslessly into a str that re-encodes to the same bytes."""
    return data.decode("utf-8", BYTE_EXACT_ERRORS)


def encode_exact(text: str) -> bytes:
    """The inverse of decode_exact. Total: a str carrying no surrogates
    encodes exactly as plain UTF-8 would."""
    return text.encode("utf-8", BYTE_EXACT_ERRORS)


def write_exact(text: str, stream=None, *, newline: bool = True) -> None:
    """Write a possibly-byte-exact result to stdout without corrupting it.

    print() encodes through the stream's own error handler, which is strict
    by default -- a surrogate would raise there and turn a correct answer
    into a crash. Writing the bytes ourselves is the only way to keep the
    exactness the decode side just preserved.
    """
    import sys

    stream = stream if stream is not None else sys.stdout
    eol = "\n" if newline else ""
    buf = getattr(stream, "buffer", None)
    if buf is None:  # captured/text-only stream (pytest capsys): best effort
        stream.write(text + eol)
        return
    stream.flush()
    buf.write(encode_exact(text) + eol.encode())
    buf.flush()


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
        if unit == "B":
            if value < 1024:
                return f"{n} B"
        # Threshold-check the ROUNDED value, not the raw one: 1023.97 KiB is
        # under the limit and renders as "1024.0 KiB" once rounded to one
        # decimal -- a unit that displays its own overflow. Deciding on the
        # number the reader will actually see is the only way the two agree.
        elif round(value, 1) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
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
    # ctx.bounds, not `budget_tokens * 4`: a negative budget made this
    # `raw[:-4]` -- almost the whole input -- from the one function documented
    # as the hard backstop. The failure direction must be toward less output.
    budget_bytes = _bounds.budget_bytes(budget_tokens)
    # encode_exact, not a bare encode: this is the ONE backstop every emission
    # passes through, so a byte-exact --bytes result reaches it carrying lone
    # surrogates, and strict UTF-8 raised here -- the exactness fix turned a
    # correct answer into a crash at the measuring step. The count is
    # identical for text that has no surrogates.
    raw = encode_exact(text)
    if len(raw) <= budget_bytes:
        if continuation:
            return text + f"\nnext: {continuation}"
        return text
    # "ignore" would drop a partial character at the cut; decode_exact keeps
    # whatever is there, and the cut is declared either way.
    cut = decode_exact(raw[:budget_bytes])
    nl = cut.rfind("\n")
    # >= 0, not > 0: index 0 IS a line boundary. When the only newline inside
    # the budget was the first character, the trim was skipped entirely and
    # this "hard backstop" kept a mid-line fragment -- the one thing it exists
    # to prevent -- because a falsy index read as "no newline found".
    if nl >= 0:
        cut = cut[:nl]
    total_est = estimate_tokens(len(raw))
    note = f"\n[ctx:truncated shown≈{budget_tokens} of ≈{total_est} est tokens]"
    tail = continuation or truncation_continuation
    if tail:
        note += f"\nnext: {tail}"
    return cut + note
