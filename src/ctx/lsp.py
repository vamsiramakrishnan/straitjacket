"""Language-server tier: the semantic questions a parser cannot answer.

The skeleton (tree-sitter) says *where* symbols are; a language server says
what a name at a position *means*: its definition across files, every
reference to it, its type and signature. This module is a small JSON-RPC
client over stdio for any server on PATH, used by ``ctx lsp`` and, as a
rung of the ``ctx refs`` ladder, above jedi and the textual fallback.

Design rules, all measured elsewhere in this repo:

- **Fail-open, disclosed.** No server for the language, a server that
  crashes, or one that times out returns ``None``/raises ``LspError``; the
  caller falls to the next rung and says so in the output header. The
  harness never blocks on a language server.
- **Bounded.** One server process per call, ``initialize`` → the one
  request → ``shutdown``; a request that outlasts ``timeout`` is a failure.
  Servers that index whole projects (pyright, rust-analyzer) can take a few
  seconds on first contact; the default budget allows that and no more.
- **Coordinates are the interchange.** Positions in are ``line:col``
  (1-based, as every other ctx verb prints them); results out are
  ``(repo-relative path, line, text)`` rows exactly like ``ctx refs``.
- **Declarative registry, overridable.** ``SERVERS`` maps a skeleton
  language to candidate argv lists; the first whose executable is on PATH
  wins. ``CTX_LSP_SERVERS`` (JSON, same shape) replaces the table, which is
  how tests run a fake server and how a user points at their own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ctx.bounds import explicit

DEFAULT_TIMEOUT_S = 20.0

#: language (as ctx.skeleton names it) -> candidate server commands.
SERVERS: dict[str, list[list[str]]] = {
    "python": [["pyright-langserver", "--stdio"], ["pylsp"], ["jedi-language-server"]],
    "typescript": [["typescript-language-server", "--stdio"]],
    "javascript": [["typescript-language-server", "--stdio"]],
    "go": [["gopls"]],
    "rust": [["rust-analyzer"]],
    "c": [["clangd"]],
    "c++": [["clangd"]],
    "c#": [["csharp-ls"], ["omnisharp", "-lsp"]],
    "java": [["jdtls"]],
    "kotlin": [["kotlin-language-server"]],
    "lua": [["lua-language-server"]],
    "php": [["intelephense", "--stdio"], ["phpactor", "language-server"]],
    "ruby": [["ruby-lsp"], ["solargraph", "stdio"]],
    "scala": [["metals"]],
    "shell": [["bash-language-server", "start"]],
    "swift": [["sourcekit-lsp"]],
}

_LANGUAGE_IDS = {
    "python": "python", "typescript": "typescript", "javascript": "javascript", "go": "go",
    "rust": "rust", "c": "c", "c++": "cpp", "c#": "csharp", "java": "java", "kotlin": "kotlin",
    "lua": "lua", "php": "php", "ruby": "ruby", "scala": "scala", "shell": "shellscript",
    "swift": "swift",
}


#: Settings pushed at start and served to `workspace/configuration`. Only
#: pyright reads these; every other server ignores unknown sections.
_SETTINGS: dict[str, Any] = {"python": {"analysis": {"diagnosticMode": "workspace"}}}


def _configuration_for(item: Any) -> Any:
    """Answer one `workspace/configuration` item from ``_SETTINGS`` by its
    dotted section (``python.analysis`` → the analysis dict), else null."""
    section = str((item or {}).get("section") or "") if isinstance(item, dict) else ""
    node: Any = _SETTINGS
    for part in [p for p in section.split(".") if p]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if section else None


DEFAULT_SETTLE_S = 3.0


def settle_seconds() -> float:
    """How long `open` waits for the server's first diagnostics before
    asking. CTX_LSP_SETTLE_S overrides (0 for a server that never publishes)."""
    raw = os.environ.get("CTX_LSP_SETTLE_S")
    try:
        return float(raw) if raw is not None else DEFAULT_SETTLE_S
    except ValueError:
        return DEFAULT_SETTLE_S


class LspError(RuntimeError):
    """A server was expected to answer and did not (crash, timeout, error)."""


def registry() -> dict[str, list[list[str]]]:
    raw = os.environ.get("CTX_LSP_SERVERS")
    if not raw:
        return SERVERS
    try:
        table = json.loads(raw)
        return {str(k): [list(map(str, argv)) for argv in v] for k, v in table.items()}
    except Exception:
        return SERVERS


def server_for(language: str | None) -> list[str] | None:
    """The first configured server command for ``language`` whose executable
    is on PATH, or None (the tier is then simply absent for that language)."""
    if not language:
        return None
    for argv in registry().get(language, []):
        if argv and shutil.which(argv[0]):
            return [shutil.which(argv[0]) or argv[0], *argv[1:]]
    return None


def roster() -> dict[str, str]:
    """language -> server command found on PATH, or 'none'. For ctx doctor."""
    from ctx.skeleton import _LANG_BY_EXT

    out = {}
    for lang in sorted(set(_LANG_BY_EXT.values())):
        argv = server_for(lang)
        out[lang] = Path(argv[0]).name if argv else "none"
    return out


class Client:
    """One language-server process, JSON-RPC over stdio, for one call."""

    def __init__(self, argv: list[str], root: Path, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.argv = argv
        self.root = Path(root).resolve()
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self._responses: dict[int, dict[str, Any]] = {}
        self._reader: threading.Thread | None = None
        self._dead = threading.Event()
        self._diagnosed: set[str] = set()

    # ----------------------------------------------------------- lifecycle
    def __enter__(self) -> Client:
        try:
            self._proc = subprocess.Popen(
                self.argv, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise LspError(f"cannot start {self.argv[0]}: {exc}") from None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": self.root.as_uri(),
            "rootPath": str(self.root),
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
            "initializationOptions": {},
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "hover": {"contentFormat": ["plaintext", "markdown"]},
                    "definition": {}, "references": {}, "publishDiagnostics": {},
                },
                # Advertise configuration support and then send empty
                # settings: pyright asks for its settings before it will
                # analyze anything, and without either signal it waits
                # forever (measured: initialize answered, then silence).
                "workspace": {
                    "workspaceFolders": True, "configuration": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                },
            },
        })
        self.notify("initialized", {})
        # Workspace-wide analysis: pyright's default `openFilesOnly` answers
        # references from the open document alone (measured: 2 sites in one
        # file for a symbol used across the repository). Other servers
        # ignore keys they do not know.
        self.notify("workspace/didChangeConfiguration", {"settings": _SETTINGS})
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._proc and self._proc.poll() is None:
                try:
                    self.request("shutdown", None, timeout=3.0)
                except LspError:
                    pass
                self.notify("exit", None)
        finally:
            self.close()

    def close(self) -> None:
        p = self._proc
        if p is None:
            return
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
        except OSError:
            pass
        self._dead.set()

    # ------------------------------------------------------------- framing
    def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.poll() is not None:
            raise LspError(f"{self.argv[0]} is not running")
        body = json.dumps(payload).encode("utf-8")
        with self._lock:
            try:
                self._proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise LspError(f"{self.argv[0]} closed its stdin: {exc}") from None

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        out = proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if length is None:
                    continue
                body = out.read(length)
                if not body:
                    return
                try:
                    msg = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                    self._responses[int(msg["id"])] = msg
                elif isinstance(msg, dict) and msg.get("method") == "textDocument/publishDiagnostics":
                    uri = str((msg.get("params") or {}).get("uri") or "")
                    if uri:
                        self._diagnosed.add(uri)
                elif isinstance(msg, dict) and "id" in msg and "method" in msg:
                    # A server→client request: answer so the server never
                    # blocks on us. workspace/configuration wants one entry
                    # per requested item (pyright stalls on a bare null);
                    # everything else (registerCapability, workDoneProgress/
                    # create) takes null.
                    result: Any = None
                    if msg["method"] == "workspace/configuration":
                        items = (msg.get("params") or {}).get("items") or []
                        result = [_configuration_for(it) for it in items]
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except Exception:
            pass
        finally:
            self._dead.set()

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:
        self._seq += 1
        rid = self._seq
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        budget = float(explicit(timeout, self.timeout))  # type: ignore[arg-type]
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            msg = self._responses.pop(rid, None)
            if msg is not None:
                if "error" in msg:
                    err = msg["error"] or {}
                    raise LspError(f"{method}: {err.get('message', 'error')}")
                return msg.get("result")
            if self._dead.is_set() and rid not in self._responses:
                raise LspError(f"{self.argv[0]} exited before answering {method}")
            time.sleep(0.01)
        raise LspError(f"{method}: no answer from {self.argv[0]} within {budget:g}s")

    # ------------------------------------------------------------- documents
    def open(self, rel: str, language: str) -> str:
        path = self.root / rel
        text = path.read_text(encoding="utf-8", errors="replace")
        uri = path.as_uri()
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": _LANGUAGE_IDS.get(language, language), "version": 1, "text": text,
        }})
        # Let the server settle before the question. A server applies the
        # settings it asked for asynchronously after `initialized`; asked
        # too early, pyright answered references from the open file only
        # (2 sites) where a second later it answered from the workspace
        # (32 sites in 6 files). Its diagnostics for the opened document are
        # the signal that settings are applied and the file is analyzed;
        # a server that never publishes costs at most the settle budget.
        deadline = time.monotonic() + settle_seconds()
        while time.monotonic() < deadline and uri not in self._diagnosed and not self._dead.is_set():
            time.sleep(0.02)
        return uri

    # --------------------------------------------------------------- queries
    def definition(self, uri: str, line: int, col: int) -> list[dict[str, Any]]:
        return _locations(self.request("textDocument/definition", _pos(uri, line, col)))

    def references(self, uri: str, line: int, col: int, *, include_declaration: bool = True) -> list[dict[str, Any]]:
        params = _pos(uri, line, col)
        params["context"] = {"includeDeclaration": include_declaration}
        return _locations(self.request("textDocument/references", params))

    def hover(self, uri: str, line: int, col: int) -> str | None:
        res = self.request("textDocument/hover", _pos(uri, line, col))
        if not res:
            return None
        contents = res.get("contents")
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return str(contents.get("value") or "")
        if isinstance(contents, list):
            return "\n".join(
                c if isinstance(c, str) else str(c.get("value") or "") for c in contents
            )
        return None


def _pos(uri: str, line: int, col: int) -> dict[str, Any]:
    """1-based ctx coordinates → 0-based LSP position."""
    return {"textDocument": {"uri": uri}, "position": {"line": max(0, line - 1), "character": max(0, col - 1)}}


def _locations(res: Any) -> list[dict[str, Any]]:
    if res is None:
        return []
    items = res if isinstance(res, list) else [res]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        uri = it.get("uri") or it.get("targetUri")
        rng = it.get("range") or it.get("targetSelectionRange") or it.get("targetRange") or {}
        start = (rng.get("start") or {})
        if not uri:
            continue
        out.append({"uri": str(uri), "line": int(start.get("line", 0)) + 1, "col": int(start.get("character", 0)) + 1})
    return out


# ------------------------------------------------------------------ facade
def _rel_of(uri: str, root: Path) -> str | None:
    if not uri.startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse

    p = Path(unquote(urlparse(uri).path)).resolve()
    try:
        return p.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _sites(locs: list[dict[str, Any]], root: Path) -> list[tuple[str, int, str]]:
    """Locations → (rel, line, line text), workspace-confined, deduplicated."""
    texts: dict[str, list[str]] = {}
    sites: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int]] = set()
    for loc in locs:
        rel = _rel_of(loc["uri"], root)
        if rel is None or (rel, loc["line"]) in seen:
            continue
        seen.add((rel, loc["line"]))
        if rel not in texts:
            try:
                texts[rel] = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                texts[rel] = []
        lines = texts[rel]
        text = lines[loc["line"] - 1] if 0 < loc["line"] <= len(lines) else ""
        sites.append((rel, loc["line"], text.rstrip()))
    return sorted(sites)


def query(
    root: Path, rel: str, line: int, col: int, what: str, *, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[list[tuple[str, int, str]] | str | None, str]:
    """One semantic question at a position. Returns ``(answer, server_name)``:
    sites for ``definition``/``references``, text for ``hover``. Raises
    ``LspError`` when no server exists for the file's language or the server
    fails; the caller decides what rung comes next."""
    from ctx.skeleton import language_for

    language = language_for(rel)
    argv = server_for(language)
    if argv is None:
        raise LspError(f"no language server on PATH for {language or 'this file'}")
    with Client(argv, root, timeout=timeout) as client:
        uri = client.open(rel, language or "")
        if what == "definition":
            return _sites(client.definition(uri, line, col), root), Path(argv[0]).name
        if what == "references":
            return _sites(client.references(uri, line, col), root), Path(argv[0]).name
        if what == "hover":
            return client.hover(uri, line, col), Path(argv[0]).name
    raise LspError(f"unknown query {what!r}")


def symbol_position(root: Path, rel: str, symbol: str) -> tuple[int, int] | None:
    """Where a named symbol's identifier sits, from the file's skeleton: the
    row's first line, and the column of the last dotted component on it."""
    from ctx.skeleton import skeleton_for
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(root))
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    rows = skeleton_for(store, ws, rel).get("symbols") or []
    want = symbol.split(".")
    for row in rows:
        dotted = ([row["scope"]] if row.get("scope") else []) + [row["name"]]
        if dotted[-len(want):] == want or row["name"] == want[-1]:
            line = int(row["range"][0])
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()[line - 1]
            except (OSError, IndexError):
                return line, 1
            col = text.find(want[-1])
            return line, (col + 1) if col >= 0 else 1
    return None
