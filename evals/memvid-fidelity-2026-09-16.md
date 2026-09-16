# Can memvid hold our evidence? (2026-09-16)

The idea was a portable evidence capsule: one file attached to a pull
request, which a reviewer opens offline, in which every `run:` and `blob:`
handle the PR body cites resolves to the exact bytes that produced it. Today
those handles die with the container that minted them.

[memvid](https://github.com/memvid/memvid) looked like the right container.
A single `.mv2` file, no server, an embedded write-ahead log, append-only
checksummed frames, a BM25 index with optional vectors, time travel by frame
or timestamp, and a `verify()` call. Everything a capsule needs except the
one property straitjacket cannot trade away.

**It does not return the bytes you put in.** A third-party mechanism here may
index, rank or carry evidence; it may be the source of truth only if a round
trip is the identity function. This one is not, and it does not say so.

```bash
pip install memvid-sdk
python evals/memvid_fidelity.py
```

## Result

`memvid-sdk` 2.0.160, local file, no API key, no network. Each case is one
`put(text=...)`, a `commit()`, then the content frames read back with
`blob()` and concatenated.

| case | in | pages | back | verdict |
|---|---:|---:|---:|---|
| a payload smaller than one page | 30 | 0 | 0 | **no byte path at all** |
| mid-size, ends without a newline | 5,000 | 5 | 5,000 | exact |
| log-shaped, one trailing newline | 18,890 | 16 | 18,889 | **trailing newline lost** |
| two trailing newlines | 18,891 | 16 | 18,889 | **both lost** |
| runs of blank lines in the body | 2,400 | 0 | 0 | **no byte path at all** |
| CRLF line endings | 20,890 | 16 | 18,889 | **every `\r` stripped** |
| trailing spaces on every line | 24,890 | 16 | 18,889 | **every trailing space stripped** |

One case in seven survives intact. The store normalizes per-line whitespace
and drops trailing newlines, so a diff, an indentation-significant fixture, a
log with aligned columns, or anything with Windows line endings comes back
altered. A payload below the page threshold produces no content frame, so
there is no address that reaches its bytes at all.

Two details make this worse than a documented lossy encoding:

- **`verify(deep=True)` passed every check in all seven cases**, including the
  ones that lost 6,001 bytes. Its integrity checks cover the index and frame
  structure, not whether the content equals what was stored. A capsule that
  reports itself healthy while handing back different bytes is the exact
  failure mode our addresses exist to prevent.
- **`find()` never returned a payload verbatim** (0 of 7). Its `text` is a
  ranked window sized for a model to read, which is the right thing for
  search and the wrong thing for an address.

There is also no listed way to enumerate a record's content pages: the
timeline reports the logical `put` as frame 0, whose blob is empty, while the
content sits in frames 1..N that have to be found by scanning ids.

## What this changes

The capsule is still worth building. memvid is not the store.

- **Bytes stay ours.** The authoritative copy goes into the capsule in an
  encoding with no line semantics to normalize, carrying its `sha256`, and
  every read verifies that hash before the bytes are handed to anyone. A
  mismatch is an error, not a warning.
- **memvid earns the index.** BM25 over the same content, the frame timeline,
  and time travel by frame are real and worth having. They rank; they do not
  answer.
- **The receipt travels with the file.** A capsule should be able to prove
  its own fidelity on open, which means the check above belongs in the reader,
  not only in this eval.

Nothing here is a claim about memvid's performance numbers, its cloud
features, or its fact-card extraction, none of which this script touches.
It answers one question, on one version, offline.

Machine record: `--out` writes the per-case JSON, including the sha of what
went in and what came back.
