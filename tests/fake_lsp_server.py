"""A minimal language server for tests: JSON-RPC over stdio, answering
initialize / shutdown, textDocument/definition, references and hover with
fixed locations derived from the opened document, and issuing one
server→client request (workspace/configuration) so the client's null
answer path is exercised. Runs under the same interpreter as the tests."""

from __future__ import annotations

import json
import sys
import os
import re
from pathlib import Path


def _read() -> dict | None:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    if length is None:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def _write(msg: dict) -> None:
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def main() -> int:
    opened: dict[str, str] = {}
    versions = {}
    root = None
    while True:
        msg = _read()
        if msg is None:
            return 0
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if os.environ.get('CTX_TEST_LSP_LOG'):
            with open(os.environ['CTX_TEST_LSP_LOG'], 'a') as log:
                log.write(json.dumps({'pid': os.getpid(), 'method': method}) + '\n')
        if method == "initialize":
            root = Path(params['rootPath'])
            _write({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {
                "definitionProvider": True, "textDocumentSync": 2,
                "renameProvider": True, "codeActionProvider": True}}})
            # A server→client request the client must answer (with null).
            _write({"jsonrpc": "2.0", "id": 999, "method": "workspace/configuration", "params": {"items": []}})
        elif method in {"textDocument/didOpen", "textDocument/didChange"}:
            td = params["textDocument"]
            opened[td["uri"]] = td.get("text", params.get('contentChanges', [{}])[0].get('text', ''))
            versions[td['uri']] = td['version']
            diagnostics = [{'message': 'unknown name BROKEN', 'severity': 1,
                            'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 6}}}] if 'BROKEN' in opened[td['uri']] else []
            report = {'uri': td['uri'], 'diagnostics': diagnostics}
            if not os.environ.get('CTX_TEST_LSP_UNVERSIONED'):
                report['version'] = td['version']
            _write({'jsonrpc': '2.0', 'method': 'textDocument/publishDiagnostics', 'params': report})
        elif method == 'textDocument/rename':
            changes = []
            for path in sorted(root.glob('*.py')):
                uri = path.as_uri()
                edits = []
                for line, text in enumerate(opened.get(uri, path.read_text()).splitlines()):
                    for hit in re.finditer(r'\bhello\b', text):
                        edits.append({'range': {'start': {'line': line, 'character': hit.start()},
                                                'end': {'line': line, 'character': hit.end()}},
                                      'newText': params['newName']})
                if edits:
                    changes.append({'textDocument': {'uri': uri, 'version': versions.get(uri)}, 'edits': edits})
            _write({'jsonrpc': '2.0', 'id': rid, 'result': {'documentChanges': changes}})
        elif method == 'textDocument/codeAction':
            uri = params['textDocument']['uri']
            edit = {'range': {'start': {'line': 0, 'character': 4}, 'end': {'line': 0, 'character': 9}}, 'newText': 'greet'}
            _write({'jsonrpc': '2.0', 'id': rid, 'result': [
                {'title': 'Rename declaration', 'edit': {'changes': {uri: [edit]}}},
                {'title': 'Run arbitrary command', 'command': {'command': 'unsafe'}}]})
        elif method == "textDocument/definition":
            uri = params["textDocument"]["uri"]
            _write({"jsonrpc": "2.0", "id": rid, "result": {
                "uri": uri, "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 9}}}})
        elif method == "textDocument/references":
            uri = params["textDocument"]["uri"]
            other = uri.rsplit("/", 1)[0] + "/other.py"
            _write({"jsonrpc": "2.0", "id": rid, "result": [
                {"uri": uri, "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 9}}},
                {"uri": uri, "range": {"start": {"line": 2, "character": 11}, "end": {"line": 2, "character": 16}}},
                {"uri": other, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}},
                {"uri": "file:///outside/elsewhere.py", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}},
            ]})
        elif method == "textDocument/hover":
            _write({"jsonrpc": "2.0", "id": rid, "result": {"contents": {"kind": "plaintext", "value": "def hello(name: str) -> str"}}})
        elif method == "shutdown":
            _write({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "exit":
            return 0
        elif rid is not None:
            _write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown {method}"}})


if __name__ == "__main__":
    sys.exit(main())
