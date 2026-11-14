"""Deterministic text handling: token estimation, ANSI/control stripping,
secret redaction, and bounded emission (SPEC §8, §12.4, §16).

Redaction markers -- the one place this is stated. A redacted secret is shown
as ``[ctx:redacted:<pattern>:<hash of the whole match>]`` (:func:`redact`,
:func:`secret_marker`) wherever the surrounding contract lets the text change
width: digests, chunk-map labels and rows, log templates, search hits'
wholly-contained secrets. Where the contract fixes the width -- a clip that a
budget or window fit has already measured (:func:`clip_redacting`,
:func:`tail_redacting`) and a range answer whose header declares its bytes
and whose continuations must tile the artifact (``ctx get --bytes/--lines/
--span``, :func:`edge_masked`) -- only the visible part of a secret that
CROSSES the cut is blanked, with one ``*`` per byte. A ``redaction: applied
[...]`` note names only patterns whose marker reached the text shown.

Redaction limits -- as measured, and the one place they are stated:

* An unbounded token (JWT, ``sk-``, ``ghp_`` ... :data:`LONG_TOKEN_PREFIXES`)
  is fully masked in digests, chunk rows and range answers up to 60 KiB
  long. Its run is followed at most :data:`TOKEN_RUN_SCAN_CHARS` (64 KiB)
  from a cut, so a longer run is not judged: a byte-range piece span inside
  a token over 64 KiB shows raw token characters.
* A window inside a private-key block is masked when the BEGIN line lies at
  most :data:`PEM_LOOKBEHIND_CHARS` (16 KiB) behind it, and not beyond.
* Fixed-shape secrets are judged on :data:`REDACTION_CONTEXT_CHARS` (1,024)
  characters around a cut: a sendgrid key's second half or an
  ``aws_secret_access_key=`` value further than that from the shown text is
  not recognized.
* A token directly adjacent to a non-ASCII letter is not matched: the shared
  patterns are word-boundary anchored and Python's ``\\b`` treats that letter
  as a word character.

What is redacted, and the stated non-goal:

* Workspace files, stored artifacts, and text STORED by one call and shown
  by a later one (debt notes, collab goals/notes/evidence, task-ledger and
  relay messages, spawned-agent output) are model-visible content like any
  other: the verbs that print them pass it through the pipeline once
  (:func:`model_visible`, :func:`redact_strings`).
* A multi-line secret (a private-key block) is masked as a block before any
  line-by-line view or transform takes the text apart: digests, diffs and
  templates replace each body line with :data:`PRIVATE_KEY_LINE`, never a
  digit-masked copy of it (:func:`private_key_line_ranges`).
* NON-GOAL: an argument echoed back within the same call to the caller who
  supplied it is not redacted -- the caller already holds it, and a
  suggested command must be the exact command to be of any use (hook
  ask/deny reasons, ``ask``'s subject, ``plan``'s objective,
  ``checkpoint``'s bad evidence). Where such an echo is clipped, the clip
  helper only keeps a cut from leaving a misleading fragment; it promises
  nothing more.
"""

from __future__ import annotations

from ctx import bounds as _bounds

import hashlib
import re

