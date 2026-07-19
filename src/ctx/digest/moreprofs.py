"""Additional deterministic digest profiles: Go test, Jest/Vitest,
compiler/linter diagnostics, and git diff (SPEC §9)."""

from __future__ import annotations

import re
from collections import Counter

from ctx.digest.base import DigestContext, Profile
from ctx.textutil import fmt_int

_GO_FAIL_RE = re.compile(r"^--- FAIL: (\S+)", re.MULTILINE)
_GO_PKG_RE = re.compile(r"^(ok|FAIL|---)\s", re.MULTILINE)


class GoTestProfile(Profile):
    version = "gotest/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        argv = ctx.manifest["argv"]
        if len(argv) >= 2 and argv[0] == "go" and argv[1] == "test":
            return "argv invokes go test"
        head = ctx.stdout.text[:4000]
        if "--- FAIL:" in head or ("=== RUN" in head and "--- PASS:" in ctx.stdout.text):
            return "stdout contains go test markers"
        return None

    def render(self, ctx: DigestContext) -> str:
        text = ctx.stdout.text
        fails = _GO_FAIL_RE.findall(text)
        passes = len(re.findall(r"^--- PASS: ", text, re.MULTILINE))
        pkg_ok = len(re.findall(r"^ok\s", text, re.MULTILINE))
        pkg_fail = len(re.findall(r"^FAIL\s", text, re.MULTILINE))
        summary = [
            "summary:",
            f"  tests: passed {fmt_int(passes)} · failed {fmt_int(len(fails))}"
            f" · packages ok {pkg_ok} · packages failed {pkg_fail}",
        ]
        shown = 0
        for i, line in enumerate(text.splitlines(), start=1):
            m = re.match(r"^--- FAIL: (\S+)", line)
            if m:
                summary.append(f"  first failure stdout:L{i}: {m.group(1)}")
                shown += 1
                break
        rid = "run:PENDING"
        suggestions = [f"ctx search {rid} 'FAIL' --context 4"]
        if fails:
            suggestions.insert(0, f"ctx search {rid} '{fails[0]}' --context 6")
        return "\n".join(
            ctx.header_lines() + summary + self.coverage_lines(ctx, shown or 1) + self.next_lines(ctx, suggestions)
        )


_CARGO_RESULT_RE = re.compile(
    r"^test result: (?P<status>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed; "
    r"(?P<ignored>\d+) ignored; (?P<measured>\d+) measured; (?P<filtered>\d+) filtered out",
    re.MULTILINE,
)
_CARGO_FAILED_RE = re.compile(r"^test (\S+) \.\.\. FAILED$")
_CARGO_PANIC_RE = re.compile(r"panicked at (?P<loc>\S+?:\d+:\d+)")


