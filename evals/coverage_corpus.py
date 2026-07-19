"""Coverage-corpus benchmark: which systems does the digest layer actually serve?

The rtk-corpus method (evals/rtk-corpus-2026-07-18.md) made re-runnable.
Every corpus is a (argv, captured bytes, exit code) triple replayed through a
stub binary carrying the real tool's name, so `ctx run` exercises the true
capture path: argv-anchored detection, shape-based fallback, slim inline,
budgets, telemetry. Live corpora are generated from real toolchains present
on the machine (cargo, pip, ps, find); tools absent here (docker daemon,
kubectl, gh, mvn, rspec) replay faithfully-shaped fixtures and are labeled
`replay` in the report — provenance is a column, never a footnote.

Usage:
    python evals/coverage_corpus.py [--workdir DIR] [--out report.md]

The report is a markdown table: corpus · source · raw tokens · digest tokens
· ratio · claiming profile · what the digest actually says (first census
line). Hypotheses about new profiles are made or killed by this table, per
the house rule: measure on real corpora before building.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx.textutil import estimate_tokens  # noqa: E402

# --------------------------------------------------------------------------
# corpus definitions
# --------------------------------------------------------------------------


@dataclass
class Corpus:
    name: str
    family: str  # rtk/SPEC family this exercises
    argv: list[str]  # what the agent would have typed
    text: str
    exit_code: int
    source: str  # "live" | "replay"


def _run(cmd: list[str], cwd: str | None = None, env: dict | None = None) -> tuple[str, int]:
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True)
    return p.stdout + p.stderr, p.returncode


def _seed_rust_crate(root: Path, failing: int) -> Path:
    """A crate with 150 tests; `failing` of them panic. Real cargo output."""
    proj = root / ("rust-fail" if failing else "rust-pass")
    (proj / "src").mkdir(parents=True, exist_ok=True)
    (proj / "Cargo.toml").write_text(
        '[package]\nname = "covbench"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    tests = []
    for i in range(150):
        if i < failing:
            body = f'assert_eq!(add({i}, 1), {i}, "case {i} drifted");'
        else:
            body = f"assert_eq!(add({i}, 1), {i + 1});"
        tests.append(f"    #[test] fn case_{i:03}() {{ {body} }}")
    (proj / "src" / "lib.rs").write_text(
        "pub fn add(a: i64, b: i64) -> i64 { a + b }\n\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n" + "\n".join(tests) + "\n}\n",
        encoding="utf-8",
    )
    return proj


def live_corpora(work: Path) -> list[Corpus]:
    out: list[Corpus] = []
    if shutil.which("cargo"):
        for failing in (6, 0):
            proj = _seed_rust_crate(work, failing)
            text, code = _run(
                ["cargo", "test"], cwd=str(proj), env={"RUST_BACKTRACE": "1"}
            )
            out.append(
                Corpus(
                    f"cargo test ({'6 of 150 failing' if failing else '150 passing'})",
                    "test-runner (rust)",
                    ["cargo", "test"],
                    text,
                    code,
                    "live",
                )
            )
    if shutil.which("pip"):
        text, code = _run(["pip", "list"])
        out.append(Corpus("pip list", "package listing", ["pip", "list"], text, code, "live"))
    text, code = _run(["ps", "aux"])
    out.append(Corpus("ps aux", "process table", ["ps", "aux"], text, code, "live"))
    repo = Path(__file__).resolve().parent.parent
    text, code = _run(["find", "src", "tests", "-type", "f"], cwd=str(repo))
    out.append(
        Corpus("find src tests -type f", "directory tree", ["find", "src", "tests", "-type", "f"], text, code, "live")
    )
    return out


# Fixtures: shaped after real tool output (formats verified against current
# docs/READMEs); volumes chosen from the flood regime where digests matter.


def _pad_table(header: list[str], data: list[list[str]], gap: int = 3) -> str:
    """Column-pad like docker/kubectl tabwriters: every cell padded to the
    widest value in its column."""
    widths = [
        max(len(header[c]), *(len(r[c]) for r in data)) for c in range(len(header))
    ]
    def fmt(row: list[str]) -> str:
        return (" " * gap).join(v.ljust(w) for v, w in zip(row, widths)).rstrip()
    return "\n".join([fmt(header)] + [fmt(r) for r in data]) + "\n"


def _docker_ps(rows: int) -> str:
    data = []
    for i in range(rows):
        state = "Up 3 hours" if i % 7 else "Exited (137) 2 hours ago"
        port = f"0.0.0.0:{8000 + i}->8080/tcp" if i % 7 else ""
        data.append(
            [
                f"{i:012x}",
                f"registry.local/svc-{i % 9}:1.{i % 14}",
                '"/entrypoint.sh run"',
                "4 hours ago",
                state,
                port,
                f"svc-{i % 9}-replica-{i}",
            ]
        )
    return _pad_table(
        ["CONTAINER ID", "IMAGE", "COMMAND", "CREATED", "STATUS", "PORTS", "NAMES"], data
    )


def _kubectl_pods(rows: int) -> str:
    data = []
    for i in range(rows):
        if i % 23 == 3:
            status, ready, restarts = "CrashLoopBackOff", "0/1", str(14 + i % 5)
        elif i % 31 == 7:
            status, ready, restarts = "ImagePullBackOff", "0/1", "0"
        else:
            status, ready, restarts = "Running", "1/1", "0"
        data.append(
            [f"payments-worker-{i:04}-{'abcdef'[i % 6] * 5}", ready, status, restarts, f"{i % 40}d"]
        )
    return _pad_table(["NAME", "READY", "STATUS", "RESTARTS", "AGE"], data)


def _gh_pr_list(rows: int) -> str:
    lines = []
    for i in range(rows):
        state = "OPEN" if i % 4 else "DRAFT"
        lines.append(
            f"{300 - i}\tfix: contain flood in digest path wave {i}\tclaude/wave-{i}\t{state}\t2026-07-{(i % 28) + 1:02}"
        )
    return "\n".join(lines) + "\n"


def _mvn_test() -> str:
    head = textwrap.dedent(
        """\
        [INFO] Scanning for projects...
        [INFO] -------------------< com.example:payments-core >--------------------
        [INFO] Building payments-core 2.4.1
        [INFO] --------------------------------[ jar ]---------------------------------
        [INFO] --- maven-surefire-plugin:3.2.5:test (default-test) @ payments-core ---
        [INFO] -------------------------------------------------------
        [INFO]  T E S T S
        [INFO] -------------------------------------------------------
        """
    )
    body = []
    for i in range(140):
        fails = 2 if i in (4, 11) else 0
        body.append(
            f"[INFO] Running com.example.payments.Suite{i:02}Test\n"
            + (
                f"[ERROR] Tests run: 24, Failures: {fails}, Errors: 0, Skipped: 1, "
                f"Time elapsed: 3.{i} s <<< FAILURE! -- in com.example.payments.Suite{i:02}Test\n"
                f"[ERROR] com.example.payments.Suite{i:02}Test.roundTrip -- Time elapsed: 0.41 s <<< FAILURE!\n"
                "org.opentest4j.AssertionFailedError: expected: <404> but was: <500>\n"
                f"\tat com.example.payments.Suite{i:02}Test.roundTrip(Suite{i:02}Test.java:88)\n"
                if fails
                else f"[INFO] Tests run: 24, Failures: 0, Errors: 0, Skipped: 1, Time elapsed: 1.{i} s\n"
            )
        )
    tail = textwrap.dedent(
        """\
        [INFO] Results:
        [ERROR] Failures:
        [ERROR]   Suite04Test.roundTrip:88 expected: <404> but was: <500>
        [ERROR]   Suite11Test.roundTrip:88 expected: <404> but was: <500>
        [ERROR] Tests run: 432, Failures: 4, Errors: 0, Skipped: 18
        [INFO] BUILD FAILURE
        [INFO] Total time:  41.335 s
        """
    )
    return head + "".join(body) + tail


def _rspec() -> str:
    dots = ("." * 60 + "F" + "." * 40 + "F" + "." * 30) + "\n\n"
    fails = textwrap.dedent(
        """\
        Failures:

          1) Checkout::Totals applies the bulk discount over 100 units
             Failure/Error: expect(totals.discount).to eq(Money.new(1200))

               expected: #<Money fractional:1200 currency:USD>
                    got: #<Money fractional:0 currency:USD>

               (compared using ==)
             # ./spec/checkout/totals_spec.rb:48:in `block (3 levels) in <top (required)>'

          2) Checkout::Totals rounds tax at the line-item level
             Failure/Error: expect(totals.tax).to eq(Money.new(817))

               expected: #<Money fractional:817 currency:USD>
                    got: #<Money fractional:818 currency:USD>

               (compared using ==)
             # ./spec/checkout/totals_spec.rb:61:in `block (3 levels) in <top (required)>'

        Finished in 4.83 seconds (files took 1.9 seconds to load)
        132 examples, 2 failures

        Failed examples:

        rspec ./spec/checkout/totals_spec.rb:47 # Checkout::Totals applies the bulk discount over 100 units
        rspec ./spec/checkout/totals_spec.rb:60 # Checkout::Totals rounds tax at the line-item level
        """
    )
    return dots + fails


def _aws_ec2(rows: int) -> str:
    import json

    instances = [
        {
            "InstanceId": f"i-0{i:015x}",
            "InstanceType": "m7g.large" if i % 3 else "r7g.xlarge",
            "State": {"Name": "running" if i % 11 else "stopped"},
            "PrivateIpAddress": f"10.42.{i // 250}.{i % 250}",
            "Tags": [{"Key": "Name", "Value": f"batch-worker-{i}"}],
            "LaunchTime": "2026-07-01T00:00:00+00:00",
        }
        for i in range(rows)
    ]
    return json.dumps({"Reservations": [{"Instances": instances}]}, indent=4) + "\n"


def replay_corpora() -> list[Corpus]:
    return [
        Corpus("docker ps -a (40 containers)", "container table", ["docker", "ps", "-a"], _docker_ps(40), 0, "replay"),
        Corpus("kubectl get pods (180 pods)", "container table", ["kubectl", "get", "pods"], _kubectl_pods(180), 0, "replay"),
        Corpus("gh pr list (30 PRs)", "forge listing", ["gh", "pr", "list", "--limit", "30"], _gh_pr_list(30), 0, "replay"),
        Corpus("mvn test (3,360 tests, 4 failing)", "test-runner (jvm)", ["mvn", "test"], _mvn_test(), 1, "replay"),
        Corpus("rspec (132 examples, 2 failing)", "test-runner (ruby)", ["rspec"], _rspec(), 1, "replay"),
        Corpus("aws ec2 describe-instances (120)", "cloud json", ["aws", "ec2", "describe-instances"], _aws_ec2(120), 0, "replay"),
    ]


# --------------------------------------------------------------------------
# measurement: replay every corpus through the real `ctx run` capture path
# --------------------------------------------------------------------------

_PROFILE_RE = re.compile(r"profile=(\S+)\]")


def measure(corpus: Corpus, work: Path) -> dict:
    ws = work / "ws" / re.sub(r"\W+", "-", corpus.name)
    ws.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "ctx", "init"], cwd=str(ws), capture_output=True
    )
    bindir = ws / ".stub-bin"
    bindir.mkdir(exist_ok=True)
    payload = ws / ".stub-bin" / "payload.txt"
    payload.write_text(corpus.text, encoding="utf-8")
    stub = bindir / corpus.argv[0]
    stub.write_text(
        f'#!/bin/sh\ncat "{payload}"\nexit {corpus.exit_code}\n', encoding="utf-8"
    )
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    p = subprocess.run(
        [sys.executable, "-m", "ctx", "run", "--"] + corpus.argv,
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
    )
    digest = p.stdout
    m = _PROFILE_RE.search(digest)
    first_summary = ""
    lines = digest.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() in ("summary:", "output (complete):") and i + 1 < len(lines):
            first_summary = lines[i + 1].strip()[:90]
            break
    return {
        "corpus": corpus,
        "raw_tokens": estimate_tokens(len(corpus.text.encode("utf-8"))),
        "digest_tokens": estimate_tokens(len(digest.encode("utf-8"))),
        "profile": m.group(1) if m else "?",
        "summary": first_summary,
        "digest": digest,
    }


def report(rows: list[dict]) -> str:
    out = [
        "| corpus | source | raw tok | digest tok | ratio | profile | first summary line |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        c = r["corpus"]
        ratio = (r["raw_tokens"] / r["digest_tokens"]) if r["digest_tokens"] else 0.0
        out.append(
            f"| {c.name} | {c.source} | {r['raw_tokens']:,} | {r['digest_tokens']:,} "
            f"| {ratio:.1f}× | {r['profile']} | {r['summary']} |"
        )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dump-digests", action="store_true", help="print each digest body")
    args = ap.parse_args()
    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="covbench-"))
    work.mkdir(parents=True, exist_ok=True)

    rows = [measure(c, work) for c in live_corpora(work) + replay_corpora()]
    table = report(rows)
    if args.out:
        Path(args.out).write_text(table + "\n", encoding="utf-8")
    print(table)
    if args.dump_digests:
        for r in rows:
            print(f"\n===== {r['corpus'].name} ({r['profile']}) =====")
            print(r["digest"])


if __name__ == "__main__":
    main()
