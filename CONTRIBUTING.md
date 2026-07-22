# Contributing to Straitjacket

Straitjacket accepts changes that preserve its core contract: potentially unbounded output becomes immutable evidence plus a bounded, deterministic, addressable view.

Read this page before changing a public command, evidence profile, host integration, specification, or evaluation claim.

## Development setup

Install the package and baseline development dependencies:

```bash
python -m pip install -e '.[dev]'
```

Install all optional Python engines when working on repository analysis or the full CI path:

```bash
python -m pip install -e '.[dev,map,fast,code,scip,sem]'
```

Optional system binaries such as ripgrep and Universal Ctags may improve speed or precision. They must not become mandatory for the core capture path.

## Run the test suite

```bash
python -m pytest tests/ -q
```

The suite is acceptance-oriented. It covers determinism, budgets, hook contracts, path and symlink escapes, redaction, engine fallbacks, and public behavior.

CI exercises both a rich environment and a minimal environment. A feature that works only when an optional binary or extra is installed is incomplete unless the specification explicitly requires that dependency.

## Core engineering rules

### 1. Capture before flood

Potentially unbounded output must be captured before it enters the model transcript. Post-hoc truncation is a safety net, not the preferred architecture.

### 2. Omission keeps an address

A model-visible result may omit evidence to stay within budget. It must declare the omission and retain a resolvable continuation address.

Silent truncation is a contract violation.

### 3. Rendering is deterministic

Model-visible bytes must not contain incidental variation such as:

- absolute host paths;
- timestamps or temporary paths without evidentiary value;
- locale-dependent output;
- ANSI control sequences;
- unstable ordering.

Identical evidence under the same contract and budget must produce identical output.

### 4. Optional engines have deterministic fallbacks

Optional engines may improve speed or precision. Their absence must produce a labeled fallback, not a broken command or an anonymous behavior change.

The active engine must be disclosed when it affects interpretation.

### 5. The hook hot path stays small

The pre-tool hook runs on every intercepted call. It must:

- remain standard-library only;
- avoid expensive initialization;
- emit exactly one valid host decision on every path;
- follow the configured internal-error policy;
- preserve host usability when optional instrumentation fails.

### 6. Safety does not adapt

Behavioral measurements may tune evidence delivery. They must not weaken hard path, process, storage, redaction, or quota constraints.

### 7. Claims require a referee

A mechanism does not ship because it is plausible or elegant. Define the workload, comparison, acceptance criteria, and failure conditions before treating the mechanism as product truth.

Record the result in `evals/`, including negative findings and regimes where the mechanism does not win.

## Specifications and acceptance

`spec/` is normative. The executable test suite is the acceptance gate.

A behavior change should include, as applicable:

1. a specification or schema change;
2. acceptance tests;
3. implementation;
4. CLI and guide updates;
5. a changelog entry;
6. an evaluation receipt when the change carries a performance or quality claim.

Do not merge a new public mechanism with implementation only.

## Documentation changes

Use [Documentation style](docs/DOCUMENTATION-STYLE.md).

A public CLI or behavior change is incomplete until the owning documentation changes in the same pull request.

Inspect at least:

| Change | Documentation owner |
|---|---|
| First-use or installation flow | `README.md`, `docs/GETTING-STARTED.md` |
| CLI syntax or semantics | `docs/CLI.md` |
| Recommended task workflow | `docs/USE-CASES.md` |
| Vocabulary or invariant | `docs/CONCEPTS.md`, `spec/` |
| Architecture rationale | Relevant document in `docs/` |
| Shipped behavior | `CHANGELOG.md` |
| Measured claim | `evals/` |

Do not duplicate volatile versions, test counts, or benchmark totals across entry-point pages. Link to their source of truth.

## Change workflow

1. Identify the system plane that owns the change: safety, execution, derivation, evidence, delivery, or behavior.
2. Define the contract and acceptance referee.
3. Add or update tests before relying on the mechanism.
4. Implement the smallest complete change.
5. Run the minimal and relevant optional-engine paths.
6. Update the specification, documentation, changelog, and evaluation evidence.
7. Review the diff for new model-visible nondeterminism, silent omission, and unbounded output.

## Engine disclosure

When several engines can produce the same logical result, disclose the selected engine and include it in relevant cache keys.

Examples include search, repository maps, symbol extraction, and reference resolution. Fallbacks should preserve the public output contract and coordinate semantics even when precision differs.

## Versioning

The project remains pre-1.0. Breaking changes are possible.

The package version in `pyproject.toml` is authoritative. Record shipped behavior in `CHANGELOG.md`. Avoid embedding the current version in multiple guide pages.

## Where decisions live

- [`spec/`](spec/) — normative contracts, schemas, ADRs, and acceptance requirements.
- [`tests/`](tests/) — executable acceptance behavior.
- [`docs/`](docs/) — guides, explanations, and architecture rationale.
- [`evals/`](evals/) — measured claims, fixtures, referees, and negative results.
- [`ROADMAP.md`](ROADMAP.md) — designed work that has not shipped.
- [`CHANGELOG.md`](CHANGELOG.md) — release history and current shipped behavior.

---

[Documentation](docs/README.md) · [Documentation style](docs/DOCUMENTATION-STYLE.md) · [Specifications](spec/) · [Evaluation receipts](evals/)