class CargoTestProfile(Profile):
    """Rust test harness (``cargo test`` / bare libtest binaries).

    Measured motivation (evals/coverage-corpus-2026-07-19.md): 150 tests with
    6 failures under text/v1 compressed 26× but named exactly one failing
    test — the spec3 lesson (a test digest without a failing census starves
    the fix loop) replayed on a second runner. Detection is anchored on the
    ``test result:`` shape, never argv alone, so ``cargo test`` runs that die
    in the compiler correctly fall through to lint/build profiles.
    """

    version = "cargotest/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        combined = ctx.stdout.text + "\n" + ctx.stderr.text
        if not _CARGO_RESULT_RE.search(combined):
            return None
        if not re.search(r"^running \d+ tests?$", combined, re.MULTILINE):
            return None
        argv = ctx.manifest["argv"]
        if argv and argv[0].rsplit("/", 1)[-1] == "cargo":
            return "argv is cargo with libtest result lines"
        return "libtest 'running N tests' + 'test result:' shape"

    def render(self, ctx: DigestContext) -> str:
        combined = ctx.stdout.text + "\n" + ctx.stderr.text
        passed = failed = ignored = 0
        suites_ok = suites_failed = 0
        for m in _CARGO_RESULT_RE.finditer(combined):
            passed += int(m.group("passed"))
            failed += int(m.group("failed"))
            ignored += int(m.group("ignored"))
            if m.group("status") == "ok":
                suites_ok += 1
            else:
                suites_failed += 1
        summary = [
            "summary:",
            f"  tests (exact): passed {fmt_int(passed)} · failed {fmt_int(failed)}"
            f" · ignored {fmt_int(ignored)} · suites ok {suites_ok} · failed {suites_failed}",
        ]
        shown = 1

        # Failing-test census: one line per failure with real coordinates —
        # the census is the work queue (rtk-corpus lesson: structure at equal
        # budget, and per-item addresses for the repair loop).
        cap = failed if getattr(ctx, "dense", False) else 10
        listed = 0
        for view in (ctx.stdout, ctx.stderr):
            for i, ln in enumerate(view.text_lines, start=1):
                fm = _CARGO_FAILED_RE.match(ln)
                if fm:
                    if listed < cap:
                        summary.append(f"  failing: {fm.group(1)} · {view.name}:L{i}")
                        shown += 1
                    listed += 1
        if listed > cap:
            summary.append(f"  … {fmt_int(listed - cap)} more failing tests (complete list in 'failures:' section)")

        # First panic region inline: location + message line, anticipatory.
        for view in (ctx.stdout, ctx.stderr):
            done = False
            for i, ln in enumerate(view.text_lines, start=1):
                pm = _CARGO_PANIC_RE.search(ln)
                if pm:
                    msg = ""
                    rest = ln.split(pm.group(0), 1)[1].lstrip(": ").strip()
                    nxt = view.text_lines[i] if i < len(view.text_lines) else ""
                    msg = rest or nxt.strip()
                    summary.append(
                        f"  first panic {view.name}:L{i}: {pm.group('loc')}: {msg[:140]}"
                    )
                    shown += 1
                    done = True
                    break
            if done:
                break

        rid = "run:PENDING"
        suggestions = [f"ctx search {rid} 'FAILED' --context 0"]
        if failed:
            suggestions.insert(0, f"ctx search {rid} 'panicked at' --context 4")
        return "\n".join(
            ctx.header_lines()
            + summary
            + self.coverage_lines(ctx, shown)
            + self.next_lines(ctx, suggestions)
        )


_UNITTEST_RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_UNITTEST_VERDICT_RE = re.compile(
    r"^(?:OK|FAILED \((?P<detail>[^)]+)\))\s*$", re.MULTILINE
)
_UNITTEST_FAIL_RE = re.compile(r"^(FAIL|ERROR): (\S+) \(([\w.]+)\)")


