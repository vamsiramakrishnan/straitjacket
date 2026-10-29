# Local LSP language coverage

`python -m evals.lsp_languages --output evals/lsp-languages-local.json`
checks TypeScript, Go and Rust using real installed language servers and
compilers. It makes no model calls. These are small fixed fixtures, not a
measurement of autonomous coding success, turns, or provider cost.

Each fixture compiles and runs before editing, then introduces a compiler
error that the compiler must reject. The evaluator requests diagnostics,
restores the source, requests diagnostics again, and proposes a cross-file
rename. An unrelated same-name symbol/string must remain unchanged. Changing
bytes outside the renamed token must refuse the entire edit without writing
any file. Restoring the exact snapshot must allow preview and apply, followed
by successful compilation and execution.

The JSON receipt records fixture/source hashes, exact server/compiler versions,
commands, diagnostics and each gate. Missing tools, failed baseline execution,
false clean diagnostics, unsafe writes or failed final execution fail the run.

## Diagnostic scope

The default gate permits the explicit `unavailable_unversioned` diagnostic
status only when the server omits versions on both reports, the broken file
produces an error and the repaired file does not. This demonstrates observed
error detection and clearing, but cannot prove those reports correspond to the
current snapshot. The `--require-fresh-diagnostics` flag requires versioned or
pull diagnostics for every selected language and fails this case.

TypeScript language server 6.0.2 publishes diagnostics without versions. Its
rename and compiler/runtime checks can pass while fresh diagnostics remain
unverified. Do not report this as complete LSP diagnostic conformance.

Rust-analyzer splits native pull diagnostics and compiler push diagnostics.
ctx now preserves errors from matching-version push reports when reading a
full pull response. Fresh means associated with the current document snapshot;
it does not mean every asynchronous analyzer has finished. The explicit compiler
and executable checks remain required. Rust startup waits for the documented
`experimental/serverStatus` quiescent signal to avoid querying an unloaded
crate graph, with a bounded timeout and process cleanup.

Go's gopls can attach version zero to unopened rename targets. ctx opens those
targets and recomputes the rename once, then applies its existing strict version
checks. It refuses requests needing more than 64 open documents rather than
evicting documents that establish the edit versions. Zero is not a wildcard.

## Reproduction

Observed local tools:

- `npm install typescript@6.0.3 typescript-language-server@6.0.2`
- Go 1.27.3 and `go install golang.org/x/tools/gopls@v0.21.1`
- Rust 1.93.1 with `rustup component add --toolchain 1.93.1 rust-analyzer rust-src`

Put the selected executables on PATH. `--languages go rust` selects a subset;
the default is all three languages. No runtime dependencies are fetched by the
fixtures; the Rust executable uses `cargo run --offline`.

Mixed-language refactors remain bounded by the observation policy. For example,
a TypeScript rename returning an edit to an unobserved JavaScript file is
refused. This replay does not claim mixed-language or large-project coverage.
