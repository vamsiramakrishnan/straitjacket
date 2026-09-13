#!/usr/bin/env python3
"""DeepSWE v1.1 adapter (datacurve-ai/deep-swe), graded with the task's own verifier.

DeepSWE is 113 original, long-horizon feature tasks on active open-source
repositories across TypeScript, Go, Python, JavaScript and Rust. Every task is
a Harbor-format directory:

    task.toml        repo, base commit, language, image, limits
    instruction.md   the prompt the agent sees, verbatim
    environment/     Dockerfile reproducing the prebuilt image
    tests/           test.sh + grader.py + config.json + test.patch (held out)
    solution/        the reference patch (validate.py only, never the harness)

v1.1 grading semantics, which this adapter reproduces exactly:

1. Only COMMITTED work counts. The `[[verifier.collect]]` hook in task.toml is
   `git diff --binary <base> HEAD > model.patch`; uncommitted edits are lost.
2. The patch is applied to a PRISTINE checkout in a separate environment, so
   an agent cannot monkey-patch the test runner or leave state behind.
3. `test.patch` (the held-out tests) is applied AFTER the model patch, with
   every file it touches reset first, so editing tests cannot help.
4. `grader.py grade` scores a whitelist of JUnit/CTRF node ids: reward is 1
   iff every fail-to-pass id passes and no pass-to-pass id fails; an id
   missing from the report is a failure.

What is different here: there is no docker. The image's `RUN`/`ENV` lines
are replayed into a per-run virtualenv (`uv venv --seed`) with `/app`
rewritten to the checkout path, the agent gets that venv on PATH, and the
verifier runs from a pristine COPY of the venv taken before the agent
started -- the same "clean container" property, one directory over. Python
tasks only; the Go/TS/Rust images need toolchain steps this replay does not
translate. `validate.py --adapter deepswe` proves the referee per task (gold
resolves; base, cheating tests, and vandalised source do not) before any paid
arm is run, because a task whose environment did not build correctly must
fail loudly there rather than silently score both arms 0.

The corpus is fetched into evals/_cache/deep-swe on first use (or point
`--adapter-arg tasks=/path/to/deep-swe/tasks`). Selection is deterministic:
`language=python` (default), optional `ids=a,b,c`, sorted by task id.

Usage:
    python evals/agentbench/validate.py --adapter deepswe --adapter-arg ids=cattrs-partial-structuring-recovery
    python evals/agentbench/harness.py --adapter deepswe --arms naive sj --model haiku \
        --adapter-arg ids=... --max-turns 60 --jobs 4
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE.parent / "_cache"
CORPUS_URL = "https://github.com/datacurve-ai/deep-swe.git"

# Files the harness (both arms) or the sj wrapper leave in the workspace. They
# are excluded via .git/info/exclude so an instruction that says "commit
# everything" cannot sweep harness state into model.patch. The working tree
# both arms see is otherwise identical.
WORKSPACE_EXCLUDES = (
    "ctx.toml", ".ctxignore", ".ctx/", ".ctx-session-reads/", ".ctx-surface/",
    ".claude/", "CLAUDE.md", "AGENTS.md",
)

# Dockerfile RUN steps that build the image's git time-travel or assert its
# cleanliness. The checkout is materialized by this adapter instead.
_SKIP_RUN = ("git clone", "git status --porcelain", "core.hooksPath")


# --------------------------------------------------------------------- corpus

def _toml_str(text: str, key: str) -> str | None:
    m = re.search(rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else None


def _toml_num(text: str, section: str, key: str, default: float) -> float:
    m = re.search(rf'^\[{re.escape(section)}\]\s*\n(.*?)(?=^\[|\Z)', text, re.MULTILINE | re.DOTALL)
    if not m:
        return default
    k = re.search(rf'^\s*{re.escape(key)}\s*=\s*([\d.]+)', m.group(1), re.MULTILINE)
    return float(k.group(1)) if k else default


def _corpus_root(kw: dict) -> pathlib.Path:
    explicit = kw.get("tasks") or os.environ.get("DEEPSWE_TASKS")
    if explicit:
        return pathlib.Path(explicit).resolve()
    dst = CACHE / "deep-swe"
    if not (dst / "tasks").is_dir():
        CACHE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", CORPUS_URL, str(dst)],
                       check=True, timeout=600)
    return dst / "tasks"


def load(n: int, **kw) -> list[dict]:
    """Deterministic task list. `language=` (default python), `ids=a,b`."""
    root = _corpus_root(kw)
    language = kw.get("language", "python")
    ids = {s.strip() for s in kw["ids"].split(",") if s.strip()} if kw.get("ids") else None
    corpus_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                   text=True).stdout.strip() or None
    tasks = []
    for d in sorted(root.iterdir()):
        toml = d / "task.toml"
        if not toml.is_file():
            continue
        text = toml.read_text(encoding="utf-8")
        lang = _toml_str(text, "language")
        if language != "all" and lang != language:
            continue
        tid = _toml_str(text, "task_id") or d.name
        if ids is not None and tid not in ids:
            continue
        tasks.append({
            "id": tid,
            "dir": str(d),
            "language": lang,
            "repo_url": _toml_str(text, "repository_url"),
            "base_commit": _toml_str(text, "base_commit_hash"),
            "verifier_timeout": _toml_num(text, "verifier", "timeout_sec", 1800.0),
            "build_timeout": _toml_num(text, "environment", "build_timeout_sec", 1800.0),
            "corpus_commit": corpus_commit,
        })
    if ids is not None:
        missing = ids - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"deepswe: unknown task ids {sorted(missing)}")
    return tasks[:n] if n else tasks


# ------------------------------------------------------------------ checkout

def _run(argv: list[str], cwd: pathlib.Path | None = None, check: bool = True,
         env: dict | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                          check=check, timeout=timeout)


def _mirror(repo_url: str, base_commit: str) -> pathlib.Path:
    """One bare mirror per upstream repo, shared by every run and arm."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_url.rstrip("/").removesuffix(".git").split("github.com/")[-1])
    mirrors = CACHE / "mirrors"
    mirrors.mkdir(parents=True, exist_ok=True)
    dst = mirrors / f"{name}.git"
    with open(mirrors / f"{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not dst.is_dir():
            _run(["git", "clone", "-q", "--mirror", repo_url, str(dst)], timeout=3600)
        have = subprocess.run(["git", "cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=dst,
                              capture_output=True).returncode == 0
        if not have:
            _run(["git", "fetch", "-q", "--prune", "origin"], cwd=dst, timeout=3600)
    return dst


def _materialize(task: dict, workdir: pathlib.Path) -> str:
    """The image's git time-travel: default branch AT base, no future history."""
    mirror = _mirror(task["repo_url"], task["base_commit"])
    head = _run(["git", "symbolic-ref", "HEAD"], cwd=mirror).stdout.strip()
    default = head.rsplit("/", 1)[-1] or "main"
    if workdir.exists():
        shutil.rmtree(workdir)
    _run(["git", "clone", "-q", "--no-checkout", str(mirror), str(workdir)], timeout=1800)
    _run(["git", "checkout", "-q", "-B", default, task["base_commit"]], cwd=workdir)
    _run(["git", "remote", "remove", "origin"], cwd=workdir)
    branches = _run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                    cwd=workdir).stdout.split()
    for b in branches:
        if b != default:
            _run(["git", "branch", "-D", b], cwd=workdir, check=False)
    for t in _run(["git", "tag"], cwd=workdir).stdout.split():
        anc = subprocess.run(["git", "merge-base", "--is-ancestor", t, "HEAD"], cwd=workdir,
                             capture_output=True).returncode == 0
        if not anc:
            _run(["git", "tag", "-d", t], cwd=workdir, check=False)
    _run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=workdir)
    _run(["git", "gc", "-q", "--prune=now"], cwd=workdir, timeout=1800)
    _run(["git", "submodule", "update", "--init", "--recursive"], cwd=workdir, check=False,
         timeout=1800)
    for k, v in (("user.email", "agent@local"), ("user.name", "agent"),
                 ("core.hooksPath", "/dev/null"), ("commit.gpgsign", "false")):
        _run(["git", "config", k, v], cwd=workdir)
    exclude = workdir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("\n# agentbench harness state\n" + "\n".join(WORKSPACE_EXCLUDES) + "\n")
    return default