# CSI, OSC, and other ESC-introduced sequences, then bare control chars
# (tool output is untrusted; terminal control sequences never reach the model).
_ANSI_RE = re.compile(
    r"""
    \x1b\[[0-9;?]*[ -/]*[@-~]      # CSI sequences
  | \x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)?  # OSC sequences; stop at \n so an unterminated OSC can't eat the rest
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
    # A password inside a URL (`postgres://admin:pw@host/db`): the userinfo
    # part, user and password, up to the `@`.
    "url-credentials": re.compile(
        r"\b[A-Za-z][A-Za-z0-9+.-]{1,20}://[^\s:/@'\"<>]{1,128}:[^\s@/'\"<>]{1,256}@"
    ),
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
    # Redact BEFORE dropping components: a secret with "/" in it (an AWS
    # secret key, a slack hook URL) would otherwise be cut at a separator
    # and its tail shown without the part its pattern needs.
    path = redact_before_transform(path)
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
            if not (key.isdecimal() and (key == "0" or not key.startswith("0"))):  # fix: isdigit() accepts non-ASCII digits (e.g. superscripts) that int() rejects
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


def index_lines(text: str) -> list[str]:
    """Split on ``\n`` only, the way the store's line index counts lines.

    ``str.splitlines()`` also breaks on ``\r``, ``\v``, ``\f``, ``\x1c``,
    ``\x85``, U+2028 and U+2029, so a line index built on ``\n`` and a body
    split with ``splitlines()`` disagreed on any content containing one of
    them: ``ctx get`` printed four lines under a header claiming three, with
    every number after the break off by one. Search already split this way
    (``_retrieval/search.py``); retrieval and spans now do too. A trailing
    ``\r`` is dropped from each line for display, since CRLF content is one
    line per ``\n`` either way.
    """
    if not text:
        return []
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


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


# ------------------------------------------------- payload strings, one line
# A digest is read by a model that treats a line beginning ``next:`` as a
# suggested command and ``[ctx …]`` as a digest header. A string taken from
# the payload must therefore never break a digest line: every character that
# a reader may take as a line break is rendered as a visible escape instead.
_LINE_BREAK_ESCAPES = {
    "\n": "\\n", "\r": "\\r", "\x0b": "\\v", "\x0c": "\\f",
    "\x1c": "\\x1c", "\x1d": "\\x1d", "\x1e": "\\x1e",
    "\x85": "\\x85", "\u2028": "\\u2028", "\u2029": "\\u2029",
}
_LINE_BREAK_RE = re.compile("[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
# The subset that survives strip_control (which already removes \v, \f and
# \x1c-\x1e) and that no profile's own structure ever emits -- every line
# break a profile writes is a plain \n -- so any occurrence in a rendered
# body came from the payload.
_BODY_BREAK_RE = re.compile("[\r\x85\u2028\u2029]")


def one_line(text: str) -> str:
    """``text`` as one line: every line-break character, ``\n`` included,
    becomes a visible escape (``\\n``, ``\\u2028``…). Identity for any string
    without one, so ordinary content renders byte-for-byte as before. Use it
    on payload strings that did not come from a line split -- JSON keys and
    values, decoded header fields."""
    if _LINE_BREAK_RE.search(text) is None:
        return text
    return _LINE_BREAK_RE.sub(lambda m: _LINE_BREAK_ESCAPES[m.group()], text)


def neutralize_line_breaks(body: str) -> str:
    """Escape the line-break characters other than ``\n`` in a rendered
    digest body. Lines split on ``\n`` can still carry ``\r``, U+0085,
    U+2028 or U+2029 -- which some readers break on -- and no profile writes
    any of them itself, so escaping them all touches payload text only."""
    if _BODY_BREAK_RE.search(body) is None:
        return body
    return _BODY_BREAK_RE.sub(lambda m: _LINE_BREAK_ESCAPES[m.group()], body)


def strip_control(text: str) -> str:
    """Remove ANSI escape sequences and non-printable control characters,
    preserving newlines and tabs."""
    text = _ANSI_RE.sub("", text)
    return _CTRL_RE.sub("", text)


def _redaction_of(policy) -> tuple[bool, tuple[str, ...]]:
    """(enabled, patterns) from a ``Redaction`` config section.

    ``enabled`` is documented in CONFIGURATION.md as the switch that turns
    secret redaction off. It was parsed into the Config object and then read
    by NOBODY: every caller reached past it for ``.patterns``, so
    ``enabled = false`` was a setting the docs promised and the code ignored.

    Taking the SECTION rather than its patterns is what makes that
    impossible to repeat -- there is no longer a call shape that can drop the
    flag on the way in. tests/test_redaction_switch.py pins that no call site
    reaches for ``.redaction.patterns`` again.
    """
    return bool(getattr(policy, "enabled", True)), tuple(getattr(policy, "patterns", ()))


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


# ------------------------------------------- redaction around a cut or slice
#: How far around a cut (or a displayed slice) the redaction patterns look.
#: A secret is recognized at a cut when the whole of it lies within this many
#: characters of the visible part; patterns are only ever run on windows of
#: this bounded size, never on a whole payload, because several of them are
#: quadratic on adversarial runs (``eyJ`` + 20 class characters + ``-``,
#: repeated: every ``-`` is a word boundary that starts a new attempt which
#: scans to the end of the run). Bounded windows keep the cost per call
#: constant and the cost per digest linear in what it displays.
REDACTION_CONTEXT_CHARS = 1024
#: How far back a private-key BEGIN line is searched (plain ``str.find``,
#: linear) when a displayed slice may sit inside a PEM block.
PEM_LOOKBEHIND_CHARS = 16384

_PEM_BEGIN = "-----BEGIN "
_PEM_END = "-----END "
_PEM_TAIL = "PRIVATE KEY-----"
_PEM_LABEL_MAX = 64
_PEM_LABEL_OK = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ ")

try:
    from contextvars import ContextVar as _ContextVar
except ImportError:  # pragma: no cover - stdlib since 3.7
    _ContextVar = None  # type: ignore[assignment]

#: The redaction policy in force while a digest is rendered. Set by
#: ``ctx.digest.render_run_digest`` (and the fact-tier extraction) for the
#: duration of a profile's render, so the clip helpers below honour the
#: workspace's ``[redaction]`` section without threading it through every
#: profile. ``None`` (outside a render) means the default: every pattern.
_ACTIVE_POLICY = _ContextVar("ctx_redaction_policy", default=None) if _ContextVar else None


class redaction_scope:
    """``with redaction_scope(ws.config.redaction): ...`` -- the policy the
    clip helpers use inside the block."""

    def __init__(self, policy) -> None:
        self._policy = policy
        self._token = None

    def __enter__(self):
        if _ACTIVE_POLICY is not None:
            self._token = _ACTIVE_POLICY.set(self._policy)
        return self

    def __exit__(self, *exc) -> None:
        if _ACTIVE_POLICY is not None and self._token is not None:
            _ACTIVE_POLICY.reset(self._token)


def active_patterns(policy=None) -> list[tuple[str, "re.Pattern[str]"]]:
    """(name, regex) of the patterns a policy enables; [] when redaction is
    off. ``policy=None`` reads the active scope, else the default (all)."""
    if policy is None and _ACTIVE_POLICY is not None:
        policy = _ACTIVE_POLICY.get()
    if policy is None:
        return list(REDACTION_PATTERNS.items())
    enabled, names = _redaction_of(policy)
    if not enabled:
        return []
    return [(n, REDACTION_PATTERNS[n]) for n in names if n in REDACTION_PATTERNS]


def pem_spans(text: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    """Private-key blocks in ``text[start:end]`` found with ``str.find`` only
    (linear): what ``REDACTION_PATTERNS["private-key"]`` matches -- a BEGIN
    line through the next matching END line, or to the end when there is
    none -- without handing a regex an unbounded run."""
    end = len(text) if end is None else end
    out: list[tuple[int, int]] = []
    pos = start
    while True:
        i = text.find(_PEM_BEGIN, pos, end)
        if i < 0:
            return out
        j = text.find(_PEM_TAIL, i + len(_PEM_BEGIN), min(end, i + len(_PEM_BEGIN) + _PEM_LABEL_MAX + len(_PEM_TAIL)))
        if j < 0 or not set(text[i + len(_PEM_BEGIN):j]) <= _PEM_LABEL_OK:
            pos = i + len(_PEM_BEGIN)
            continue
        k = j + len(_PEM_TAIL)
        stop = end
        while True:
            e = text.find(_PEM_END, k, end)
            if e < 0:
                break
            t = text.find(_PEM_TAIL, e + len(_PEM_END), min(end, e + len(_PEM_END) + _PEM_LABEL_MAX + len(_PEM_TAIL)))
            if t >= 0 and set(text[e + len(_PEM_END):t]) <= _PEM_LABEL_OK:
                stop = t + len(_PEM_TAIL)
                break
            k = e + len(_PEM_END)
        out.append((i, stop))
        pos = stop


#: Patterns whose tokens have no length bound, by the literal they start
#: with. A token of one of these longer than the regex context (a 5 KB JWT,
#: a 9 KB ``sk-`` run) is recognized by a linear scan of its whole run of
#: token characters instead: a run that starts with one of these literals
#: and is too long for the bounded regex window is treated as the secret.
LONG_TOKEN_PREFIXES: dict[str, tuple[str, ...]] = {
    "github-token": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    "gitlab-token": ("glpat-",),
    "slack-token": ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"),
    "stripe-key": ("sk_live_", "sk_test_", "rk_live_", "rk_test_"),
    "sendgrid-key": ("SG.",),
    "pypi-token": ("pypi-AgEIcHlwaS5vcmc",),
    "huggingface-token": ("hf_",),
    "anthropic-key": ("sk-ant-",),
    "jwt": ("eyJ",),
    "generic-api-token": ("sk-",),
}
#: How far a run of token characters is followed from a slice's edge to find
#: its start and end. A run longer than this is not judged (declared limit).
TOKEN_RUN_SCAN_CHARS = 65536
_TOKEN_RUN_BREAK = re.compile(r"[^A-Za-z0-9_.+/=-]")


class SecretHit:
    """One redaction match seen around a slice: ``start``/``end`` clipped to
    the slice, ``whole_start``/``whole_end`` the match itself. A plain slotted
    class, not a dataclass: hook-path modules import this module lazily and
    ``dataclasses`` is on the hot path's forbidden list (it drags in
    ``inspect``)."""

    __slots__ = ("start", "end", "name", "whole_start", "whole_end")

    def __init__(self, start: int, end: int, name: str, whole_start: int, whole_end: int) -> None:
        self.start, self.end, self.name = start, end, name
        self.whole_start, self.whole_end = whole_start, whole_end

    def __repr__(self) -> str:
        return (f"SecretHit({self.start}, {self.end}, {self.name!r}, "
                f"{self.whole_start}, {self.whole_end})")

    def crosses(self, start: int, end: int) -> bool:
        return self.whole_start < start or self.whole_end > end


_MARKER_NAME_RE = re.compile(r"\[ctx:redacted:([a-z0-9-]*)")


def shown_marker_names(text: str, candidates) -> list[str]:
    """Pattern names whose marker is VISIBLE in ``text``: a whole name, or
    the start of one where a clip cut the marker (a cut name names every
    candidate that starts so). A ``redaction: applied`` note built from this
    claims only what the reader can see was redacted."""
    out: set[str] = set()
    cands = set(candidates)
    for m in _MARKER_NAME_RE.finditer(text):
        g = m.group(1)
        if g in cands:
            out.add(g)
        elif g:
            out.update(c for c in cands if c.startswith(g))
    return sorted(out)


def secret_marker(name: str, matched: str) -> str:
    """The marker :func:`redact` writes for ``matched``."""
    tag = hashlib.sha256(matched.encode("utf-8", "replace")).hexdigest()[:8]
    return f"[ctx:redacted:{name}:{tag}]"


def _token_run(text: str, pos: int, scan: int) -> tuple[int, int] | None:
    """The run of token characters containing ``pos`` -- found with one
    reversed and one forward search, both linear -- or None when ``pos`` is
    not in a run or the run is longer than ``scan`` in either direction."""
    if not (0 <= pos < len(text)) or _TOKEN_RUN_BREAK.match(text, pos):
        return None
    lo = max(0, pos - scan)
    back = _TOKEN_RUN_BREAK.search(text[lo:pos][::-1])
    if back is None and lo > 0:
        return None
    a = pos - back.start() if back else lo
    fwd = _TOKEN_RUN_BREAK.search(text, pos, min(len(text), pos + scan))
    if fwd is None and pos + scan < len(text):
        return None
    b = fwd.start() if fwd else min(len(text), pos + scan)
    return a, b


def private_key_line_ranges(text: str, prefix: str = "", policy=None) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of ``text`` that lie inside a private-key
    block (plain search, linear). ``prefix`` is text immediately before
    ``text`` (bounded by the caller) so a block whose BEGIN line precedes the
    view is still found. [] when the policy excludes the pattern.

    A key's body lines match no pattern alone, so any view that takes the
    stream apart line by line -- templates, line listings, zoom -- must ask
    this BEFORE it transforms or shows a line."""
    if "private-key" not in {n for n, _ in active_patterns(policy)}:
        return []
    joined = prefix + text
    if "PRIVATE KEY-----" not in joined:
        return []
    base = len(prefix)
    out = []
    for a, b in pem_spans(joined):
        if b <= base:
            continue
        a = max(a, base) - base
        b = b - base
        out.append((text.count("\n", 0, a) + 1, text.count("\n", 0, max(a, b - 1)) + 1))
    return out


