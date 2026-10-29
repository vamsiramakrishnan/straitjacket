# Language servers and safe edits

ctx uses installed language servers for definitions, references, hover, diagnostics,
rename and code actions. No model or GPU service is called. `ctx refs` automatically
tries LSP after the SCIP index and before the Jedi/textual fallback. Direct
`ctx lsp` commands disclose missing servers and failures.

```sh
ctx lsp hover src/example.py:4:7
ctx lsp refs repo:src/example.py:Example.method
ctx lsp diagnostics src/example.py:1:1
```

Positions use one-based lines and UTF-16 columns. The server registry covers
Python, TypeScript/JavaScript, Go, Rust, C/C++, C#, Java, Kotlin, Lua, PHP, Ruby,
Scala, shell and Swift. Install the corresponding server on PATH. Registry
coverage is not certification of every server. Local replay covers Pyright and
the TypeScript, Go and Rust combinations listed below.
`CTX_LSP_SERVERS` accepts a JSON map from language IDs to candidate command arrays.

## Reuse and freshness

On POSIX, separate ctx invocations use a private, user-owned Unix socket to reuse
one server per workspace and configuration. Requests are serialized. Changed
open files receive versioned `didChange` messages; project file changes produce
watch notifications and invalidate cached diagnostics. Processes expire after
120 idle seconds. `CTX_LSP_IDLE_S` sets an idle period capped at 600 seconds.
`CTX_LSP_REUSE=off` uses one process per call. Non-POSIX systems use that path too.

The broker caps documents, file sizes, messages, and the watched file set. A
server failure discards the session; the next request starts a fresh one. Reuse
does not cache semantic answers. Server configuration/environment and ctx source
changes select a different session. The socket never accepts executable commands.

Diagnostics count as fresh only when the server reports the current document
version, or returns a full pull-diagnostic response for the synchronized file.
Receipts also check the file digest before and after diagnostics. Unversioned or
missing push reports cannot establish a clean result. `ctx lsp diagnostics`
returns exit 3 for issues, stale reports or unavailable diagnostics.

A full pull response can cover only the server's native checks. Matching-version
push diagnostics are included when available, but an empty response does not
replace a project compiler or test run. TypeScript language server 6.0.2 sends
unversioned diagnostics; ctx reports their freshness as unavailable even when
the displayed message is useful.

After a ctx-owned edit, available LSP checks join the existing builtin syntax
checks. The transaction shares an eight-second scheduling allowance, with short
per-request deadlines; an unavailable check is recorded, never called a passing
LSP check. Inspect the per-provider receipt: an aggregate clean result can reflect
the builtin check while another provider is unavailable. Diagnostics describe
edited files, not a complete project build. Behavioral verification remains
separate with `ctx edit verify`.

## Rename and code actions

```sh
ctx lsp rename src/example.py:4:7 --new-name better_name --out rename-plan.json
ctx edit preview rename-plan.json
ctx edit apply rename-plan.json

ctx lsp actions src/example.py:4:7
ctx lsp actions src/example.py:4:7 --action-index 0 --out action-plan.json
ctx edit preview action-plan.json
ctx edit apply action-plan.json
```

Planning does not edit source. Code-action indices refer to the response obtained
while planning, so inspect the preview before applying. Commands, unresolved
actions, annotations and file create/delete/rename operations are refused. Symbol
rename edits the contents of existing files. Each target must have been observed
before the server request, remain inside the workspace and pass current exclusion
policy. Versioned edits must match the session's known version. UTF-16 ranges are
validated, overlapping edits are refused, and original CRLF bytes are preserved.

Semantic plans seal complete source-file hashes and refuse any changed target,
including an added import above the old text. They do not use span relocation.
The observation is limited to existing text files in the target's language, up to
4096 files and 32 MiB. Other-language edits are refused rather than guessed.
Multi-file apply uses the existing cooperative CAS transaction: editors that do
not participate in that protocol can still race a filesystem write.

## Evidence and remaining work

Run the model-free replay with an installed Python server:

```sh
python -m evals.lsp_edit_loop --output evals/lsp-edit-loop-local.json
python -m pytest tests/test_lsp.py tests/test_lsp_edit_loop.py -q
```

The replay compares three fresh-server lookups with three reused-server lookups,
checks equal answers, introduces an error, then performs and executes a cross-file
rename. It records latency, response bytes, process reuse and verification status.
It does not measure autonomous coding success, agent turns or model cost. Those
remain null rather than inferred from lookup speed. Task-separated agent replays
are required before policy promotion.

The [complex edit comparison](../evals/lsp-edit-comparison-2026-10-29.md) includes
both an explicit-edit workflow and direct Jedi rename as non-LSP baselines.
It separates measured tool-traffic tokens from hypothetical billing scenarios.

Run the additional language checks with installed servers and compilers:

```sh
python -m evals.lsp_languages --output evals/lsp-languages-local.json
```

The [local receipt](../evals/lsp-languages-2026-10-29.json) records versions,
fixture and implementation hashes, cross-file rename, unchanged unrelated names,
stale-plan rejection, an intentionally failing compiler check and restored
executable behavior. The pinned combinations are TypeScript 6.0.3 with
typescript-language-server 6.0.2, Go 1.27.3 with gopls 0.21.1, and Rust with
rust-analyzer 1.93.1. The installed-server CI workflow repeats these checks.

The default gate requires successful edits and compiler/runtime verification;
diagnostic freshness has a separate result. Add `--require-fresh-diagnostics`
to require that stronger gate, which the pinned TypeScript server cannot satisfy.
This verifies small fixed fixtures, not arbitrary projects or every refactoring.