# --------------------------------------------------------------- environment

def _rewrite_paths(text: str, mapping: dict[str, str]) -> str:
    """Rewrite container-absolute paths. Longest source first; whole path
    components only, so `/app` never matches inside `/application` or inside
    an already-rewritten `<workdir>/tests`."""
    for src in sorted(mapping, key=len, reverse=True):
        text = re.sub(r"(?<![\w.\-])" + re.escape(src) + r"(?![\w\-])", mapping[src], text)
    return text


def _dockerfile_steps(dockerfile: pathlib.Path) -> tuple[dict[str, str], list[str]]:
    """(ENV/ARG assignments, RUN commands) in order, continuations joined."""
    lines, buf = [], ""
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        s = raw.rstrip()
        if not buf and (not s.strip() or s.lstrip().startswith("#")):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + "\n"
            continue
        lines.append(buf + s)
        buf = ""
    env: dict[str, str] = {}
    runs: list[str] = []
    for line in lines:
        m = re.match(r"^\s*(\w+)\s+(.*)$", line, re.DOTALL)
        if not m:
            continue
        ins, arg = m.group(1).upper(), m.group(2)
        if ins in ("ENV", "ARG"):
            if "=" in arg:
                for k, v in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\'[^\']*\'|\S*)', arg):
                    env[k] = v.strip("\"'")
            else:
                k, _, v = arg.partition(" ")
                env[k.strip()] = v.strip().strip("\"'")
        elif ins == "RUN":
            cmd = arg.strip()
            if any(skip in cmd for skip in _SKIP_RUN):
                continue
            runs.append(cmd)
    return env, runs