def in_line_ranges(n: int, ranges) -> bool:
    return any(a <= n <= b for a, b in ranges)


#: What a line inside a private-key block shows as in a line-by-line view.
PRIVATE_KEY_LINE = "[ctx:redacted:private-key]"


def secret_hits(
    text: str, start: int, end: int, *, policy=None, context: int = REDACTION_CONTEXT_CHARS,
    pem_lookbehind: int = PEM_LOOKBEHIND_CHARS, scan: int = TOKEN_RUN_SCAN_CHARS,
) -> list[SecretHit]:
    """Every redaction match that overlaps ``text[start:end]``, judged on
    the text AROUND the slice -- never on the whole text:

    * the patterns run on ``[start - context, end + context]`` only (cost
      bounded per call: several patterns are quadratic on adversarial runs);
    * private-key blocks are found with plain ``str.find`` from up to
      ``pem_lookbehind`` before the slice;
    * a run of token characters crossing an edge and too long for that
      window is followed (linear scans, up to ``scan`` either way) and
      treated as a secret when it starts with an unbounded pattern's literal
      (:data:`LONG_TOKEN_PREFIXES`).

    Sorted by start; not merged (each keeps its name and whole extent).
    """
    pats = active_patterns(policy)
    if not pats or start >= end:
        return []
    found: list[SecretHit] = []
    w0 = max(0, start - context)
    w1 = min(len(text), end + context)
    window = text[w0:w1]
    names = {n for n, _ in pats}
    for name, rx in pats:
        if name == "private-key":
            for a, b in pem_spans(text, max(0, start - pem_lookbehind), w1):
                if b > start and a < end:
                    found.append(SecretHit(max(a, start), min(b, end), name, a, b))
            continue
        for m in rx.finditer(window):
            a, b = m.start() + w0, m.end() + w0
            if a >= end:
                break
            if b > start and b > a:
                found.append(SecretHit(max(a, start), min(b, end), name, a, b))
    seen: set[tuple[int, int]] = set()
    for pos in (start, end - 1):
        run = _token_run(text, pos, scan)
        if run is None or run in seen or run[1] - run[0] <= context:
            continue
        seen.add(run)
        head = text[run[0]: run[0] + 32]
        for name, prefixes in LONG_TOKEN_PREFIXES.items():
            if name in names and head.startswith(prefixes):
                found.append(SecretHit(max(run[0], start), min(run[1], end), name, run[0], run[1]))
                break
    found.sort(key=lambda h: (h.start, -h.end))
    return found


