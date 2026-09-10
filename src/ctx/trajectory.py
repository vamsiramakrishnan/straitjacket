"""What the agent actually looked at, reconstructed from a transcript.

This is the instrument for the question ContextBench asks and
``evals/contextbench.py`` could not answer: *during issue resolution, did the
agent reach the context a human said it needed?* That is a property of a
**trajectory**, not of a retrieval policy. A hand-written policy scored in one
pass measures the policy; the regions an agent opened over twenty turns measure
the agent and the tools it had.

## The one rule that makes the measurement worth anything

**One extractor, every arm.** The comparison that matters is an agent with ctx
against the same agent without it. If the ctx arm were measured through ctx's
own telemetry and the native arm by parsing a transcript, the instrument would
differ between arms and the result would be built in before the first run. So
nothing here imports ctx state, reads the store, or requires ctx to have been
installed. It reads a recorded transcript and nothing else.

That constraint drives the design: wherever a tool result *carries line
coordinates*, those coordinates are the evidence, because they are what the
model saw. Claude Code's ``Read`` renders ``   123→text``; ``ctx get`` renders
``L123: text``; ``grep -n`` renders ``path:123:text``. Three tools, three
dialects, one fact — the model saw line 123 of that file. Parsing the result
rather than the arguments also captures truncation for free: a ``Read`` that
asked for 2,000 lines and was cut off at 500 observed 500.

## Explored is not utilized

Two sets come back, because the paper's own finding is that they diverge.
**Explored** is every region the agent saw. **Utilized** is every region it
went on to edit. An agent that reads forty files and edits one explored forty
and utilized one, and the gap between those numbers is a real property of the
loop rather than a defect of either.

## Attribution honesty

Not every call yields coordinates. ``cat file`` in a Bash call shows content
with no line numbers at all, so the whole file is attributed and the region is
marked ``inferred`` rather than ``parsed``. Every report carries the split. A
trajectory scored mostly from inferred regions is a soft number and has to say
so, because whole-file attribution inflates recall and destroys precision at
the same time.

Calls are classified three ways, because collapsing them hides the only signal
that this module is missing a dialect. **Observing** calls yielded regions.
**Inert** calls were never going to show source: a push, a test run, an edit, a
directory listing. **Unattributed** calls looked like a read and parsed to
nothing — those, and only those, are instrument defects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Bytes per token, matching :func:`ctx.textutil.estimate_tokens`. Duplicated
#: as a constant rather than imported so this module stays importable with no
#: ctx workspace, store or config in play — the arm-agnostic rule above.
BYTES_PER_TOKEN = 4

#: A whole-file attribution larger than this is refused rather than recorded.
#: Claiming an agent "observed" 20,000 lines because it ran `cat` on a vendored
#: bundle would swamp every other region in the trajectory; the call is counted
#: as unattributed instead, which is the honest outcome.
MAX_INFERRED_LINES = 3000

# -- result dialects, one per tool family ---------------------------------
# Each captures (line number) from a rendered result line. The model saw that
# line of that file; the file comes from the call's input or a preceding
# header, depending on the dialect.

#: Claude Code `Read`: "   123→def foo():" (arrow, sometimes tab-separated).
_READ_LINE = re.compile(r"^\s*(\d+)[→\t]")
#: `ctx get` / `ctx def` body: "L123: def foo():"
_CTX_LINE = re.compile(r"^\s*L(\d+):")
#: `ctx def` header: "definition: repo:path/to.py L240:336@abcd1234 (function)"
_CTX_DEF = re.compile(r"definition:\s+repo:(\S+)\s+L(\d+):(\d+)")
#: grep -n / rg -n / Grep(output_mode=content): "path/to.py:123:text"
_GREP_LINE = re.compile(r"^([^\s:][^:]*):(\d+):")
#: `ctx search` file header ("src/ctx/hook.py:") then indented "  L2917: ..."
_CTX_SEARCH_FILE = re.compile(r"^(\S[^\s:]*\.[A-Za-z0-9_]+):$")
#: A bare "path:line" coordinate, e.g. in `ctx refs` output.
_REF_LINE = re.compile(r"^repo:(\S+?):L(\d+):")

#: Bash forms whose output carries no coordinates, so the file is attributed
#: whole. `sed -n 'A,Bp'` is the exception: its range is in the command.
_BASH_WHOLE = re.compile(r"\b(?:cat|bat)\s+(?:-\S+\s+)*([^\s|;&><]+)")
_BASH_SED_RANGE = re.compile(r"\bsed\s+-n\s+['\"]?(\d+),(\d+)p")
_BASH_SED_FILE = re.compile(r"\bsed\s+-n\s+['\"]?\d+,\d+p['\"]?\s+([^\s|;&><]+)")
_BASH_HEAD = re.compile(r"\bhead\s+-?n?\s*(\d+)\s+([^\s|;&><]+)")
#: `grep -n PAT FILE` on a single file renders "123:text" with no path, and its
#: -A/-B context lines render "124-text". The path is in the command. Without
#: this the native arm loses most of its observations, because single-file grep
#: is how an agent without ctx reads around a match — a bias in exactly the
#: comparison this module exists to keep honest.
_GREP_BARE = re.compile(r"^(\d+)[:-]")
_GREP_FILE = re.compile(
    r"\b(?:grep|rg)\b[^|;&]*?(?<!-)\s([\w.@~/-]*(?:/[\w.@-]+|[\w-]+\.[A-Za-z0-9]{1,6}))\s*$"
)
#: A redirect makes the operand a destination, not a source: `cat >> f <<EOF`
#: is a heredoc write that an earlier revision counted as reading f.
_REDIRECT = re.compile(r"[<>]")

#: Tools that only ever list names. A directory listing is not context: the
#: model learned that a path exists, not what is in it. Counting these as
#: retrieval would credit `ls -R` with finding the whole repository.
_NAME_ONLY = {"Glob", "LS", "NotebookRead"}

#: A ctx invocation, anchored at a command boundary. An earlier revision tested
#: `"ctx " in command`, which fired on every heredoc that happened to *contain*
#: the words "ctx search" while editing a document. Substring matching a shell
#: command is how an instrument starts attributing reads to calls that never
#: read anything.
_CTX_READ = re.compile(
    r"(?:^|[;&|]\s*|\$\(\s*)ctx\s+(?:--\S+\s+\S+\s+)*"
    r"(?:get|search|def|refs|callers|callees|impact|impls|q|ask|diag)\b"
)

#: Readers, and a path-shaped operand for them to read.
_READER = re.compile(r"^(cat|bat|head|tail|sed|grep|rg|ag|ack|less|more)\b(.*)$", re.S)
_PATH_OPERAND = re.compile(r"(?:^|\s)(?!-)[\w.@~/-]*(?:/[\w.@-]+|\.[A-Za-z0-9]{1,6})(?:\s|$)")


def _reads_source(command: str) -> bool:
    """Does this shell command read a file, as opposed to filtering a stream?

    Two distinctions the naive version got wrong, both measured on a real
    session. A **pipe stage** reads stdin, never a file: `python … | tail -30`
    is not a source read, and matching `tail` after a `|` classified 249 calls
    as failures of this module when they were nothing of the kind. And a reader
    with **no path operand** reads stdin too. So: consider only the segment
    before the first pipe, and require something path-shaped after the verb.

    This matters because `unattributed` is the extractor's own defect counter.
    Inflate it with commands that were never going to show source and it stops
    being able to tell anyone that a dialect is missing.
    """
    head = command.split("|", 1)[0]
    for segment in re.split(r"&&|;", head):
        m = _READER.match(segment.strip())
        if not m:
            continue
        rest = m.group(2)
        if _REDIRECT.search(rest):
            continue  # a write or a heredoc, not a read
        operand = _PATH_OPERAND.search(rest)
        if operand and not operand.group(0).strip().startswith(("/", "~")):
            return True
    return False


@dataclass(frozen=True)
class Region:
    """One contiguous span of a file the model is known to have seen."""

    path: str
    start: int
    end: int
    origin: str  # read | grep | ctx-get | ctx-def | ctx-search | ctx-refs | bash | edit
    basis: str  # parsed (coordinates in the result) | inferred (from arguments)

    @property
    def lines(self) -> int:
        return max(0, self.end - self.start + 1)


@dataclass
class Trajectory:
    """Everything one recorded session lets us say about what was looked at."""

    explored: list[Region] = field(default_factory=list)
    utilized: list[Region] = field(default_factory=list)
    visible_tokens: int = 0
    calls: int = 0
    observing_calls: int = 0      # yielded at least one region
    inert_calls: int = 0          # never going to show source (git, pytest, a push)
    unattributed_calls: int = 0   # looked like a read, parsed to nothing
    by_origin: dict[str, int] = field(default_factory=dict)
    by_basis: dict[str, int] = field(default_factory=dict)

    @property
    def parsed_share(self) -> float:
        """Fraction of observed lines that came from real coordinates.

        The confidence qualifier on every number derived from this trajectory.
        """
        total = sum(self.by_basis.values())
        return round(self.by_basis.get("parsed", 0) / total, 4) if total else 0.0


def _norm(path: str) -> str:
    p = str(path or "").strip().strip("'\"").replace("\\", "/")
    for prefix in ("./", "/"):
        while p.startswith(prefix):
            p = p[len(prefix) :]
    return p


def _relativize(path: str, root: str | None) -> str:
    """Absolute paths in a transcript become repo-relative, or are refused.

    Returns "" for anything outside the repository. Gold annotations are
    repo-relative, so a scratch file under /tmp can never match one — but it
    can still be attributed as thousands of "observed" lines and drown the
    trajectory. Measured on a real session: temp logs contributed the large
    majority of whole-file attribution before this refused them.
    """
    p = str(path or "").replace("\\", "/")
    if root:
        r = str(root).replace("\\", "/").rstrip("/")
        if p.startswith(r + "/"):
            return p[len(r) + 1 :]
    if p.startswith("/") or p.startswith("~"):
        return ""
    p = _norm(p)
    return "" if p.startswith("..") or not p else p


def _spans(numbers: Iterable[int], gap: int = 2) -> list[tuple[int, int]]:
    """Consecutive line numbers collapse into spans; a gap starts a new one.

    Grep results are a scatter of single lines, and recording them as hundreds
    of one-line regions makes every later set operation quadratic for no gain.
    """
    ns = sorted({n for n in numbers if n > 0})
    if not ns:
        return []
    out, start, prev = [], ns[0], ns[0]
    for n in ns[1:]:
        if n - prev <= gap:
            prev = n
            continue
        out.append((start, prev))
        start = prev = n
    out.append((start, prev))
    return out


def _file_of(call: dict[str, Any]) -> str:
    inp = call.get("input") or {}
    for key in ("file_path", "filePath", "path", "notebook_path"):
        if inp.get(key):
            return str(inp[key])
    return ""


def _from_read(call: dict[str, Any], root: str | None) -> list[Region]:
    path = _relativize(_file_of(call), root)
    if not path:
        return []
    result = call.get("result") or ""
    hits = [int(m.group(1)) for m in (_READ_LINE.match(l) for l in result.splitlines()) if m]
    if hits:
        return [Region(path, a, b, "read", "parsed") for a, b in _spans(hits)]

    # No coordinates in the render: fall back to the arguments, and if those
    # are absent the call read the file whole.
    inp = call.get("input") or {}
    offset = int(inp.get("offset") or 1)
    limit = inp.get("limit")
    if limit:
        return [Region(path, offset, offset + int(limit) - 1, "read", "inferred")]
    lines = result.count("\n") + 1 if result else 0
    if not lines or lines > MAX_INFERRED_LINES:
        return []
    return [Region(path, 1, lines, "read", "inferred")]


def _from_grep(call: dict[str, Any], root: str | None) -> list[Region]:
    """`Grep`, and any Bash grep/rg whose output carries `path:line:`."""
    per_file: dict[str, list[int]] = {}
    for line in (call.get("result") or "").splitlines():
        m = _GREP_LINE.match(line)
        if m:
            per_file.setdefault(_relativize(m.group(1), root), []).append(int(m.group(2)))
    return [
        Region(p, a, b, "grep", "parsed")
        for p, ns in per_file.items()
        for a, b in _spans(ns)
    ]


def _from_ctx(call: dict[str, Any], root: str | None) -> list[Region]:
    """`ctx get`, `ctx def`, `ctx search`, `ctx refs` — one dialect each.

    Parsed from the *rendered result*, exactly like the native tools above, so
    the ctx arm is measured by the same instrument rather than a friendlier one.
    """
    result = call.get("result") or ""
    regions: list[Region] = []

    # ctx def: the header states the authoritative span.
    for m in _CTX_DEF.finditer(result):
        regions.append(
            Region(_relativize(m.group(1), root), int(m.group(2)), int(m.group(3)), "ctx-def", "parsed")
        )

    # ctx refs: "repo:path:L123: text"
    ref_files: dict[str, list[int]] = {}
    for line in result.splitlines():
        m = _REF_LINE.match(line)
        if m:
            ref_files.setdefault(_relativize(m.group(1), root), []).append(int(m.group(2)))
    for p, ns in ref_files.items():
        regions.extend(Region(p, a, b, "ctx-refs", "parsed") for a, b in _spans(ns))

    # ctx search: a bare "path:" header, then indented "  L123: text".
    search_files: dict[str, list[int]] = {}
    current = ""
    for line in result.splitlines():
        head = _CTX_SEARCH_FILE.match(line)
        if head:
            current = _relativize(head.group(1), root)
            continue
        if line.startswith(("[ctx", "coverage", "result:", "next:", "snapshots", "patterns")):
            current = ""
            continue
        m = _CTX_LINE.match(line)
        if m and current and line.startswith(" "):
            search_files.setdefault(current, []).append(int(m.group(1)))
    for p, ns in search_files.items():
        regions.extend(Region(p, a, b, "ctx-search", "parsed") for a, b in _spans(ns))

    if regions:
        return regions

    # ctx get: an unindented "L123:" body against the addressed file. The ref
    # is in the command, not the render, so the path comes from the argv.
    body = [int(m.group(1)) for m in (_CTX_LINE.match(l) for l in result.splitlines()) if m]
    if not body:
        return []
    command = str((call.get("input") or {}).get("command") or "")
    m = re.search(r"repo:([^\s]+?)(?:[:\s#]|$)", command)
    if not m:
        return []
    path = _relativize(m.group(1), root)
    return [Region(path, a, b, "ctx-get", "parsed") for a, b in _spans(body)]


def _from_bash(call: dict[str, Any], root: str | None) -> list[Region]:
    command = str((call.get("input") or {}).get("command") or "")
    if _CTX_READ.search(command):
        found = _from_ctx(call, root)
        if found:
            return found
    grepped = _from_grep(call, root)
    if grepped:
        return grepped
    single = _GREP_FILE.search(command.split("|", 1)[0])
    if single:
        path = _relativize(single.group(1), root)
        ns = [int(m.group(1)) for m in
              (_GREP_BARE.match(l) for l in (call.get("result") or "").splitlines()) if m]
        if path and ns:
            return [Region(path, a, b, "grep", "parsed") for a, b in _spans(ns)]

    rng = _BASH_SED_RANGE.search(command)
    fil = _BASH_SED_FILE.search(command)
    if rng and fil:
        return [
            Region(_relativize(fil.group(1), root), int(rng.group(1)), int(rng.group(2)),
                   "bash", "inferred")
        ]
    head = _BASH_HEAD.search(command)
    if head:
        return [
            Region(_relativize(head.group(2), root), 1, int(head.group(1)), "bash", "inferred")
        ]
    whole = _BASH_WHOLE.search(command)
    if whole:
        result = call.get("result") or ""
        lines = result.count("\n") + 1 if result else 0
        if 0 < lines <= MAX_INFERRED_LINES:
            return [Region(_relativize(whole.group(1), root), 1, lines, "bash", "inferred")]
    return []


def _from_edit(call: dict[str, Any], root: str | None) -> list[Region]:
    """Where the agent *acted*, which is utilization rather than exploration.

    The line numbers are not in an Edit call, so the region is the file with a
    zero-width span: enough to say which files were touched without pretending
    to know where. Callers score utilization at file level only.
    """
    path = _relativize(_file_of(call), root)
    return [Region(path, 0, 0, "edit", "inferred")] if path else []


def extract(calls: list[dict[str, Any]], *, root: str | None = None) -> Trajectory:
    """Reconstruct one trajectory from :func:`ctx.replay.parse_transcript` output.

    ``root`` is the repository path, used only to make absolute transcript
    paths repo-relative so they can be compared with gold annotations.
    """
    traj = Trajectory()
    for call in calls:
        tool = str(call.get("tool") or "")
        result = call.get("result") or ""
        command = str((call.get("input") or {}).get("command") or "")
        traj.calls += 1
        traj.visible_tokens += len(result.encode("utf-8", "replace")) // BYTES_PER_TOKEN

        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            traj.utilized.extend(_from_edit(call, root))
            traj.inert_calls += 1
            continue
        if tool in _NAME_ONLY:
            traj.inert_calls += 1  # understood, and correctly yields nothing
            continue

        if tool in ("Read", "read_file"):
            found = _from_read(call, root)
        elif tool in ("Grep", "grep_search", "search"):
            found = _from_grep(call, root)
        elif tool == "Bash":
            found = _from_bash(call, root)
        elif tool.startswith("mcp__ctx") or tool == "ctx":
            found = _from_ctx(call, root)
        else:
            found = _from_grep(call, root) or _from_ctx(call, root)

        found = [r for r in found if r.path]  # out-of-repo paths were refused
        if found:
            traj.observing_calls += 1
            traj.explored.extend(found)
            for r in found:
                traj.by_origin[r.origin] = traj.by_origin.get(r.origin, 0) + r.lines
                traj.by_basis[r.basis] = traj.by_basis.get(r.basis, 0) + r.lines
            continue

        # Nothing observed. Two very different reasons, and collapsing them
        # would hide the only signal that this module is missing a dialect.
        expected = (
            tool in ("Read", "read_file", "Grep", "grep_search")
            or tool.startswith("mcp__ctx")
            or (tool == "Bash" and (_reads_source(command) or bool(_CTX_READ.search(command))))
        )
        if expected:
            traj.unattributed_calls += 1
        else:
            traj.inert_calls += 1
    return traj


# --------------------------------------------------------------------------
# scoring against gold regions


def _prf(hit: int, retrieved: int, gold: int) -> dict[str, float]:
    r = hit / gold if gold else 0.0
    p = hit / retrieved if retrieved else 0.0
    f = 2 * r * p / (r + p) if (r + p) else 0.0
    return {"recall": round(r, 4), "precision": round(p, 4), "f1": round(f, 4)}


def line_sets(regions: Iterable[Region]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for r in regions:
        if r.end >= r.start >= 1:
            out.setdefault(r.path, set()).update(range(r.start, r.end + 1))
    return out


def score_regions(
    regions: list[Region] | list[dict[str, Any]],
    gold: list[dict[str, Any]],
    *,
    block_overlap: float = 0.5,
) -> dict[str, Any]:
    """File / block / line recall, precision and F1 against gold blocks.

    The canonical definition, shared by the trajectory scorer and
    ``evals/contextbench.py`` so the two cannot drift. ``gold`` is a list of
    ``{file, start, end}``. A gold block counts as retrieved when the observed
    lines cover at least ``block_overlap`` of it.
    """
    regs = [
        r if isinstance(r, Region) else Region(r["file"], r["start"], r["end"],
                                               r.get("origin", "?"), r.get("basis", "?"))
        for r in regions
    ]
    observed = line_sets(regs)
    gold_lines: dict[str, set[int]] = {}
    for b in gold:
        gold_lines.setdefault(b["file"], set()).update(range(b["start"], b["end"] + 1))

    hit_lines = sum(len(gold_lines[f] & observed.get(f, set())) for f in gold_lines)
    n_gold_lines = sum(len(v) for v in gold_lines.values())
    n_obs_lines = sum(len(v) for v in observed.values())

    blocks, hit_blocks = [], 0
    for b in gold:
        span = set(range(b["start"], b["end"] + 1))
        got = len(span & observed.get(b["file"], set()))
        cov = got / len(span) if span else 0.0
        ok = cov >= block_overlap
        hit_blocks += ok
        blocks.append(
            {
                "file": b["file"],
                "lines": f"{b['start']}:{b['end']}",
                "coverage": round(cov, 3),
                "verdict": "retrieved" if ok else ("partial" if cov else "missed"),
            }
        )

    return {
        "file": _prf(len(set(gold_lines) & set(observed)), len(observed), len(gold_lines)),
        "block": _prf(hit_blocks, len(regs), len(gold)),
        "line": _prf(hit_lines, n_obs_lines, n_gold_lines),
        "counts": {
            "gold_files": len(gold_lines),
            "gold_blocks": len(gold),
            "gold_lines": n_gold_lines,
            "observed_files": len(observed),
            "observed_regions": len(regs),
            "observed_lines": n_obs_lines,
            "gold_lines_seen": hit_lines,
        },
        "blocks": blocks,
    }


def score_trajectory(
    traj: Trajectory, gold: list[dict[str, Any]], *, block_overlap: float = 0.5
) -> dict[str, Any]:
    """Score a trajectory, and price it.

    Beyond the three granularities this adds the two numbers the A/B actually
    turns on:

    **Evidence density** — gold lines seen per model-visible token. This is
    where containment should show up if it shows up anywhere, because a digest
    changes the denominator without touching the numerator. Recall alone cannot
    see that; a native arm can reach the same gold and pay ten times for it.

    **Gold regret** — model-visible tokens minus the tokens of the gold context
    itself. The oracle is what a human said was needed, so unlike the
    facts-used oracle in :mod:`ctx.replay` this is not a lower bound that
    flatters the harness. It is what the turn would have cost if the agent had
    opened exactly the right regions and nothing else.

    **Explored versus utilized** — the paper's own finding, reported rather
    than assumed: the files the agent read against the files it edited.
    """
    scored = score_regions(traj.explored, gold, block_overlap=block_overlap)
    gold_tokens = sum(
        (b["end"] - b["start"] + 1) for b in gold
    ) * 10 // BYTES_PER_TOKEN  # ~10 bytes/line of source, the store's own rule of thumb
    seen = scored["counts"]["gold_lines_seen"]

    explored_files = {r.path for r in traj.explored}
    edited_files = {r.path for r in traj.utilized}

    scored["cost"] = {
        "visible_tokens": traj.visible_tokens,
        "gold_tokens": gold_tokens,
        "gold_regret": traj.visible_tokens - gold_tokens,
        "evidence_density": round(seen / traj.visible_tokens, 5) if traj.visible_tokens else 0.0,
    }
    scored["trajectory"] = {
        "calls": traj.calls,
        "observing_calls": traj.observing_calls,
        "inert_calls": traj.inert_calls,
        "unattributed_calls": traj.unattributed_calls,
        "parsed_share": traj.parsed_share,
        "by_origin": dict(sorted(traj.by_origin.items())),
        "explored_files": len(explored_files),
        "edited_files": len(edited_files),
        "explored_not_edited": len(explored_files - edited_files),
    }
    return scored


def render(scored: dict[str, Any], *, title: str = "") -> str:
    """One bounded block per scored trajectory, in the house digest shape."""
    c, t, cost = scored["counts"], scored["trajectory"], scored["cost"]
    out = [f"[ctx trajectory{(' ' + title) if title else ''}]"]
    out.append(
        f"  observed: {c['observed_files']} files · {c['observed_lines']:,} lines · "
        f"{t['observing_calls']}/{t['calls']} calls observed "
        f"({t['inert_calls']} inert, {t['unattributed_calls']} unattributed) · "
        f"parsed {t['parsed_share']:.0%}"
    )
    out.append(
        f"  gold:     {c['gold_files']} files · {c['gold_blocks']} blocks · "
        f"{c['gold_lines']:,} lines · {c['gold_lines_seen']:,} seen"
    )
    for level in ("file", "block", "line"):
        s = scored[level]
        out.append(
            f"  {level:<6}  recall {s['recall']:.3f}  precision {s['precision']:.3f}  "
            f"f1 {s['f1']:.3f}"
        )
    out.append(
        f"  cost:     {cost['visible_tokens']:,} visible tok · oracle {cost['gold_tokens']:,} · "
        f"regret {cost['gold_regret']:,} · density {cost['evidence_density']:.5f}"
    )
    out.append(
        f"  usage:    explored {t['explored_files']} files, edited {t['edited_files']}, "
        f"{t['explored_not_edited']} never edited"
    )
    if t["by_origin"]:
        rows = " · ".join(f"{k} {v:,}" for k, v in t["by_origin"].items())
        out.append(f"  origin:   {rows}")
    if t["parsed_share"] < 0.5:
        out.append(
            "  note:     under half of the observed lines came from real "
            "coordinates; whole-file attribution inflates recall and sinks "
            "precision, so read these as soft."
        )
    return "\n".join(out)


# --------------------------------------------------------------------------
# transcript entry point


GOLD_SCHEMA = "ctx.gold/v1"


def load_gold(path: str) -> dict[str, Any]:
    """Read a gold-context file.

    ``{"schema": "ctx.gold/v1", "instance_id": …, "root": …,
       "blocks": [{"file": …, "start": …, "end": …}]}``

    Produced by ``python evals/contextbench.py --emit-gold``. Kept as a plain
    file rather than a corpus import so ``ctx replay --gold`` needs no network,
    no dataset dependency, and no opinion about where the annotations came
    from — any corpus that can express regions can drive it.
    """
    import json

    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("schema") != GOLD_SCHEMA:
        raise ValueError(f"expected {GOLD_SCHEMA}, got {doc.get('schema')!r}")
    blocks = [
        {"file": str(b["file"]), "start": int(b["start"]), "end": int(b["end"])}
        for b in doc.get("blocks", [])
        if int(b.get("end", 0)) >= int(b.get("start", 0)) >= 1
    ]
    if not blocks:
        raise ValueError(f"{path} declares no usable gold blocks")
    return {**doc, "blocks": blocks}


def score_transcript(
    transcript: str, gold: dict[str, Any], *, block_overlap: float = 0.5
) -> dict[str, Any]:
    """Score one recorded session against gold regions. No model, no network.

    This is the free half of the ContextBench design: every archived transcript
    becomes retroactively scoreable the moment gold exists for its task, and
    the same call scores a ctx arm and a native arm identically.
    """
    from ctx.replay import parse_transcript

    traj = extract(parse_transcript(transcript), root=gold.get("root"))
    scored = score_trajectory(traj, gold["blocks"], block_overlap=block_overlap)
    scored["instance_id"] = gold.get("instance_id", "")
    scored["transcript"] = str(transcript)
    return scored