def _venv_paths(workdir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    base = workdir.parent / workdir.name  # sidecars sit NEXT TO the checkout, never inside it
    return (pathlib.Path(f"{base}.venv"), pathlib.Path(f"{base}.venv-pristine"),
            pathlib.Path(f"{base}.deepswe.json"))


def _build_env(task: dict, workdir: pathlib.Path) -> dict[str, str]:
    """Replay the image's install steps into a fresh venv; snapshot it."""
    venv, pristine, sidecar = _venv_paths(workdir)
    for p in (venv, pristine):
        if p.exists():
            shutil.rmtree(p)
    # The image family runs 3.12 (tasks carry `*_312` test modules that
    # validation flagged as red on 3.11). Override with DEEPSWE_PYTHON.
    _run(["uv", "venv", "-q", "--seed", "--python", os.environ.get("DEEPSWE_PYTHON", "3.12"),
          str(venv)], timeout=600)

    mapping = {"/app": str(workdir)}
    denv, runs = _dockerfile_steps(pathlib.Path(task["dir"]) / "environment" / "Dockerfile")
    session = {
        "VIRTUAL_ENV": str(venv),
        "PATH": f"{venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "SKIP_WRITE_GIT_CHANGELOG": "1",
        "SKIP_GENERATE_AUTHORS": "1",
    }
    for k, v in denv.items():
        v = _rewrite_paths(v, mapping)
        session[k] = os.path.expandvars(v) if "$" not in v or k != "PATH" else \
            v.replace("$PATH", session["PATH"]).replace("${PATH}", session["PATH"])
    if any("poetry" in cmd for cmd in runs):
        _run([str(venv / "bin" / "pip"), "install", "-q", "poetry"], timeout=900)

    env = {**os.environ, **session}
    log = []
    for cmd in runs:
        cmd = _rewrite_paths(cmd, mapping)
        proc = subprocess.run(["bash", "-o", "pipefail", "-c", cmd], cwd=workdir, env=env,
                              capture_output=True, text=True, timeout=task["build_timeout"])
        log.append(f"$ {cmd}\n{proc.stdout}{proc.stderr}\n[rc={proc.returncode}]\n")
        if proc.returncode != 0:
            pathlib.Path(f"{sidecar}.build.log").write_text("".join(log), encoding="utf-8")
            raise RuntimeError(f"{task['id']}: environment step failed (rc={proc.returncode}): "
                               f"{cmd[:120]!r} -- see {sidecar}.build.log")
    pathlib.Path(f"{sidecar}.build.log").write_text("".join(log), encoding="utf-8")

    # The image asserts a porcelain-clean tree after install; a dirty tree
    # here means the install wrote into the checkout and model.patch would
    # carry it. Record rather than fail: grader resets are per-file anyway.
    dirty = _run(["git", "status", "--porcelain"], cwd=workdir).stdout.strip()
    shutil.copytree(venv, pristine, symlinks=True)
    sidecar.write_text(json.dumps({"env": session, "dirty_after_build": dirty}, indent=1),
                       encoding="utf-8")
    return session


def session_env(task: dict, workdir: pathlib.Path) -> dict[str, str]:
    """Environment the harness merges into the agent subprocess."""
    _, _, sidecar = _venv_paths(workdir)
    return json.loads(sidecar.read_text(encoding="utf-8"))["env"]


# ----------------------------------------------------------- adapter contract

def prepare(task: dict, workdir: pathlib.Path) -> str:
    _materialize(task, workdir)
    _build_env(task, workdir)
    # The tree shape both arms see: ctx.toml is present (excluded from git).
    (workdir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return (pathlib.Path(task["dir"]) / "instruction.md").read_text(encoding="utf-8")


def apply_gold(task: dict, workdir: pathlib.Path) -> None:
    """solution/solve.sh, verbatim semantics: apply, branch, commit. validate.py only."""
    patch = pathlib.Path(task["dir"]) / "solution" / "solution.patch"
    _run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=workdir)
    _commit_all(workdir, "feature/solution", "Apply reference solution")


def _commit_all(workdir: pathlib.Path, branch: str, message: str) -> None:
    _run(["git", "checkout", "-q", "-b", branch], cwd=workdir, check=False)
    _run(["git", "add", "-A"], cwd=workdir)
    _run(["git", "-c", "user.name=oracle", "-c", "user.email=oracle@local", "commit", "-q",
          "--no-verify", "--allow-empty", "-m", message], cwd=workdir)


def _patch_paths(text: str) -> list[str]:
    seen, out = set(), []
    for line in text.splitlines():
        m = re.match(r'^diff --git (?:"?a/(.*?)"?) (?:"?b/(.*?)"?)$', line)
        path = m.group(2) if m else (line[6:] if line.startswith("+++ b/") else None)
        if path and path != "/dev/null" and path not in seen:
            seen.add(path)
            out.append(path)
    return out


# Referee controls for validate.py. DeepSWE's grader discards the agent's test
# edits by construction, so SWE-bench's "gold + tampered tests must not
# resolve" is the wrong control here: the cheat that matters is "tests made
# to pass WITHOUT a fix", and the execution check is "gold with the touched
# source vandalised" -- if that resolves, nothing is being run.
CONTROL_STATES = {"baseline": False, "gold": True, "tampered": False, "vandal": False}


def control(task: dict, workdir: pathlib.Path, state: str) -> None:
    tests_dir = pathlib.Path(task["dir"]) / "tests"
    if state == "baseline":
        return
    if state == "gold":
        apply_gold(task, workdir)
        return
    if state == "tampered":
        # No fix. Every held-out test file replaced by one that trivially
        # passes, committed like a submission. Reward must stay 0.
        for rel in _patch_paths((tests_dir / "test.patch").read_text(encoding="utf-8")):
            p = workdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("def test_trivially_true():\n    assert True\n", encoding="utf-8")
        _commit_all(workdir, "feature/cheat", "Rewrite held-out tests")
        return
    if state == "vandal":
        apply_gold(task, workdir)
        sol = (pathlib.Path(task["dir"]) / "solution" / "solution.patch").read_text(encoding="utf-8")
        for rel in _patch_paths(sol):
            p = workdir / rel
            if p.is_file() and p.suffix == ".py":
                p.write_text("raise RuntimeError('vandalised')\n", encoding="utf-8")
        _commit_all(workdir, "feature/vandal", "Vandalise solution files")
        return
    raise ValueError(state)


def _verify(task: dict, workdir: pathlib.Path, patch: bytes, label: str) -> tuple[dict | None, int]:
    """Apply `patch` to a pristine checkout at `workdir` (the venv's editable
    install points there) with the venv restored from its pre-session
    snapshot, then run the task's own test.sh/grader.py. Returns
    (reward.json contents or None, test.sh return code). Each call starts
    from pristine state, so it can run more than once per session."""
    venv, pristine, sidecar = _venv_paths(workdir)
    session = json.loads(sidecar.read_text(encoding="utf-8"))["env"]
    tests_src = pathlib.Path(task["dir"]) / "tests"

    _materialize(task, workdir)
    if venv.exists():
        shutil.rmtree(venv)
    shutil.copytree(pristine, venv, symlinks=True)

    vdir = pathlib.Path(f"{workdir}.verifier-{label}")
    if vdir.exists():
        shutil.rmtree(vdir)
    tests_dst = vdir / "tests"
    shutil.copytree(tests_src, tests_dst)
    ver = vdir / "logs" / "verifier"
    art = vdir / "logs" / "artifacts"
    ver.mkdir(parents=True)
    art.mkdir(parents=True)
    (art / "model.patch").write_bytes(patch)

    mapping = {"/logs/verifier": str(ver), "/logs/artifacts": str(art),
               "/tests": str(tests_dst), "/app": str(workdir)}
    for name in ("test.sh", "config.json"):
        p = tests_dst / name
        p.write_text(_rewrite_paths(p.read_text(encoding="utf-8"), mapping), encoding="utf-8")

    env = {**os.environ, **session, "TESTS_DIR": str(tests_dst), "VERIFIER_DIR": str(ver),
           "APP_DIR": str(workdir), "ARTIFACTS_DIR": str(art)}
    try:
        proc = subprocess.run(["bash", str(tests_dst / "test.sh")], cwd=workdir, env=env,
                              capture_output=True, text=True, errors="replace",
                              timeout=task["verifier_timeout"])
        out, rc = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        out, rc = f"{exc.stdout or ''}{exc.stderr or ''}\n[verifier timeout]", -1
    (ver / "test-stdout.txt").write_text(out, encoding="utf-8")

    reward_file = ver / "reward.json"
    reward = json.loads(reward_file.read_text(encoding="utf-8")) if reward_file.is_file() else None
    return reward, rc


def _score(reward: dict | None) -> dict:
    return {
        "resolved": bool(reward and reward.get("reward") == 1),
        "f2p": f"{reward['f2p_passed']}/{reward['f2p_total']}" if reward else "n/a",
        "p2p": f"{reward['p2p_passed']}/{reward['p2p_total']}" if reward else "n/a",
        "partial": round(float(reward.get("partial", 0.0)), 4) if reward else None,
        "apply_failed": bool(reward and reward.get("apply_failed")),
        "verifier_error": reward is None,
    }


def _worktree_patch(agent_dir: pathlib.Path, base: str) -> bytes:
    """Everything the agent left in its tree, committed or not (untracked
    files included; harness state stays out via .git/info/exclude)."""
    _run(["git", "add", "-A"], cwd=agent_dir, check=False)
    diff = subprocess.run(["git", "diff", "--binary", "--cached", base], cwd=agent_dir,
                          capture_output=True)
    return diff.stdout if diff.returncode == 0 else b""


def grade(task: dict, workdir: pathlib.Path, **kw) -> dict:
    """Official score: the committed patch ([[verifier.collect]] semantics).
    Diagnostic: when the session left uncommitted work, the whole working
    tree is graded too under `worktree_*`, so a run that ran out of turns
    before `git commit` still shows how far it got. `resolved` is never
    taken from the diagnostic."""
    # [[verifier.collect]]: only committed work leaves the agent environment.
    diff = subprocess.run(["git", "diff", "--binary", task["base_commit"], "HEAD"], cwd=workdir,
                          capture_output=True)
    patch = diff.stdout if diff.returncode == 0 else b""
    uncommitted = bool(_run(["git", "status", "--porcelain"], cwd=workdir).stdout.strip())
    head_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workdir, check=False).stdout.strip()

    agent_dir = pathlib.Path(f"{workdir}.agent")
    if agent_dir.exists():
        shutil.rmtree(agent_dir)
    shutil.move(str(workdir), str(agent_dir))

    reward, rc = _verify(task, workdir, patch, "committed")
    tests_src = pathlib.Path(task["dir"]) / "tests"
    test_files = set(_patch_paths((tests_src / "test.patch").read_text(encoding="utf-8")))
    touched = set(_patch_paths(patch.decode("utf-8", errors="replace")))
    rec = {
        **_score(reward),
        "verifier_rc": rc,
        # Recorded only: the grader resets these files, so touching them
        # cannot change the score -- it is a cheating SIGNAL, not a penalty.
        "tests_tampered": bool(touched & test_files),
        "uncommitted_changes": uncommitted,
        "head_branch": head_branch,
        "patch_bytes": len(patch),
        "files_changed": len(touched),
    }
    if uncommitted and not kw.get("skip_worktree"):
        rec.update(grade_worktree(task, workdir, agent_dir))
    return rec