def merge_hits(hits) -> list[tuple[int, int, "SecretHit"]]:
    """Disjoint (start, end, first hit) intervals over sorted hits."""
    merged: list[list] = []
    for h in hits:
        if merged and h.start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], h.end)
        else:
            merged.append([h.start, h.end, h])
    return [(a, b, h) for a, b, h in merged]


def secret_intervals(text: str, start: int, end: int, *, policy=None, **kw) -> list[tuple[int, int]]:
    """Merged (start, end) intervals of :func:`secret_hits`."""
    return [(a, b) for a, b, _ in merge_hits(secret_hits(text, start, end, policy=policy, **kw))]


def mask_hits(text: str, start: int, end: int, hits) -> str:
    """``text[start:end]`` with each secret replaced by its standard marker
    (:func:`secret_marker`, tagged from the WHOLE match) -- one marker per
    line segment, so a masked multi-line block keeps its line structure."""
    merged = merge_hits(hits)
    if not merged:
        return text[start:end]
    out: list[str] = []
    pos = start
    for a, b, h in merged:
        out.append(text[pos:a])
        marker = secret_marker(h.name, text[h.whole_start:h.whole_end])
        out.append("\n".join(marker if seg else "" for seg in text[a:b].split("\n")))
        pos = b
    out.append(text[pos:end])
    return "".join(out)