class UnittestProfile(Profile):
    """Python unittest runner shape — vanilla unittest and Django's
    ``runtests.py``. Measured motivation (SWE-bench mine, 2026-07-19):
    Django failure floods fell to text/v1 with no failing census; SPEC §9
    lists unittest as a required row. Same lesson, third runner: the
    census is the work queue."""

    version = "unittest/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        combined = ctx.stdout.text + "\n" + ctx.stderr.text
        if not _UNITTEST_RAN_RE.search(combined):
            return None
        if not _UNITTEST_VERDICT_RE.search(combined):
            return None
        return "unittest 'Ran N tests' + OK/FAILED verdict shape"

    def render(self, ctx: DigestContext) -> str:
        combined = ctx.stdout.text + "\n" + ctx.stderr.text
        ran = sum(int(m.group(1)) for m in _UNITTEST_RAN_RE.finditer(combined))
        failures = errors = 0
        for m in _UNITTEST_VERDICT_RE.finditer(combined):
            for part in (m.group("detail") or "").split(","):
                k, _, v = part.strip().partition("=")
                if k == "failures":
                    failures += int(v or 0)
                elif k == "errors":
                    errors += int(v or 0)
        summary = [
            "summary:",
            f"  tests (exact): ran {fmt_int(ran)} · failures {fmt_int(failures)}"
            f" · errors {fmt_int(errors)}",
        ]
        shown = 1
        cap = (failures + errors) if getattr(ctx, "dense", False) else 10
        listed = 0
        for view in (ctx.stdout, ctx.stderr):
            for i, ln in enumerate(view.text_lines, start=1):
                fm = _UNITTEST_FAIL_RE.match(ln)
                if fm:
                    if listed < cap:
                        summary.append(
                            f"  {fm.group(1).lower()}: {fm.group(3)}.{fm.group(2)}"
                            f" · {view.name}:L{i}"
                        )
                        shown += 1
                    listed += 1
        if listed > cap:
            summary.append(f"  … {fmt_int(listed - cap)} more failing tests")
        # First failure region: the exception line AND the innermost
        # traceback frame. The frame names the file the fix lands in —
        # measured directly (SWE-bench django-13568: census-only rendering
        # dropped the gold file the traceback carried; gold-anchored replay
        # flagged it as the first digest-dropped defect).
        for view in (ctx.stdout, ctx.stderr):
            lines = view.text_lines
            for i, ln in enumerate(lines, start=1):
                if _UNITTEST_FAIL_RE.match(ln):
                    frame_j = exc_j = None
                    for j in range(i, min(i + 60, len(lines))):
                        if re.match(r'^\s+File "', lines[j]):
                            frame_j = j  # keep the LAST = innermost frame
                        if re.match(r"^\w+(\.\w+)*(Error|Exception)\b", lines[j]):
                            exc_j = j
                            break
                    if frame_j is not None:
                        summary.append(
                            f"  innermost frame {view.name}:L{frame_j + 1}: {lines[frame_j].strip()[:160]}"
                        )
                        shown += 1
                    if exc_j is not None:
                        summary.append(
                            f"  first failure {view.name}:L{exc_j + 1}: {lines[exc_j].strip()[:160]}"
                        )
                        shown += 1
                    break
            else:
                continue
            break
        rid = "run:PENDING"
        suggestions = [f"ctx search {rid} 'FAIL' 'ERROR' --context 0"]
        if failures or errors:
            suggestions.insert(0, f"ctx search {rid} 'Traceback' --context 6")
        return "\n".join(
            ctx.header_lines()
            + summary
            + self.coverage_lines(ctx, shown)
            + self.next_lines(ctx, suggestions)
        )


_JEST_SUMMARY_RE = re.compile(
    r"^(Tests|Test Suites):\s+(?P<body>.+)$", re.MULTILINE
)
_JEST_COUNT_RE = re.compile(r"(\d+) (failed|passed|skipped|todo|total)")
_JEST_FAIL_FILE_RE = re.compile(r"^\s*(?:FAIL|✕|×)\s+(.+)$", re.MULTILINE)


class JestProfile(Profile):
    version = "jest/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        joined = " ".join(ctx.manifest["argv"])
        combined = ctx.stdout.text + "\n" + ctx.stderr.text
        if _JEST_SUMMARY_RE.search(combined):
            if "jest" in joined or "vitest" in joined or "Test Suites:" in combined or "Test Files" in combined:
                return "jest/vitest summary lines present"
            return "test-runner summary lines present"
        return None

    def render(self, ctx: DigestContext) -> str:
        combined = ctx.stdout.text + "\n" + ctx.stderr.text
        summary = ["summary:"]
        for m in _JEST_SUMMARY_RE.finditer(combined):
            counts = dict((k, int(n)) for n, k in _JEST_COUNT_RE.findall(m.group("body")))
            label = m.group(1).lower()
            parts = " · ".join(f"{k} {fmt_int(v)}" for k, v in sorted(counts.items()))
            summary.append(f"  {label}: {parts}")
        shown = 0
        fail_names = _JEST_FAIL_FILE_RE.findall(combined)
        for name in list(dict.fromkeys(n.strip() for n in fail_names))[:3]:
            # Strip elapsed-time suffixes like "(12 ms)" — no timing noise.
            name = re.sub(r"\s*\(\d+\s*m?s\)\s*$", "", name)
            summary.append(f"  failing: {name[:140]}")
            shown += 1
        rid = "run:PENDING"
        return "\n".join(
            ctx.header_lines()
            + summary
            + self.coverage_lines(ctx, shown or 1)
            + self.next_lines(ctx, [f"ctx search {rid} '●' 'FAIL' --context 4"])
        )