def grade_worktree(task: dict, workdir: pathlib.Path, agent_dir: pathlib.Path) -> dict:
    wpatch = _worktree_patch(agent_dir, task["base_commit"])
    wreward, _ = _verify(task, workdir, wpatch, "worktree")
    ws = _score(wreward)
    return {
        "worktree_resolved": ws["resolved"],
        "worktree_f2p": ws["f2p"],
        "worktree_p2p": ws["p2p"],
        "worktree_partial": ws["partial"],
        "worktree_apply_failed": ws["apply_failed"],
        "worktree_patch_bytes": len(wpatch),
        "worktree_files_changed": len(_patch_paths(wpatch.decode("utf-8", errors="replace"))),
    }


def _cli_regrade(argv: list[str]) -> int:
    """Back-fill `worktree_*` on a results payload whose `<tag>.agent`
    workspaces still exist: python adapters/deepswe.py regrade RESULTS.json"""
    import argparse
    ap = argparse.ArgumentParser(prog="deepswe.py regrade")
    ap.add_argument("results", type=pathlib.Path)
    ap.add_argument("--work-root", type=pathlib.Path, default=None,
                    help="defaults to the payload's recorded work_root")
    ap.add_argument("--adapter-arg", action="append", default=[])
    args = ap.parse_args(argv)
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    work_root = args.work_root or pathlib.Path(payload["work_root"])
    kw = dict(item.partition("=")[::2] for item in args.adapter_arg)
    kw["ids"] = ",".join(payload["task_ids"])
    tasks = {t["id"]: t for t in load(0, **kw)}
    for rec in payload["results"]:
        tag = f"{rec['task_id']}_{rec['arm']}_r{rec['repeat']}".replace("/", "_")
        workdir = work_root / tag
        agent_dir = pathlib.Path(f"{workdir}.agent")
        if not rec.get("uncommitted_changes") or not agent_dir.is_dir():
            continue
        rec.update(grade_worktree(tasks[rec["task_id"]], workdir, agent_dir))
        print(f"  {tag}: worktree f2p={rec['worktree_f2p']} p2p={rec['worktree_p2p']} "
              f"partial={rec['worktree_partial']}", flush=True)
        args.results.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "regrade":
        raise SystemExit(_cli_regrade(sys.argv[2:]))
    raise SystemExit("usage: deepswe.py regrade RESULTS.json [--work-root DIR]")
