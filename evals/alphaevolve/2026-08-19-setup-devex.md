# AlphaEvolve setup DevEx integration receipt

**Date:** 2026-08-19  
**Scope:** repeat setup after one successful, doctor-verified Codex setup  
**Product seam:** `ctx setup` → `choose_setup` → receipt/fingerprint gate

## What changed

AlphaEvolve's setup-policy family evaluates six production-shaped choices:
ready no-op, detected-host setup, explicit setup, configure-all, managed repair,
and safe refusal for unmanaged conflicts. Completion, verification,
idempotency, user-config preservation, diagnostics, and explicit scope are hard
gates. Efficiency is scored only after those gates pass.

The reviewed integration adds:

- `ctx setup` as the short human front door (`ctx wrap setup` remains compatible);
- a privacy-safe `ctx.setup-receipt/v1` stored in ignored workspace bookkeeping;
- a SHA-256 fingerprint of managed setup files, installed version, selected
  hosts, and the one-way identity of the actual `ctx` executable on `PATH`
  (config contents and paths are not stored);
- a ready no-op only after a prior real doctor pass with the same fingerprint;
- automatic re-entry to idempotent repair and full verification on drift,
  upgrade, host change, failed setup, or `ctx setup --repair`.
- exact doctor validation that Codex has one MCP executable in `command` and
  the bounded server invocation in `args`; and
- refusal before any managed write when user-owned Codex TOML needs a reviewed
  MCP entry, preventing partial setup from being certified as ready.

## Paired local measurement

Command:

```bash
python -m evals.alphaevolve.setup_policy.benchmark
```

Eleven fresh temporary Git repositories were configured for Codex. Each pair
ran a forced full setup followed by the receipt-backed repeat path.

| Metric | Full verified repeat (naive baseline) | Receipt-backed repeat | Change |
|---|---:|---:|---:|
| Median latency | 12.897 ms | 2.917 ms | **4.42× faster / 77.38% lower** |
| Median visible output | 1,242 B | 152 B | **8.17× smaller / 87.76% lower** |
| Host-config rewrites on repeat | installer path runs | 0 | **eliminated** |
| Successful runs | 11/11 | 11/11 | parity |
| Fingerprint unchanged after no-op | — | 11/11 | invariant held |

These are local setup-path measurements, not a 100× product-wide claim. The
integration makes the next search measurable: package acquisition/cache time,
host probe scheduling, selective verification, and recovery-turn elimination.
The 100× aspiration is a composite end-to-end target and remains unproven.

## Safety and privacy closure

- A missing, malformed, failed, stale, version-mismatched, or changed receipt
  cannot take the no-op path.
- A failed doctor run records `success: false` and is retried next time.
- User configuration is hashed but never copied into the receipt.
- Replacing or changing the `ctx` executable invalidates the receipt.
- Broken or absent Codex MCP wiring fails doctor even when hooks are healthy.
- User-owned Codex TOML conflicts stop before hooks or instruction files change.
- `--repair` bypasses the receipt and re-runs setup plus the canonical checks.
- Existing unreadable-config refusal behavior remains in the installers.
