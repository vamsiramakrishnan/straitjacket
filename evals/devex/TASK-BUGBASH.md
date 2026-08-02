# Task: bug bash

Find real defects in this repository. Prove each one.

You are **not** fixing anything. Do not modify `src/`, `tests/`, `scripts/`,
or any existing file. Your entire deliverable is evidence.

## Deliverable

Write `findings.json` at the repository root: a JSON **array**, one object
per defect.

```json
[
  {
    "id": "short-kebab-slug",
    "file": "src/ctx/example.py",
    "line": 123,
    "severity": "crash | wrong-output | data-loss | contract-violation | docs-drift",
    "claim": "One sentence stating what is wrong.",
    "repro": "python3 bugbash/repro_short_kebab_slug.py"
  }
]
```

## The reproduction rule — read this twice

`repro` is a shell command, run from the repository root, that **exits
non-zero on this tree because of the defect**, and would exit zero once the
defect is fixed. It is the only thing that makes a finding count.

- Put repro scripts in a new `bugbash/` directory. That directory and
  `findings.json` are the **only** paths you may create.
- A repro is executed against a **pristine checkout** of this repository —
  not your working tree. Only `findings.json` and `bugbash/` are copied
  across. A repro that depends on any other edit you made will not run.
- The repro must actually exercise the defect. A command that fails for an
  unrelated reason (missing file, syntax error, bare `exit 1`) is discarded,
  and so is the finding.
- Unverifiable claims score **zero**. A finding you cannot reproduce is
  worse than one you never made — do not pad the list.

## What counts

Real defects, in rough priority order: crashes, wrong output, data loss,
violated contracts (a documented promise the code does not keep), and
documentation that contradicts the code.

Style, naming, formatting, and "this could be refactored" are **not**
defects and will not be counted.

## Constraints

- Run this repository's CLI as `python3 -m ctx ...` from the repository
  root. The `ctx` on PATH is a **different install**.
- Work only inside this repository checkout.
- Quality over quantity. Ten confirmed defects beat forty claims.