def blank_hits(text: str, start: int, end: int, hits) -> str:
    """``text[start:end]`` with each secret's characters blanked to ``*`` --
    one per UTF-8 byte, newlines kept -- so the answer has exactly the width
    (and byte count, line count) it declares. Used where a range answer's
    accounting must not move; see the module note on markers."""
    merged = merge_hits(hits)
    if not merged:
        return text[start:end]
    out: list[str] = []
    pos = start
    for a, b, _ in merged:
        out.append(text[pos:a])
        out.append("".join(
            ch if ch == "\n" else "*" * len(ch.encode("utf-8", "surrogateescape"))
            for ch in text[a:b]))
        pos = b
    out.append(text[pos:end])
    return "".join(out)


def edge_masked(pre: str, mid: str, post: str, policy=None) -> str:
    """``mid`` -- a range ctx is about to return -- with every secret that
    CROSSES its edges blanked at the same width, judged with ``pre`` and
    ``post`` (the text around it, already bounded by the caller). A secret
    wholly inside is left to the emission pass, which redacts it with its
    marker. Identity whenever nothing crosses an edge."""
    if not mid:
        return mid
    text = pre + mid + post
    s, e = len(pre), len(pre) + len(mid)
    hits = [h for h in secret_hits(text, s, e, policy=policy) if h.crosses(s, e)]
    return blank_hits(text, s, e, hits) if hits else mid


