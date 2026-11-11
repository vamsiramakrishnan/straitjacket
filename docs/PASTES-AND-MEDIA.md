<sub><a href="README.md">« straitjacket / docs</a></sub>

# Pastes, large files, images and video

Command output reaches ctx because ctx ran the command or a hook caught the
result. Three other things reach a model with no command to hang a bound on: a
large block pasted into the prompt, a large file the agent opens, and an image or
video. This page says what happens to each from the user's side, on Claude Code,
Codex and Antigravity, and where the limits are.

Terms used here. A **handle** is a retrieval address for stored bytes, such as
`run:93c664557458#stdout`; `ctx get HANDLE --lines A:B` returns a bounded region of
it. A **digest** is the small, deterministic summary ctx returns in place of the
bytes. **Intake** is storing text that no command produced; see
[`ctx intake`](CLI.md#store-a-paste-file-or-clipboard-ctx-intake). If these are new,
read [How it works](HOW-IT-WORKS.md) first.

## What happens to each

| You or the agent | Claude Code | Codex | Antigravity |
|---|---|---|---|
| **You paste a large block into the prompt** | The prompt gate stores it. `advise` (default) adds handles as context; the paste still reaches the model this turn. `block` stops the prompt and tells you what to resend. | Same behaviour, taken from Codex's documentation; not run live. | No prompt hook. The paste reaches the model whole. Run `ctx intake` yourself first. |
| **The agent reads a large text file** | `Read` is rewritten to the first 240 lines, or denied with an outline, with a `ctx get` hint. | The same route in the governance table (documented; not run live). | `read_file` is bounded by rewriting its line range; `view_file` is denied with the command named. |
| **The agent looks at an image or video** | The agent runs `ctx view`. `Read` refuses binary extensions before any hook runs. A result the host declares as an image or document is left untouched. | The agent runs `ctx view`. The host's own image tool is not governed. | The agent runs `ctx view`. Image generation is not governed. |

Nothing here is automatic for media: ctx has no hook that turns an image or video
into an overview. The agent, or you, runs `ctx view`.

## A large paste

### What the gate does

The prompt gate is a `UserPromptSubmit` hook, registered by `ctx setup` for Claude
Code and Codex. It is controlled by [`[intake]`](CONFIGURATION.md#intake-the-prompt-gate-for-oversized-pastes)
in `ctx.toml`.

A prompt hook can do two things and no more: add context next to the prompt, or
block the prompt before any model call. **It cannot remove the paste from a prompt
that has already been sent.** That is why the gate has two honest modes:

- **`advise` (the default)** stores the paste once and adds a digest with its
  handle as context. The context says so plainly: "The paste is STILL IN THE
  PROMPT: nothing was removed or shortened this turn." What it buys is that the model
  can cite a region and re-read it by handle instead of scrolling back, and that the
  handle survives compaction of the conversation. It saves no tokens this turn, and
  records none.
- **`block`** stores the paste, stops the prompt before any model call, and shows
  you the handles. No model call is made and nothing is spent. You send your
  instruction again with a handle where the paste was. This is the only way the
  paste stays out of context entirely.
- **`off`** does nothing and stores nothing.

What the model sees in `advise` mode, trimmed (every digest line is prefixed `| `
and sits inside a fence whose token is derived from the digest, so pasted text
cannot imitate the gate's framing or a handle):

```text
[ctx prompt-gate: advise] 1 large paste in the user's prompt was stored once by ctx.
The paste is STILL IN THE PROMPT: nothing was removed or shortened this turn. …
paste 1/1 · handle run:93c664557458#stdout · 1,201 lines · 91.5 KiB · ~23,433 tokens · digest logtemplate/v1
---- digest [ctx-fence:9ec1ab6185] ----
| [ctx run:93c664557458 profile=logtemplate/v1]
| intake: paste · 1,201 lines · 91.5 KiB · est 23,433 tokens if inlined
| templates: 2 cover 1,201/1,201 lines
| …
---- end digest [ctx-fence:9ec1ab6185] ----
```

What you see in `block` mode, trimmed:

```text
ctx stopped this prompt before the model saw it (no model call was made, nothing was spent) because it carries a large paste. The paste is stored, not lost.

Stored verbatim and unredacted on this machine only (user-state backend), … (kept 30 days, then eligible for `ctx gc`):
  1. run:93c664557458#stdout · 1,201 lines · 91.5 KiB · ~23,433 tokens

To continue, send your instruction again with the handle(s) above in place of the paste(s), for example:
  <your instruction> [ctx stored paste: run:93c664557458#stdout · 1,201 lines · 91.5 KiB]
```

The messages are built from handles, sizes and counts. They never repeat any text
of the prompt, because paste content can contain marker lines and typed text cannot
be told apart from it with confidence. The one other thing the Claude Code block
message shows you is where the paste is stored on this machine.

### What counts as oversized

A pasted region, or a prompt with no separable paste, larger than the threshold is
stored. The threshold is `[intake] threshold_bytes`; `0` (the default) means
`[budgets] max_inline_bytes`, which is 16,384 bytes. A configured value below 4,096
is raised to 4,096, so a short prompt costs nothing: the hook returns before it
reads any configuration.

- Claude Code's documentation says a collapsed paste arrives expanded between a line
  `<pasted_content id="ID">` and a line `</pasted_content id="ID">`. ctx uses those
  markers to find the paste. This is from the documentation, not observed: the
  non-interactive mode cannot produce it. A marker with no matching partner, a
  stray closer, or an id used twice is treated as ordinary text, and then no part of
  the prompt is presented as typed by you.
- A prompt with many pastes each under the threshold, but large in sum, is stored as
  one text.
- At most 16 regions are stored per prompt. If more are oversized, `block` is
  abandoned and `advise` says how many were not stored.
- A region that already starts with a ctx header and is at most 24 KiB is a digest
  pasted back, and is left alone.

### Host limits

- **Claude Code** (observed on 2.1.322). Added context over 10,000 characters is
  delivered as a file path plus a preview of about 2 KB, so ctx fits its context to
  9,000 bytes: digests that do not fit are shortened to a handle and a pointer to
  `ctx intake --chunks`. A block makes no model call.
- **Codex CLI** (from its documentation, version 0.167.1; not run here because the
  API quota was exhausted). `UserPromptSubmit` can block or add context. Added
  context is capped near 2,500 tokens, so ctx fits to an estimated 1,800 tokens
  using a pessimistic estimate. Hooks that are not managed need the user to trust
  the project first. The block message leaves out the local path.
- **Antigravity.** No prompt hook is registered. `ctx coverage --host antigravity`
  says so. Use `ctx intake` before you send the prompt: `ctx intake --clip --copy`
  stores the clipboard and puts a digest on it, ready to paste instead.

### Failures let the prompt through

The gate never makes a prompt fail. An internal error, a deadline of six seconds, an
unusable state directory or a handle that does not resolve sends the prompt through
unchanged. `block` happens only after every paste is stored and every handle
resolves from the session's own directory, including a subdirectory, a git worktree
or a submodule. A `ctx.toml` that cannot be read with confidence, or a mode that is
not exactly `advise`, `block` or `off`, means `off`.

Stored prompt text is verbatim and unredacted, in the ctx state directory with
private modes, for `[store] retention_days`. Nothing is stored in the repository.

## Pasted traces, tables and diffs

When a large paste is mostly one of three shapes, intake renders a view built for
that shape instead of the generic chunk map. These profiles apply to intake only; the
digest of a command's output does not change. A payload small enough to show whole
(about 3,600 bytes with the default budgets) is shown whole. A shape is claimed
only when it dominates the paste, so a long log with one trace in it is not a trace.

**Stack trace (`trace/v1`).** Python (including chained exceptions), JavaScript and
TypeScript, the Java family, Go and Rust. It shows the final exception, the causal
chain, the innermost frames, repeated frame cycles collapsed and counted, a file
census, and the deepest frame that looks like your own code, labelled as a heuristic.
Suppressed exceptions are siblings, not causes. Colour codes are removed before
parsing.

```text
[ctx run:22330f3a9476 profile=trace/v1]
intake: paste · trace.txt · 2,405 lines · 79.3 KiB · est 20,298 tokens if inlined · stack trace (python)
exception: "RecursionError: maximum recursion depth exceeded" · L2405
innermost frames, the exception raised last (innermost first; 2,197 printed):
  #1-#2196 ×600 cycle of 2 frames: "/app/tree.py":35 in "step" ; "/app/tree.py":31 in "walk" · L2402
  #2197 "/app/server.py":88 in "handle" · L2
your code (heuristic, a guess; verify): "/app/tree.py":35 in "step" · L2402 — innermost frame whose path is not under site-packages / node_modules / stdlib / runtime markers
```

**Delimited table (`delimited/v1`).** Comma, tab, semicolon and pipe, detected from
the data, never from a file name: a delimiter is accepted when at least 95% of the
sampled rows have the same number of fields. Row and column counts are exact. Per
column it gives the kind, the empty share and the values. Statistics come from at most
10,000 rows chosen by a fixed rule, and the digest says when they are a sample. A
table is claimed on evidence (typed cells, a categorical column, or a header of a
different kind) and declined for code, SQL, Markdown tables and logs with a bracketed
time or level prefix. A leading preamble is skipped and named.

```text
delimited: comma · 5 columns (the common field count) · header row detected (heuristic) · 4,000 data rows (exact)
statistics: all 4,000 data rows
columns (5 of 5 analysed shown):
  2 "city" · text · empty 0% · 6 distinct: "Oslo" 17%, "Lima" 17%, "Perth" 17%
  3 "status" · text · empty 0% · 3 distinct: "paid" 50%, "pending" 25%, "refunded" 25%
  4 "amount" · float · empty 0% · range 0.76 .. 499.98
```

**Unified diff (`udiff/v1`).** Plain `---` / `+++` / `@@` diffs, including colourised
ones, such as a diff from an editor, `diff -u` or a patch email that has no `diff
--git` header. It gives files, added and removed line counts, and the largest changes,
with a span per file. Counting follows the `@@` header, so a removed line that reads
`-- x` is a deletion and not a file header; binary, mode-only and rename entries
count as files. The coverage line reports hunks whose line count disagrees with their
header.

```text
diff: 40 files · +120 -40 · 40 hunks (line counts follow the @@ headers; see coverage) · L1-L720
largest changes (by lines changed):
  ±6 (+5 -1) "src/mod4.py" · modified · 1 hunk · L55-L74 · span 7558689e5f
```

### What these views guarantee

Every string taken from the paste (an exception message, a cell, a file path) goes
through one function, which redacts the original, then cleans it (terminal escapes,
control, format and zero-width characters removed, whitespace collapsed), then
redacts the cleaned text again, then clips it, and prints it quoted on one line. The
guarantee, as the module states it: every substring that a shared redaction pattern
matches in the original field or in the cleaned field is replaced whole, and nothing
is clipped, stripped or joined before both readings have been redacted. Where the
storage window itself cut a field, the last 100 characters are not shown. No field is
withheld. Because strings are quoted on one line, paste content cannot start a line
or imitate the digest's structure, a `next:` command, a `[ctx …]` header or a span id.
The property is tested over generated inputs in `tests/test_shape_redaction_matrix.py`.

**Not guaranteed.** A secret deliberately disguised so that neither reading matches a
shared pattern is not detected, here or in any other digest. That includes a token
split by characters the cleaner removes and glued to the word before it, a token glued
to a preceding word with no separator, credentials inside a URL, look-alike
(full-width) letters, a token split by a combining mark, and a secret spread across
two fields. Redaction here is pattern-based, and a pattern is a limit.

The same hardening applies to the other profiles: payload text can no longer plant a
line that looks like a digest's own structure. JSON key names, JSONL field names and
level values, the PDF version bytes, and any carriage return, U+0085, U+2028 or U+2029
are escaped where they could start a line.

## A large file

On Claude Code and Codex, ctx watches the agent's read tool. On Antigravity it
watches `read_file` and `view_file`. A file above `[budgets] max_inline_bytes`
(16,384 bytes by default) is not delivered whole:

- Claude Code: `Read` is rewritten to the first `max_inline_lines` lines (240 by
  default), and the reason carries a size, an outline hint and the way to fetch the
  rest: `ctx get repo:<path> --lines A:B` or `ctx search repo:<path> '<pattern>'`.
  When ctx can build a symbol outline for the file, a whole-file read is denied with
  the outline as the answer.
- Antigravity (observed on `agy` 1.5.5): `read_file` is bounded by overwriting its
  `StartLine` and `EndLine`, keeping the requested start and never passing the
  requested end, bounded by bytes as well as lines. `view_file` is denied with a
  range that fits.
- A cumulative session read budget (`session_read_budget_bytes`, 256 KiB) tightens
  later reads.
- Secret-bearing paths and paths outside the workspace always ask; they are never
  rewritten.

To store a file once and get a map of it, run
[`ctx intake <path>`](CLI.md#store-a-paste-file-or-clipboard-ctx-intake) (or
`ctx intake - < file` for one outside the workspace). Intake parses at most 32 MiB
of a payload. The whole file is stored, and the rest is an **unmapped tail**: the
digest names it by byte range and prints the `ctx get --bytes A:B` command that
reaches it. Nothing past 32 MiB is mapped, profiled or searched by the profile, but
all of it is addressable by byte range.

A file that an `@`-reference puts in the context on Claude Code enters with no tool
call, so no hook sees it.

## Images and video

`ctx view TARGET` returns an overview that is enough to decide where to look, keeps
the original addressable, and crops or extracts from the **original** pixels or
frames when you ask for detail. The flags are in the
[CLI guide](CLI.md#look-at-an-image-or-video-ctx-view). The result is a digest, a
workspace-relative file to open under `.ctx-session-reads/media/`, and exact `next:`
commands. ctx does not put pixels into the model's context itself: the agent opens
the derived file with whatever image tool its host provides.

### Optional engines and what works without each

Pillow (the `image` extra: `pip install 'ctx-harness[image]'`) and ffmpeg (with
ffprobe, on `PATH`) are optional. A missing engine is a declared skip with exit
status `1`, never a traceback.

| | With Pillow | Without Pillow |
|---|---|---|
| **With ffmpeg** | Everything: image overview, grid, crops; video census, contact sheet, `--at`, `--range`, `--scenes`, `--subs`; animated images. | Video census, `--at`, `--scenes` and `--subs`. The contact sheet and `--range` stop with a skip, because tiling and labelling need Pillow. An image gets its census only. |
| **Without ffmpeg** | Every image operation, and animated images. A video gets container facts only (format, size, hash). | An image gets its census only: format, dimensions, size, hash and a raw token estimate, with the skip stated. A video gets container facts only. |

Without ffmpeg there is no video support at all: no frames, scenes or subtitles, and
no duration or codec facts, because ffprobe supplies them. Without Pillow there is
no image resizing, no overview, no grid, no crop and no contact sheet. What still works without Pillow is every video operation that
returns a single frame, scene list or subtitle track, validated structurally (a
start-of-image marker, a frame header with real dimensions, a start of scan and an
end-of-image marker) before it is written; a frame that fails the check is a declared
failure with exit status `1` and no file. `ctx intake` and `ctx image digest` need
neither engine for their typed census; `ctx image diff` needs Pillow.

SVG is markup and is never rasterized; read it as text. Audio gets a typed census and
is never transcribed. PDF gets its census.

### How ffmpeg is run

The files this processes are untrusted. The target is opened once, walking each path
component from the workspace root without following symlinks. The bytes are copied to
a private copy, and the type sniff, the recorded hash, Pillow, ffprobe and ffmpeg all
use that copy, so the recorded hash is the hash of what was decoded. On Linux the
copy is an unnamed temporary file held by a descriptor; elsewhere it is a named file
in a private directory. ffmpeg and ffprobe inherit it read-only.

Every ffmpeg and ffprobe call is pinned to the container the sniff established, with:

- a **format** allow-list of one demuxer (`-f` and `-format_whitelist`), chosen from
  the container ctx recognised: MP4/MOV/M4A, Matroska/WebM, WAV, AVI, MP3, FLAC and
  Ogg. Never auto-detect.
- a **protocol** allow-list of `file` only, so a container that references other
  files or URLs cannot make ffmpeg open them; for MOV, external data references are
  switched off (`-enable_drefs 0`).
- a **codec** allow-list (`-codec_whitelist`) and a pixel cap (`-max_pixels`),
  under a fixed input name in an empty directory, in its own process group.

What that excludes: playlists, concat scripts, image-sequence patterns and any other
container ffmpeg could be talked into reading; every network protocol; raw and bitmap
decoders (`rawvideo`, `bmp`, `v210` and similar), which turn arbitrary referenced
bytes into pixels; video codecs outside the list (H.264, HEVC, VP8, VP9, AV1, MPEG-4
and a few older families are on it; others are refused with the codec named); and
bitmap subtitle tracks (DVD, PGS, DVB), which would need OCR. Text that originates in
the media (tags, codec names, subtitle text) cannot forge digest structure; subtitle
text is shown in a delimited, budgeted block and is data, not instructions.

Limits, each checked before allocation: an image up to 64 MiB and 80 megapixels;
video and audio up to 4 GiB (decided from the first 64 KiB, so other types are held to
64 MiB and a file that grows during the copy is refused at the cap); video frames up
to 16,384 px on a side and 64 megapixels, at most 32:1 aspect ratio; and a timeout on
every subprocess (20 s to probe, 30 s per frame, 60 s for subtitles, 90 s for a
sheet, 120 s for scene detection).

An interrupted view leaves nothing behind. SIGTERM, SIGHUP and SIGINT kill the ffmpeg
process groups, remove working directories, restore the previous handlers and then end
by that signal; a signal that was being ignored when the view started is left ignored.
Stale private directories from a killed earlier run (same owner, private mode, older
than six hours, holding only ctx's own file names) are removed at start.

### Token figures are estimates

Every token figure in a `ctx view` digest is an estimate. Images use the
28-pixel-patch rule, `ceil(width / 28) x ceil(height / 28)`, after scaling to fit the
**standard** image tier (1,568 px on the long edge and at most 1,568 tokens). The
tier a given model uses is the model's, not ctx's: a model on a higher-resolution tier
prices the same picture differently, and ctx does not know which model will read it.
A video's raw cost is one frame per second at its own resolution, labelled
`video@1fps`. Anything else falls back to bytes divided by 4.

Because the API already scales a large image to a bounded cost, compression alone
saves little: a 4000 x 3000 photo is about 1,568 tokens raw and about 1,036 as a
1024-pixel overview. The saving is selection. A legible grid-cell crop of a
2560 x 1440 screenshot is about 342 tokens against 1,568, and a 10-second clip is one
contact sheet of about 1,120 tokens against about 2,990 raw. The figures in
this paragraph were produced by running `ctx view` on generated files, not measured on
any model's bill.

## Where each limit is stated

| Limit | Where |
|---|---|
| A paste cannot be removed from the prompt already sent | [A large paste](#a-large-paste), [Host capabilities](HOST-CAPABILITIES.md#what-ctx-can-do-at-each-point) |
| 32 MiB parse cap; the unmapped tail is addressed by bytes | [`ctx intake`](CLI.md#store-a-paste-file-or-clipboard-ctx-intake) |
| No video without ffmpeg; no image resizing or grids without Pillow | [Optional engines](#optional-engines-and-what-works-without-each) |
| Token figures are estimates for the standard image tier | [Token figures](#token-figures-are-estimates) |
| Redaction in the paste views is pattern-based | [What these views guarantee](#what-these-views-guarantee) |

---

[CLI guide](CLI.md) · [Configuration](CONFIGURATION.md) · [Host capabilities](HOST-CAPABILITIES.md) · [How it works](HOW-IT-WORKS.md)
