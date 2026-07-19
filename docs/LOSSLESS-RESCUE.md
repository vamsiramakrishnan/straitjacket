<sub><a href="README.md">straitjacket / docs</a></sub>

# Lossless mid-session rescue: taking the rewriting proxy's last edge, without its costs

**The gap.** All our gates are forward-looking: they prevent bloat at birth,
entry, and emission, but cannot un-spend a transcript that is already
large. A rewriting proxy (Headroom) can — that was its one structural
advantage left after the 2026-07-18 benchmarks (it held S4:haiku to 37%
window vs our 51%, measured at the wire). Its price, also measured: 12–16
point cache-hit deficit, 3–6× ongoing cache-creation, silent evidence
destruction (the quiet-needle drop), and a per-request latency tax.

**Hypotheses tested before building:**

- **H-A — one-shot beats per-request.** If elision happens as a frozen,
  deterministic *epoch* at a threshold crossing (instead of varying
  compression on every request), the cache is re-bought once at the smaller
  size and stays stable afterward. T2 simulation with measured prices and
  real S4 wire shapes (100k transcript, 27k elidable, 40 remaining
  requests): epoch rescue nets **+$0.05** on sonnet where per-request
  rewriting pays **$0.90** in churn — ~18× less overhead — and frees 13.5%
  of the window ≈ **18 turns of lossless runway** at the measured
  1.5k/request growth. Confirmed; this is the shipped design.
- **H-B — checkpoint-anchored host compaction** (nudge `ctx checkpoint`
  before the host's own lossy compaction): complementary, prompt-level,
  kept as skill doctrine rather than a mechanism.
- **H-C — rescue is rarely needed.** Real harnessed sessions peaked at 51%
  (S4) and 48% (overhaul round) — the gates do most of the work. Confirmed:
  rescue ships **disabled by default** (`--rescue-pct 0`), an explicit
  Tier-1 opt-in, so the Tier-0 observer's byte-exact invariant is untouched.

**The mechanism (`ctx.rescue`, v0.10.0):** when proxy-observed window
fullness crosses the opt-in threshold, one epoch freezes a deterministic
elision set — every tool_result older than the most recent 6 and larger
than 1 KiB, identified by ordinal. From then on, every request (the client
keeps resending the full original transcript; it is never aware) is
rewritten by the same pure function to a byte-identical prefix. Further
crossings latch further epochs; sets only grow; state survives restarts.

**No shortcuts, by construction:**

- *Nothing is destroyed*: every elided block's full bytes are persisted to
  `<state>/elided/<sha256>.txt` **before** the stub exists; the stub carries
  the hash, byte count, and retrieval path. (Contrast: the rewriting
  proxy's needle drop left no trace at any price.)
- *Determinism is the cache strategy*: property-tested — same transcript →
  byte-identical rewrite; grown transcript → byte-identical shared prefix.
- *Disclosure everywhere*: `rescued: N` on each wire record, a stderr
  banner on startup ("this mode is not byte-exact"), and the elided files
  themselves as the audit trail.
- *Fail-open*: any parse problem forwards the original body untouched.

**Live validation (real API, threshold forced to 5% so rescue fires on a
cheap session):** a 10-file read-and-report task under active rescue —
5 epochs latched, 5 tool_results elided mid-session, every elided file
intact on disk with its facts, `rescued: 1..5` disclosed on the wire, and
the model finished with **10/10 facts correct**, including facts whose
transcript blocks had been elided. Success under rescue, zero loss.

**Where this leaves the comparison:** the rewriting proxy's remaining
advantages are zero-integration generality (any client, no workspace) and
its memory features — both out of scope by principle, not capability. On
transcript economics there is no remaining regime where it wins on our
hosts: prevention (gates) for the common case, lossless epoch rescue for
the tail, both cache-stable.
