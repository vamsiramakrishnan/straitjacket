"""Private local LSP broker: one serialized server per workspace/configuration.

The socket accepts bounded semantic requests, never shell commands or edits.
Only the owning OS user can connect. No network listener or model call is used.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time

from ctx.lsp import Client, LspError, _pos, _sites

MAX_MESSAGE = 8 * 1024 * 1024


def _read(sock, count):
    result = bytearray()
    while len(result) < count:
        part = sock.recv(count - len(result))
        if not part:
            raise LspError("LSP broker closed the connection")
        result.extend(part)
    return bytes(result)


def _receive(sock):
    size = struct.unpack("!I", _read(sock, 4))[0]
    if size > MAX_MESSAGE:
        raise LspError("LSP broker message exceeds 8 MiB")
    return json.loads(_read(sock, size))


def _send(sock, value):
    data = json.dumps(value).encode()
    if len(data) > MAX_MESSAGE:
        raise LspError("LSP broker message exceeds 8 MiB")
    sock.sendall(struct.pack("!I", len(data)) + data)


def _address(root: Path, argv: list[str]) -> Path:
    directory = Path(tempfile.gettempdir()) / f"ctx-lsp-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    stat = directory.lstat()
    if directory.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise LspError("LSP socket directory must be private to the current user")
    # Hash, never persist, environment values. A different interpreter/config or
    # ctx implementation must not reuse an already-running server's old state.
    volatile = {'_', 'SHLVL', 'CTX_TASK_QUERY', 'CTX_TASK_CONTEXT_FILE', 'CTX_EDIT_ATTEMPT',
                'CTX_MODEL', 'CTX_HOST', 'CTX_TURN_ID', 'CTX_CONVERSATION_ID'}
    identity = [str(root.resolve()), argv, sorted((k, v) for k, v in os.environ.items() if k not in volatile),
                Path(__file__).read_bytes().hex(), Path(__file__).with_name('lsp.py').read_bytes().hex()]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    return directory / (key + '.sock')


def dispatch(client: Client, request: dict) -> dict:
    from ctx.skeleton import language_for

    rel, operation = request['rel'], request['operation']
    if operation not in {'definition', 'references', 'hover', 'diagnostics', 'rename', 'actions'}:
        raise LspError('unsupported LSP operation')
    # Refresh documents already known to this server before answering a new query.
    for uri in list(client.documents):
        from ctx.lsp import _rel_of
        opened = _rel_of(uri, client.root)
        if opened and (client.root / opened).is_file():
            client.open(opened, language_for(opened) or '')
        else:
            client.notify('textDocument/didClose', {'textDocument': {'uri': uri}})
            del client.documents[uri]
            client.diagnostics.pop(uri, None)
    uri = client.open(rel, language_for(rel) or '')
    line, col = request.get('line', 1), request.get('col', 1)
    if type(line) is not int or type(col) is not int or line < 1 or col < 1:
        raise LspError('LSP positions must be positive integers')
    if operation == 'definition':
        answer = _sites(client.definition(uri, line, col), client.root)
    elif operation == 'references':
        answer = _sites(client.references(uri, line, col), client.root)
    elif operation == 'hover':
        answer = client.hover(uri, line, col)
    elif operation == 'diagnostics':
        answer = client.diagnostic_report(uri)
    elif operation == 'rename':
        if not client.capabilities.get('renameProvider'):
            raise LspError('server does not advertise rename support')
        name = request.get('new_name')
        if not isinstance(name, str) or not name or len(name) > 256 or any(c.isspace() for c in name):
            raise LspError('rename requires a nonempty name without whitespace, at most 256 characters')
        params = _pos(uri, line, col)
        params['newName'] = name
        answer = client.request('textDocument/rename', params)
    else:
        if not client.capabilities.get('codeActionProvider'):
            raise LspError('server does not advertise code actions')
        position = _pos(uri, line, col)['position']
        diagnostic = client.diagnostics.get(uri, {})
        current = diagnostic.get('version') == client.documents[uri][0]
        answer = client.request('textDocument/codeAction', {
            'textDocument': {'uri': uri}, 'range': {'start': position, 'end': position},
            'context': {'diagnostics': diagnostic.get('diagnostics', []) if current else []}})
    return {'answer': answer, 'server': Path(client.argv[0]).name,
            'pid': client._proc.pid, 'versions': {u: v for u, (v, _) in client.documents.items()}}


def _refresh_project(client: Client, previous: dict | None) -> dict:
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(client.root))
    files = ws.list_files()
    if len(files) > 20000:
        raise LspError('workspace exceeds the 20000-file persistent LSP watch limit')
    current = {}
    for name in files:
        path = ws.confine(name, must_exist=True)
        stat = path.stat()
        current[path.as_uri()] = (stat.st_mtime_ns, stat.st_size)
    if previous is not None:
        events = [{'uri': uri, 'type': 1 if uri not in previous else 2}
                  for uri, value in current.items() if previous.get(uri) != value]
        events.extend({'uri': uri, 'type': 3} for uri in previous.keys() - current.keys())
        if events:
            client.diagnostics.clear()
            client._diagnosed.clear()
            client.notify('workspace/didChangeWatchedFiles', {'changes': events})
    return current


def request(root: Path, argv: list[str], payload: dict, timeout: float) -> dict:
    import fcntl

    deadline = time.monotonic() + timeout
    address = _address(root, argv)
    def connect():
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(max(.01, deadline - time.monotonic()))
        try:
            sock.connect(str(address))
        except BaseException:
            sock.close()
            raise
        return sock

    with address.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            sock = connect()
        except (FileNotFoundError, ConnectionRefusedError):
            address.unlink(missing_ok=True)
            proc = subprocess.Popen([sys.executable, '-m', 'ctx.lsp_service', str(address),
                                     str(root.resolve()), json.dumps(argv)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            startup_deadline = min(deadline, time.monotonic() + 5)
            while True:
                try:
                    sock = connect()
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if proc.poll() is not None or time.monotonic() >= startup_deadline:
                        if proc.poll() is None:
                            proc.terminate()
                        raise LspError('LSP broker failed to start') from None
                    time.sleep(.02)
    with sock:
        sock.settimeout(max(.01, deadline - time.monotonic()))
        _send(sock, {**payload, 'timeout': timeout})
        result = _receive(sock)
    if 'error' in result:
        raise LspError(result['error'])
    return result


def serve(address: Path, root: Path, argv: list[str]) -> None:
    client = None
    project = None
    idle = min(600, max(.2, float(os.environ.get('CTX_LSP_IDLE_S', '120'))))
    last = time.monotonic()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(8)
        listener.settimeout(.2)
        try:
            while time.monotonic() - last < idle:
                try:
                    sock, _ = listener.accept()
                except socket.timeout:
                    continue
                with sock:
                    sock.settimeout(5)
                    try:
                        payload = _receive(sock)
                        timeout = float(payload['timeout'])
                        if not 0 < timeout <= 120:
                            raise LspError('timeout must be greater than 0 and at most 120 seconds')
                        if client is None:
                            client = Client(argv, root, timeout=timeout).__enter__()
                            project = None
                        client.timeout = timeout
                        project = _refresh_project(client, project)
                        result = dispatch(client, payload)
                        _send(sock, result)
                    except (LspError, OSError, ValueError, KeyError, TypeError) as exc:
                        if client is not None:
                            client.close()
                            client = None
                        try:
                            _send(sock, {'error': str(exc)})
                        except OSError:
                            pass
                last = time.monotonic()
        finally:
            if client is not None:
                client.__exit__()
            address.unlink(missing_ok=True)


if __name__ == '__main__':
    serve(Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3]))