def mask_intervals(text: str, start: int, end: int, intervals, marker: str = "[ctx:redacted]") -> str:
    """``text[start:end]`` with each interval replaced by ``marker`` (kept
    for callers that hold bare intervals; prefer :func:`mask_hits`)."""
    if not intervals:
        return text[start:end]
    out: list[str] = []
    pos = start
    for a, b in intervals:
        out.append(text[pos:a])
        segs = text[a:b].split("\n")
        out.append("\n".join(marker if seg else "" for seg in segs))
        pos = b
    out.append(text[pos:end])
    return "".join(out)


def clip_redacting(text: str, limit: int, policy=None) -> str:
    """``text[:limit]``, except that when a secret STRADDLES the cut, its
    visible part is blanked to ``*`` (same width).

    Clipping a line to a display width and redacting the digest afterwards
    leaves a straddling credential as a prefix too short for its pattern to
    match (``ghp_`` plus 19 characters), so that prefix reached the model.
    Here the line is matched around the cut first, and the result keeps the
    width of ``text[:limit]`` exactly, so widths and budgets downstream hold.

    It is byte for byte ``text[:limit]`` whenever no match crosses the cut
    (ordinary content, and secrets wholly inside the window -- still
    redacted by the emission pass -- or wholly outside it), and always when
    the policy disables redaction; a pattern the policy excludes blanks
    nothing. The policy is ``policy``, else the active
    :class:`redaction_scope`, else every pattern.

    Limits (declared): patterns run on ``text[:limit + 1024]`` only
    (:data:`REDACTION_CONTEXT_CHARS`), so the cost is constant per call. A
    straddling token is caught when its pattern matches within that window
    (any length for the ``{20,}``-style patterns: the run is simply
    truncated), or -- for the unbounded tokens of
    :data:`LONG_TOKEN_PREFIXES` (JWT, ``sk-``, ``ghp_``, ...) -- when its
    whole run of token characters is at most 64 KiB
    (:data:`TOKEN_RUN_SCAN_CHARS`). Not caught: a fixed-shape secret whose
    required later part lies more than 1,024 characters past the cut (a
    sendgrid key's second half, an ``aws_secret_access_key=`` value) and a
    token run longer than 64 KiB; their first ``limit - start`` characters
    can show.
    """
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    pats = active_patterns(policy)
    if not pats:
        return text[:limit]
    window = text[: limit + REDACTION_CONTEXT_CHARS]
    start = limit
    for name, rx in pats:
        if name == "private-key":
            for a, b in pem_spans(window):
                if a < limit < b:
                    start = min(start, a)
            continue
        for m in rx.finditer(window):
            if m.start() >= limit:
                break
            if m.end() > limit:
                start = min(start, m.start())
                break
    # An unbounded token too long for the window (a 5 KB JWT): followed to
    # its start by a linear scan and judged by its literal prefix.
    run = _token_run(text, limit - 1, TOKEN_RUN_SCAN_CHARS)
    if run and run[1] > limit and run[1] - run[0] > REDACTION_CONTEXT_CHARS:
        active = {n for n, _ in pats}
        head = text[run[0]: run[0] + 32]
        if any(n in active and head.startswith(p) for n, p in LONG_TOKEN_PREFIXES.items()):
            start = min(start, run[0])
    # Same width: the visible part of the secret is blanked character for
    # character, so every downstream width, window fit and budget cut lands
    # exactly where it did before -- the prefix is the only thing that moves.
    return text[:start] + "*" * (limit - start)


def redact_before_transform(text: str, policy=None, cap: int = 4096) -> str:
    """``text`` with each secret in its first ``cap`` characters replaced by
    its standard marker -- for text that is about to be TRANSFORMED (split,
    shortened, case-folded, masked) before it is shown, which would leave a
    secret unmatchable by the emission pass. Identity -- the same object --
    when nothing matches; bounded cost (``cap`` plus the regex context)."""
    if not text:
        return text
    end = min(len(text), cap)
    hits = secret_hits(text, 0, end, policy=policy)
    return mask_hits(text, 0, end, hits) + text[end:] if hits else text


