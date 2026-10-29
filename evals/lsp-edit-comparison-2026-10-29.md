# LSP versus non-LSP complex edit replay

This is a controlled, model-free Python refactor, not a live agent trial. The
fixture has nine files. Six need changes through direct imports, a renamed import
alias, a re-export, qualified calls and multiline calls. Three contain same-name
locals, unrelated functions/methods, or strings/comments that must stay untouched.
Each arm previews the complete diff, applies a sealed ctx transaction and runs
12 behavioral assertions. No repair loops are charged to the non-LSP baseline.

The three arms are:

1. **Discovery/read/explicit edits:** real `ctx refs` with LSP disabled, bounded
   reads of the affected files, then an explicit replacement request. Jedi supplies
   the correct replacements so this scripted baseline incurs no reasoning mistakes.
2. **Jedi rename:** a benchmark adapter over Jedi's semantic rename API, converted
   to the same ctx plan. This is the stronger non-LSP baseline; it is not a new
   production ctx command.
3. **LSP rename:** the new ctx rename planner using the installed Pyright server.

All arms use identical builtin post-edit checks. The separate
[LSP edit-loop replay](lsp-edit-loop-2026-10-29.json) measures fresh LSP diagnostics.

## Initial result

Three repeats, rotating arm order. All nine runs passed. Median counts from the
[full trace receipt](lsp-edit-comparison-2026-10-29.json):

| Workflow | Tool-result tokens | Scripted argument tokens | Scripted calls |
| --- | ---: | ---: | ---: |
| Discovery/read/explicit edits | 4,295 | 864 | 14 |
| Jedi semantic rename | 3,492 | 275 | 7 |
| LSP semantic rename | 3,491 | 275 | 7 |

The semantic edit operation saves 804 result tokens, or 18.7%, against the
explicit-edit workflow. It also avoids 589 scripted argument tokens. LSP saves
effectively zero tokens against direct Jedi rename in this fixture: the one-token
receipt-handle difference is not a meaningful advantage. That distinguishes the
benefit of a semantic edit operation from the protocol that carries it.

The cold-workspace timing favored Jedi: approximately 0.12 seconds for
the direct adapter, 0.70 seconds for the explicit workflow and 1.13 seconds for
LSP. See the JSON for rerun timings; these include server startup and are not a
general latency ranking. The separate warm-session replay measures server reuse.

## Approximate cost, with explicit assumptions

If each saved result token is billed once as uncached model input:

| Hypothetical input price per million tokens | Saving per edit | Saving per 1,000 edits |
| --- | ---: | ---: |
| $1 | $0.000804 | $0.804 |
| $3 | $0.002412 | $2.412 |
| $10 | $0.008040 | $8.040 |

These are scenarios, not Argon or Flash price quotes. Actual provider cost,
reasoning, output tokens, cache discounts and repeated conversation history were
not measured. The tokenizer is `cl100k_base`, not Gemini's tokenizer. Scripted
tool calls are not model turns. The live agent cost/turn fields remain null.

## Reproduce and extend

The observed environment used Jedi 0.18.1, Pyright 1.1.408 and tiktoken 0.11.0.
Install a Python language server and the evaluation dependencies, then run:

```sh
python -m evals.lsp_edit_comparison --repeats 3 --output evals/lsp-edit-comparison-local.json
```

The receipt stores fixture and source hashes, all measured outputs, separate
argument/result token counts, failures and timing. A failed arm is reported;
its shorter trace must not be interpreted as an efficiency win. This first
fixture does not establish results for production bug fixes, other languages,
dynamic Python imports or other refactor kinds. Next evaluation should replay
held-out real edits and count downstream test success and recovery work.
