# Hot-path profile — the PreToolUse hook

**Date:** 2026-07-20 · **Host:** this container (CPython 3.11, cold FS caches warmed)
**Method:** 40× end-to-end subprocess spawns for wall-clock; 500× warm
in-process calls for the work-only figure; `cProfile` over 200 warm calls for
the attribution; `python -X importtime` for the import ledger.

The PreToolUse hook fires on **every** Bash/Read/Edit/Write the agent issues —
tens to low-hundreds of spawns per session — so its per-call latency is the
one that compounds. This receipt establishes where that latency actually lives.

## Headline: the work is already ~0.8 ms; the cost is starting Python

| Layer | Median | What it is |
|---|---|---|
| Bare `python -c pass` | 13.5 ms | CPython interpreter startup floor — unavoidable in-process |
| `+` importing `ctx.hook` and friends | ~27 ms cumulative | module import (typing, pathlib, json, re, ctx.\*) |
| `+` the actual hook work | **0.82 ms** | classify + reflex signature + ledger write |
| **End-to-end spawn** | **~38 ms** | what a host measures per intercepted call |

**~97% of per-call latency is process startup + import, not the harness's own
logic.** Any further micro-optimization of the ledger or digest is chasing the
remaining 3%.

## Attribution of the 0.82 ms of work (cProfile, per call)

```
classify                    ~1.35 ms cumulative  (shlex.split ×3, command_signature)
  reflex.check_command      ~0.56 ms
    _write_state            ~0.38 ms   (os.open + os.replace, atomic state)
    note_steer_shadow       ~0.36 ms
  _load_guard_policy        ~0.13 ms   (now JSON-cache hit; was ~5 ms via tomllib)
  engagement.note_call      ~0.15 ms
pathlib Path construction   ~0.24 ms   (~37 Path ops/call, pure overhead)
```

Two facts worth stating plainly:

1. **The digest is not in this hot path.** PreToolUse only *classifies and
   rewrites* the command (`pytest -q` → `ctx run -- pytest -q`). The digest is
   computed later, inside the `ctx run` subprocess the agent was going to spend
   anyway. "Make the digest cheaper" is a `ctx run` concern, not a per-tool-call
   latency concern.
2. **The ledger write is real but tiny** (~0.4 ms): one atomic
   `os.open`+`os.replace` of the reflex state plus a JSONL append. It is already
   the cheapest correct form (atomic, fail-open).

## Fixes applied in this pass (import ledger, the 27 ms bucket)

The hot path now imports **no `tomllib`, no `tempfile`, no `shutil`** on the
warm branch (confirmed via `importtime`; count of those three modules loaded
during one real hook call: **0**).

| Change | File | Saving |
|---|---|---|
| `tomllib` import made lazy; guard-policy parse cached as JSON in the ledger, keyed by `(mtime_ns, size)` of `ctx.toml` + `ctx-policy.toml` | `hook.py`, `config.py` | ~5 ms on the common no-change branch (TOML parsed only when a source file actually changes) |
| `tempfile.mkstemp` → hand-rolled `os.open(O_CREAT\|O_EXCL)` (drops the `shutil` transitive import) | `reflex.py` | ~4 ms (was pulled in for one atomic write) |

Wall-clock effect, same 40-spawn benchmark: **median 49.4 ms → 38.1 ms** on this
box. The cache is a pure derivation of the TOMLs (delete-safe; corruption and
stale-key both fall back to a fresh parse — 8 tests in
`tests/test_guard_policy_cache.py`).

## The real lever, and why it's already the right architecture

The only way to beat ~27 ms is **to not start Python.** That is exactly what
`native/ctx-hook-native` (Rust, ~2 ms) exists for — same JSON contract,
byte-parity test in `tests/test_native_hook.py`, used opportunistically when
present (`CTX_NATIVE_HOOK` / on PATH), Python remains canonical.

**Current coverage gap:** the shim implements only **post-tool-use** (the
emission governor). The **pre-tool-use classify path** — the one that fires most
often and does the command rewriting — is still Python-only. Porting classify to
the shim (or standing up a resident daemon the hook talks to over a unix socket)
is the ~15× lever; the ledger and digest micro-work is not. Tracked as debt, not
started here (it is a real scope change with its own parity surface).

## Remaining candidates (all small, recorded not chased)

- **Double reads within one call.** `_load_guard_policy` and reflex `read_state`
  are each invoked twice per hook call. Memoizing per-invocation saves ~0.2 ms.
  Low value, mild staleness risk — candidate only.
- **pathlib in the hot path.** ~37 `Path` constructions/call ≈ 0.24 ms of pure
  object churn; `os.path` string ops would remove it. Micro.
- Neither is worth touching before the Rust classify port, which dwarfs both.