def redact_strings(value, policy=None):
    """Every string inside ``value`` (dicts, lists, tuples) through the
    model-visible pipeline once -- for structured output (JSON) whose
    strings quote stored or workspace text. ``policy=None`` reads the
    active scope, else every pattern."""
    if policy is None and _ACTIVE_POLICY is not None:
        policy = _ACTIVE_POLICY.get()
    if isinstance(value, str):
        if policy is None:
            return redact(strip_control(value), tuple(REDACTION_PATTERNS))[0]
        return sanitize_for_model(value, policy)[0]
    if isinstance(value, dict):
        return {k: redact_strings(v, policy) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_strings(v, policy) for v in value]
    return value


def model_visible(text: str, policy=None) -> str:
    """``text`` through the model-visible pipeline once, with the
    ``redaction: applied`` note when anything fired -- for verbs that print
    text stored by an earlier call (notes, goals, messages)."""
    if policy is None and _ACTIVE_POLICY is not None:
        policy = _ACTIVE_POLICY.get()
    if policy is None:
        out, fired = redact(strip_control(text), tuple(REDACTION_PATTERNS))
    else:
        out, fired = sanitize_for_model(text, policy)
    return out + ("\nredaction: applied [" + ", ".join(fired) + "]" if fired else "")


def tail_redacting(text: str, limit: int, policy=None) -> str:
    """``text[-limit:]`` (the last ``limit`` characters), except that a
    secret straddling the cut has its visible part blanked to ``*`` at the
    same width -- the mirror of :func:`clip_redacting` for tail views."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    pats = active_patterns(policy)
    if not pats:
        return text[cut:]
    hits = [h for h in secret_hits(text, cut, min(len(text), cut + REDACTION_CONTEXT_CHARS),
                                   policy=policy) if h.whole_start < cut]
    end = min(len(text), max([h.whole_end for h in hits] + [cut]))
    return "*" * (end - cut) + text[end:]


def elide_redacting(text: str, head: int, tail: int, sep: str, policy=None) -> str:
    """``text[:head] + sep + text[-tail:]`` with both cuts redaction-aware
    (:func:`clip_redacting`, :func:`tail_redacting`); ``text`` itself when it
    fits."""
    if len(text) <= head + tail:
        return text
    return clip_redacting(text, head, policy) + sep + tail_redacting(text, tail, policy)


def sanitize_for_model(text: str, policy) -> tuple[str, list[str]]:
    """Full model-visible pipeline: control stripping then redaction.

    ``policy`` is the ``[redaction]`` config SECTION (``ws.config.redaction``),
    not its patterns tuple -- see _redaction_of for why that distinction is
    the fix rather than a style choice.
    """
    enabled, patterns = _redaction_of(policy)
    text = strip_control(text)
    return (text, []) if not enabled else redact(text, patterns)


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
    # Trim back to a line boundary only when that costs a LINE.
    #
    # Two bug bashes shaped this. The first found `if nl > 0`, where a
    # boundary at index 0 read as "no newline found" and the trim was skipped
    # exactly when the only newline in budget was the first character. The
    # second found the real damage: on newline-sparse content -- one long
    # line, or the exact-bytes body `--bytes` exists to serve -- the last
    # newline inside the budget is the HEADER's own, so trimming to it
    # deleted every byte the caller asked for while still exiting 0 under a
    # header claiming the full range.
    #
    # The half-of-budget test is the operative rule and it subsumes the
    # index-0 case (0 is never at least half of a non-empty cut). Trimming to
    # a line boundary is a readability nicety; it may never be the reason a
    # bounded preview previews nothing.
    if nl >= 0 and nl * 2 >= len(cut):
        cut = cut[:nl]
    total_est = estimate_tokens(len(raw))
    # What was actually kept, not what was asked for. The note reported the
    # nominal budget, so after the line-boundary trim discarded content it
    # claimed to have shown up to twice what it did -- an accounting line
    # that is wrong about its own accounting.
    shown_est = estimate_tokens(len(encode_exact(cut)))
    note = f"\n[ctx:truncated shown≈{shown_est} of ≈{total_est} est tokens]"
    tail = continuation or truncation_continuation
    if tail:
        note += f"\nnext: {tail}"
    return cut + note
