#!/usr/bin/env python3
"""Live receipt: the ctx plugin on the real Antigravity CLI (`agy`), headless.

Three short sessions, each in its own throwaway home and repository, with
the plugin installed through the product's own path (``ctx wrap
antigravity``). Evidence comes from the host's transcript
(``<home>/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/
transcript.jsonl``), the hook's coverage ledger (``.ctx-session-reads/
coverage.jsonl``), the ctx store, and ``ctx coverage``.

  s1  a. a command with a large output (``python3 diagnose.py``, 4,000
         lines) is rewritten by PreToolUse (an argument ``overwrite``) and
         runs once through ``ctx run``: raw bytes vs bytes the model got;
      b. the model retrieves one fact from the stored output by the handle
         the digest printed (``ctx get run:...``);
      c. a search goes the way the governance table says (agy 1.5.5's
         default toolset has no grep_search/list_dir, so ``grep`` arrives as
         run_command and the command route rewrites it);
      d. ``ctx coverage`` afterwards.
  s2  f. a rule pre-approves ``ctx`` but none covers the original command:
         the rewrite goes out as ``force_ask`` and headless agy refuses it,
         as it would have refused the original.
  s3  e. a large result from a tool the hook cannot rewrite: asks for a URL
         fetch and records what the host actually offered and did.
  s4  a deny rule behind a transparent wrapper (``timeout 5 cat``): must
      end denied.
  s5  a command substitution under an allow rule (``cat x $(touch m)``):
      must end asked (refused headless), and the marker must not exist.

Nothing is written to the real home. Commands are granted with settings-file
allow rules; no permission-bypass flag is used. agy reads the API key from
``GEMINI_API_KEY``; it is never printed or recorded.

Usage:
  python evals/agy_rewrite_live.py --agy <path-to-agy> --out <dir> \
      [--model gemini-3.6-flash-low] [--sessions s1,s2,s3,s4,s5] [--dry]

``--dry`` installs and validates the plugin and lists the hooks agy loaded
(``agy -p /hooks`` answers without a model call). Results merge into
``<out>/record.json``; the receipt is written from that record.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

FLOOD_LINES = 4000
TARGET_LINE = 2500  # the fact (b) asks for: the request id on this line


def _diagnose_py() -> str:
    return f'''\
"""Print the gateway diagnostic log ({FLOOD_LINES} lines)."""
import hashlib

for i in range(1, {FLOOD_LINES + 1}):
    rid = hashlib.sha1(str(i).encode()).hexdigest()[:12]
    region, gw = ("apac", "legacy") if i == 1717 else ("us", "primary")
    print(f"[{{i:06d}}] INFO worker-{{i % 7}} request={{rid}} region={{region}} "
          f"gateway={{gw}} latency={{(i * 37) % 90}}ms ok")
'''


def _expected_request_id(line: int) -> str:
    import hashlib

    return hashlib.sha1(str(line).encode()).hexdigest()[:12]


def _env(home: pathlib.Path, state: pathlib.Path, ctx_bin: pathlib.Path) -> dict[str, str]:
    keep = ("GEMINI_API_KEY", "LANG", "TERM", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
            "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["HOME"] = str(home)
    env["CTX_STATE_HOME"] = str(state)
    env["PATH"] = f"{ctx_bin}:/usr/local/bin:/usr/bin:/bin"
    env.setdefault("NO_PROXY", "127.0.0.1,localhost")
    return env


def _run(argv, *, cwd, env, timeout=120, stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL if stdin is None else None,
                          input=stdin)


def setup(out: pathlib.Path, agy: str, allow: list[str], deny: tuple[str, ...] = ()) -> dict:
    """Throwaway home + workspace, plugin installed through `ctx wrap antigravity`."""
    home, ws, state = out / "home", out / "ws", out / "state"
    for d in (home, ws, state):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"modelProvider": "gemini",
                                    "permissions": {"allow": allow, "deny": list(deny)}},
                               indent=2))
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "diagnose.py").write_text(_diagnose_py())
    (ws / "notes.txt").write_text("deploy checklist: NEEDLE-7731 must be rotated first\n")
    (ws / "big.log").write_text("".join(f"{i:06d} INFO worker ok\n" for i in range(20000)))
    ctx_bin = pathlib.Path(shutil.which("ctx") or sys.executable).parent
    env = _env(home, state, ctx_bin)
    wrap = _run(["ctx", "wrap", "antigravity"], cwd=ws, env=env)
    validate = _run([agy, "plugin", "validate", ".agents/plugins/ctx-harness", "--json"],
                    cwd=ws, env=env)
    hooks = _run([agy, "-p", "/hooks", "--output-format", "json"], cwd=ws, env=env)
    return {"home": home, "ws": ws, "state": state, "env": env,
            "wrap_rc": wrap.returncode, "wrap_out": wrap.stdout[-1500:],
            "validate": validate.stdout, "validate_rc": validate.returncode,
            "hooks_listing": hooks.stdout[-6000:], "hooks_rc": hooks.returncode}


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a):
        pass


def serve(directory: pathlib.Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = lambda *a, **k: _Quiet(*a, directory=str(directory), **k)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def agy_print(agy: str, s: dict, prompt: str, model: str) -> dict:
    t0 = time.time()
    p = _run([agy, "-p", prompt, "--output-format", "json", "--model", model,
              "--print-timeout", "300s"], cwd=s["ws"], env=s["env"], timeout=400)
    try:
        result = json.loads(p.stdout)
    except ValueError:
        result = {"unparsed_stdout": p.stdout[-2000:]}
    return {"rc": p.returncode, "secs": round(time.time() - t0, 1), "result": result,
            "stderr": p.stderr[-2000:]}


def transcript(home: pathlib.Path, conversation: str) -> list[dict]:
    path = (home / ".gemini" / "antigravity-cli" / "brain" / conversation / ".system_generated"
            / "logs" / "transcript.jsonl")
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def tool_results(steps: list[dict]) -> list[dict]:
    """Pair each model tool call with the result text the model then received."""
    calls, out = [], []
    for st in steps:
        if st.get("type") == "PLANNER_RESPONSE":
            calls.extend(st.get("tool_calls") or [])
        elif st.get("source") == "MODEL" and st.get("type") != "PLANNER_RESPONSE" and calls:
            call = calls.pop(0)
            text = st.get("content") or ""
            out.append({"tool": call.get("name"), "args": call.get("args"),
                        "result_bytes": len(text.encode()),
                        "rewritten": "A pre-tool hook changed the arguments" in text,
                        "result_head": text[:600]})
    return out


def coverage_rows(ws: pathlib.Path) -> list[dict]:
    from ctx.sessiondir import session_reads_dir

    path = pathlib.Path(session_reads_dir(str(ws), "coverage.jsonl"))
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _keep(s: dict, record: dict, tag: str, run: dict) -> None:
    record[f"{tag}_run"] = run
    conv = (run["result"] or {}).get("conversation_id", "")
    steps = transcript(s["home"], conv)
    record[f"{tag}_tool_results"] = tool_results(steps)
    record[f"{tag}_model_calls"] = sum(1 for st in steps if st.get("type") == "PLANNER_RESPONSE")
    record[f"{tag}_coverage_rows"] = coverage_rows(s["ws"])
    if steps:
        shutil.copy2(s["home"] / ".gemini" / "antigravity-cli" / "brain" / conv
                     / ".system_generated" / "logs" / "transcript.jsonl",
                     s["ws"].parent.parent / f"{tag}-transcript.jsonl")


def session1(ns, out: pathlib.Path, record: dict) -> None:
    """a-d (and f when the model improvises a command no rule covers)."""
    allow = ["command(python3 diagnose.py)", "command(ctx)", "command(grep)",
             "read_url(127.0.0.1)"]
    s = setup(out / "s1", ns.agy, allow)
    record["s1_setup"] = {k: v for k, v in s.items() if k not in ("home", "ws", "state", "env")}
    record["s1_allow_rules"] = allow
    if ns.dry:
        return
    raw = subprocess.run([sys.executable, "diagnose.py"], cwd=s["ws"], capture_output=True)
    record["a_raw_stdout_bytes"] = len(raw.stdout)
    record["b_expected_request_id"] = _expected_request_id(TARGET_LINE)
    site = out / "site"
    site.mkdir(exist_ok=True)
    (site / "report.txt").write_text(_report_txt())
    srv, port = serve(site)
    prompt = (
        "Work in this repository and use your tools; do not guess.\n"
        "1. Run the shell command `python3 diagnose.py` exactly as written.\n"
        f"2. Using only what step 1 gives you, report the request id printed on output line "
        f"{TARGET_LINE} (the line starting [00{TARGET_LINE}]). If the output you got does not "
        "show that line, retrieve it with the handle the output gives you.\n"
        "3. Run `grep -rn NEEDLE .` to find which file mentions NEEDLE.\n"
        f"4. Fetch http://127.0.0.1:{port}/report.txt with your URL-reading tool and say how "
        "many rows it has.\n"
        "Finally answer in four short numbered lines, then one line listing the names of the "
        "tools you have available, comma-separated."
    )
    record["s1_prompt"] = prompt.replace(str(port), "<port>")
    run = agy_print(ns.agy, s, prompt, ns.model)
    srv.shutdown()
    _keep(s, record, "s1", run)
    cov = _run(["ctx", "coverage", "--host", "antigravity"], cwd=s["ws"], env=s["env"])
    record["d_coverage"] = cov.stdout


def session2(ns, out: pathlib.Path, record: dict) -> None:
    """f: a rule pre-approves ctx, none covers the original command."""
    allow = ["command(ctx)"]
    s = setup(out / "s2", ns.agy, allow)
    record["s2_allow_rules"] = allow
    if ns.dry:
        return
    run = agy_print(ns.agy, s, "Run the shell command `python3 diagnose.py` and tell me "
                    "how many lines it printed.", ns.model)
    _keep(s, record, "s2", run)


def session3(ns, out: pathlib.Path, record: dict) -> None:
    """e: a large result from a tool the hook cannot rewrite (read_url_content)."""
    allow = ["read_url(127.0.0.1)"]
    s = setup(out / "s3", ns.agy, allow)
    record["s3_allow_rules"] = allow
    if ns.dry:
        return
    site = out / "site"
    site.mkdir(exist_ok=True)
    (site / "report.txt").write_text(_report_txt())
    record["e_page_bytes"] = (site / "report.txt").stat().st_size
    srv, port = serve(site)
    prompt = (f"Fetch http://127.0.0.1:{port}/report.txt with your URL-reading tool (not a "
              "shell command) and tell me the checksum on the row numbered 02999. Then one "
              "line listing the names of the tools you have available, comma-separated.")
    record["s3_prompt"] = prompt.replace(str(port), "<port>")
    run = agy_print(ns.agy, s, prompt, ns.model)
    srv.shutdown()
    record["e_expected_checksum"] = 2999 * 7919 % 100003
    _keep(s, record, "s3", run)


def session4(ns, out: pathlib.Path, record: dict) -> None:
    """A deny rule behind a transparent wrapper (``timeout``): must end denied."""
    allow, deny = ["command(ctx)", "command(timeout)"], ["command(cat)"]
    s = setup(out / "s4", ns.agy, allow, tuple(deny))
    record["s4_rules"] = {"allow": allow, "deny": deny}
    if ns.dry:
        return
    run = agy_print(ns.agy, s, "Run exactly this shell command and nothing else: "
                    "`timeout 5 cat big.log`. Report what happened in one line.", ns.model)
    _keep(s, record, "s4", run)


def session5(ns, out: pathlib.Path, record: dict) -> None:
    """A command substitution under an allow rule: must end asked (refused headless)."""
    allow = ["command(ctx)", "command(cat)"]
    s = setup(out / "s5", ns.agy, allow)
    record["s5_rules"] = {"allow": allow}
    if ns.dry:
        return
    run = agy_print(ns.agy, s, "Run exactly this shell command and nothing else: "
                    "`cat big.log $(touch marker.txt)`. Report what happened in one line.",
                    ns.model)
    _keep(s, record, "s5", run)
    record["s5_marker_created"] = (s["ws"] / "marker.txt").exists()


def _report_txt() -> str:
    return "".join(f"row {i:05d} status=ok shard={i % 13} checksum={i * 7919 % 100003}\n"
                   for i in range(3000))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agy", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="gemini-3.6-flash-low")
    ap.add_argument("--sessions", default="s1,s2,s3,s4,s5",
                    help="which sessions to run; results merge into <out>/record.json")
    ap.add_argument("--dry", action="store_true")
    ns = ap.parse_args()
    out = pathlib.Path(ns.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("GEMINI_API_KEY") and not ns.dry:
        print("GEMINI_API_KEY is not set", file=sys.stderr)
        return 2
    version = _run([ns.agy, "--version"], cwd=out, env={"PATH": "/usr/bin:/bin",
                                                        "HOME": str(out)}).stdout.strip()
    rec_path = out / "record.json"
    record: dict = json.loads(rec_path.read_text()) if rec_path.is_file() and not ns.dry else {}
    record.update({"agy_version": version, "model": ns.model})
    for name in ns.sessions.split(","):
        {"s1": session1, "s2": session2, "s3": session3, "s4": session4,
         "s5": session5}[name.strip()](ns, out, record)
    if ns.dry:
        print(json.dumps(record, indent=2, default=str))
        return 0

    # Costs at the shipped list price. agy reports input_tokens inclusive of
    # cache_read_tokens, and output_tokens inclusive of thinking_tokens
    # (total_tokens == input_tokens + output_tokens).
    try:
        from ctx.pricing import cost_usd

        total = 0.0
        for tag in ("s1", "s2", "s3", "s4", "s5"):
            u = ((record.get(f"{tag}_run") or {}).get("result") or {}).get("usage") or {}
            total += cost_usd({"input": u.get("input_tokens", 0) - u.get("cache_read_tokens", 0),
                               "cache_read": u.get("cache_read_tokens", 0),
                               "output": u.get("output_tokens", 0)}, "gemini-3.6-flash")
        record["cost_usd_list_price"] = round(total, 5)
    except Exception as exc:  # pricing table may not know the model
        record["cost_usd_list_price"] = f"unpriced ({type(exc).__name__})"
    text = json.dumps(record, indent=2, default=str)
    if os.environ.get("GEMINI_API_KEY"):
        assert os.environ["GEMINI_API_KEY"] not in text
    text = re.sub(re.escape(str(out)), "<out>", text)
    rec_path.write_text(text)
    print(text[-3000:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
