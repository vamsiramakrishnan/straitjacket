"""Translate untrusted LSP WorkspaceEdits into ctx's sealed CAS plans.

Only edits to observed, existing workspace text files are accepted. Commands,
resource operations, annotations, overlaps and unknown versions are refused.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ctx import anchors, lsp
from ctx.edit_transactions import EditTransactionError, create_edit_plan


def _offset(text: str, position: dict) -> int:
    line, character = position.get('line'), position.get('character')
    if type(line) is not int or type(character) is not int or min(line, character) < 0:
        raise EditTransactionError('invalid LSP position')
    lines = text.splitlines(keepends=True)
    if not lines or text.endswith(('\n', '\r')):
        lines.append('')
    if line >= len(lines):
        raise EditTransactionError('LSP line outside document')
    content = lines[line].removesuffix('\n').removesuffix('\r')
    units = 0
    for index, char in enumerate(content):
        if units == character:
            return sum(map(len, lines[:line])) + index
        units += len(char.encode('utf-16-le')) // 2
        if units > character:
            raise EditTransactionError('LSP position splits a UTF-16 surrogate pair')
    if units != character:
        raise EditTransactionError('LSP column outside document')
    return sum(map(len, lines[:line])) + len(content)


def workspace_edit_plan(ws, store, edit: dict, observed: dict[str, bytes], versions: dict) -> dict:
    try:
        return _workspace_edit_plan(ws, store, edit, observed, versions)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise EditTransactionError(f'malformed LSP workspace edit: {exc}') from exc


def _workspace_edit_plan(ws, store, edit: dict, observed: dict[str, bytes], versions: dict) -> dict:
    if not isinstance(edit, dict) or edit.get('changeAnnotations'):
        raise EditTransactionError('annotated or missing workspace edit is unsupported')
    if 'changes' in edit and 'documentChanges' in edit:
        raise EditTransactionError('workspace edit contains both edit representations')
    changes = []
    for uri, edits in edit.get('changes', {}).items():
        changes.append((uri, None, edits))
    for item in edit.get('documentChanges', []):
        if 'kind' in item or 'textDocument' not in item:
            raise EditTransactionError('LSP resource operations are not supported')
        document = item['textDocument']
        changes.append((document['uri'], document.get('version'), item['edits']))
    if not changes or len(changes) > 128:
        raise EditTransactionError('workspace edit must contain 1 to 128 files')
    requests, seen = [], set()
    for uri, version, edits in changes:
        parsed = urlparse(uri)
        if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost') or parsed.query or parsed.fragment:
            raise EditTransactionError('workspace edits require local file URIs')
        rel = lsp._rel_of(uri, ws.root)
        if rel is None or rel not in observed or rel in seen:
            raise EditTransactionError('unobserved, duplicate or outside-workspace edit target')
        seen.add(rel)
        path = ws.confine(rel, must_exist=True)
        if ws.is_ignored(rel) or path.is_symlink():
            raise EditTransactionError('edit target is excluded or a symlink')
        if version is not None and (type(version) is not int or versions.get(uri) != version):
            raise EditTransactionError('unknown or stale LSP document version')
        before = observed[rel]
        if path.read_bytes() != before:
            raise EditTransactionError('source changed while the server computed edits')
        text = before.decode('utf-8')
        if not text or not isinstance(edits, list) or not 0 < len(edits) <= 4096:
            raise EditTransactionError('empty files or excessive/missing text edits are unsupported')
        spans = []
        for item in edits:
            if 'annotationId' in item or not isinstance(item.get('newText'), str):
                raise EditTransactionError('invalid or annotated text edit')
            start, end = _offset(text, item['range']['start']), _offset(text, item['range']['end'])
            if end < start:
                raise EditTransactionError('reversed LSP edit range')
            spans.append((start, end, item['newText']))
        spans.sort(key=lambda item: (item[0], item[1]))
        for left, right in zip(spans, spans[1:]):
            if right[0] < left[1] or right[0] == left[0]:
                raise EditTransactionError('overlapping or ambiguous LSP edits')
        replacement = text
        for start, end, new_text in reversed(spans):
            replacement = replacement[:start] + new_text + replacement[end:]
        if len(replacement.encode()) > 2 * 1024 * 1024:
            raise EditTransactionError('replacement exceeds 2 MiB')
        lines = text.splitlines()
        requests.append({'path': rel, 'span': anchors.format_span(1, len(lines), anchors.anchor(lines)),
                         'replacement': replacement})
    plan = create_edit_plan(ws, store, {'schema': 'ctx.edit-request/v1', 'edits': requests}, exact_files=True)
    # Close the snapshot/planning race before exposing a usable plan.
    if any(ws.confine(rel).read_bytes() != observed[rel] for rel in seen):
        raise EditTransactionError('source changed while sealing the LSP edit plan')
    return plan


def propose(ws, store, rel: str, operation: str, *, line=1, col=1, new_name=None,
            action_index=None, timeout=20) -> dict:
    from ctx.skeleton import language_for

    observed = {}
    total = 0
    language = language_for(rel)
    for name in ws.list_files():
        if language_for(name) != language:
            continue
        path = ws.confine(name, must_exist=True)
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        data = path.read_bytes()
        try:
            data.decode('utf-8')
        except UnicodeError:
            continue
        total += len(data)
        if len(observed) >= 4096 or total > 32 * 1024 * 1024:
            raise EditTransactionError('refactor observation exceeds 4096 files or 32 MiB; narrow the workspace')
        observed[name] = data
    result = lsp.semantic_request(ws.root, rel, operation, line=line, col=col,
                                  timeout=timeout, new_name=new_name)
    edit = result['answer']
    if operation == 'actions':
        actions = edit or []
        if not isinstance(actions, list) or any(not isinstance(a, dict) for a in actions):
            raise EditTransactionError('malformed code-action response')
        if len(actions) > 64:
            raise EditTransactionError('server returned more than 64 code actions; narrow the target')
        if action_index is None:
            return {'server': result['server'], 'actions': [
                {'index': i, 'title': str(a.get('title', ''))[:200],
                 'plannable': bool(a.get('edit')) and not a.get('command') and not a.get('disabled')}
                for i, a in enumerate(actions)]}
        if type(action_index) is not int or not 0 <= action_index < len(actions):
            raise EditTransactionError('code action index out of range')
        action = actions[action_index]
        if action.get('command') or action.get('disabled') or not action.get('edit'):
            raise EditTransactionError('code action requires an unsupported command/resolve step or is disabled')
        edit = action['edit']
    return {'server': result['server'], 'plan': workspace_edit_plan(ws, store, edit, observed, result['versions'])}