# path:line:col: severity: message  (gcc/clang/tsc/eslint/ruff/mypy/rustc-ish)
_DIAG_RE = re.compile(
    r"^(?P<path>[^\s:][^:\n]*):(?P<line>\d+)(?::\d+)?[:\s-]+\s*(?P<sev>error|warning|note)\b",
    re.MULTILINE | re.IGNORECASE,
)
_BUILD_CMDS = {"tsc", "eslint", "ruff", "mypy", "pyright", "flake8", "pylint",
               "gcc", "g++", "clang", "clang++", "rustc", "cargo", "javac", "make", "ninja"}


class BuildProfile(Profile):
    version = "build/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        prog = (ctx.manifest["argv"] or [""])[0].rsplit("/", 1)[-1]
        combined = ctx.stdout.text + "\n" + ctx.stderr.text
        hits = len(_DIAG_RE.findall(combined[:200_000]))
        if prog in _BUILD_CMDS and hits:
            return f"argv is build/lint tool with {hits} diagnostics"
        if hits >= 3:
            return f"{hits} compiler-style path:line diagnostics"
        return None

    def render(self, ctx: DigestContext) -> str:
        summary = ["summary:"]
        shown = 0
        sev_counts: Counter[str] = Counter()
        by_file: Counter[str] = Counter()
        for view in (ctx.stdout, ctx.stderr):
            for m in _DIAG_RE.finditer(view.text):
                sev_counts[m.group("sev").lower()] += 1
                by_file[m.group("path")] += 1
        summary.append(
            "  diagnostics (exact): "
            + (" · ".join(f"{k} {fmt_int(v)}" for k, v in sorted(sev_counts.items())) or "none parsed")
        )
        if by_file:
            summary.append("  top files:")
            for path, n in sorted(by_file.most_common(4)):
                summary.append(f"    {path}  {n}")
                shown += 1
        for view in (ctx.stderr, ctx.stdout):
            for i, ln in enumerate(view.text_lines, start=1):
                dm = _DIAG_RE.match(ln)
                if dm and dm.group("sev").lower() == "error":
                    summary.append(f"  first error {view.name}:L{i}: {ln.strip()[:160]}")
                    shown += 1
                    break
            else:
                continue
            break
        rid = "run:PENDING"
        return "\n".join(
            ctx.header_lines()
            + summary
            + self.coverage_lines(ctx, shown or 1)
            + self.next_lines(ctx, [f"ctx search {rid} 'error' --context 2"])
        )


class GitDiffProfile(Profile):
    version = "gitdiff/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        argv = ctx.manifest["argv"]
        if argv and argv[0].rsplit("/", 1)[-1] == "git" and any(
            a in ("diff", "show", "log") for a in argv[1:3]
        ):
            return "argv invokes git diff/show/log"
        if ctx.stdout.text.startswith("diff --git "):
            return "stdout is unified diff"
        return None

    def render(self, ctx: DigestContext) -> str:
        text = ctx.stdout.text
        files = re.findall(r"^diff --git a/(\S+) b/\S+", text, re.MULTILINE)
        adds = sum(1 for ln in text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        dels = sum(1 for ln in text.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        commits = re.findall(r"^commit ([0-9a-f]{7,40})", text, re.MULTILINE)
        summary = ["summary:"]
        if commits:
            summary.append(f"  commits (exact): {fmt_int(len(commits))} · first {commits[0][:12]} · last {commits[-1][:12]}")
        if files:
            summary.append(f"  files changed (exact): {fmt_int(len(files))} · +{fmt_int(adds)} · -{fmt_int(dels)}")
            per_file: Counter[str] = Counter()
            current = None
            for ln in text.splitlines():
                m = re.match(r"^diff --git a/(\S+)", ln)
                if m:
                    current = m.group(1)
                elif current and (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---")):
                    per_file[current] += 1
            summary.append("  largest changes:")
            for path, n in sorted(per_file.most_common(4)):
                summary.append(f"    {path}  ±{n}")
        elif not commits:
            summary.append("  no diff hunks or commits parsed")
        rid = "run:PENDING"
        return "\n".join(
            ctx.header_lines()
            + summary
            + self.coverage_lines(ctx, min(4, len(files)) or 1)
            + self.next_lines(ctx, [f"ctx search {rid} 'diff --git' --context 0"])
        )
