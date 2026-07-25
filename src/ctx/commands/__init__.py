"""Command implementations for the `ctx` CLI, one module per verb family.

cli.py owns the front door, argument parsing and the `_COMMANDS` dispatch
table; this package owns the bodies. Two rules keep the harness's startup
budget intact, and both are load-bearing:

1. This __init__ imports nothing. `ctx.commands.<module>` is imported by
   name, on demand, for the single command being invoked — importing the
   families here would undo that.
2. Every module in this package keeps its dependencies inside the function
   that needs them, exactly as the old inlined if/elif chain did. A module
   groups several commands, so a module-scope `from ctx.jobs import …`
   would make `ctx jobs` pay for `ctx run`'s imports.

The user-facing one-liner for each command lives in `ctx.cliux`, not here.
"""
