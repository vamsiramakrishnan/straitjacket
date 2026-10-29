import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from conftest import make_store, make_ws
from ctx import lsp
from ctx.edit_transactions import EditTransactionError, apply_edit_plan, preview_edit_plan
from ctx.lsp_edits import propose, workspace_edit_plan
from ctx.post_edit_diagnostics import capture_baseline, verify_post_edit


@pytest.fixture
def project(state_home, workspace_dir, monkeypatch, tmp_path):
    fake = Path(__file__).with_name('fake_lsp_server.py')
    monkeypatch.setenv('CTX_LSP_SERVERS', json.dumps({'python': [[sys.executable, str(fake)]]}))
    monkeypatch.setenv('CTX_LSP_SETTLE_S', '0')
    monkeypatch.setenv('CTX_LSP_IDLE_S', '.5')
    monkeypatch.setenv('CTX_TEST_LSP_LOG', str(tmp_path / 'lsp.jsonl'))
    monkeypatch.delenv('CTX_LSP', raising=False)
    monkeypatch.delenv('CTX_LSP_REUSE', raising=False)
    (workspace_dir / 'mod.py').write_text('def hello():\n    return 1\n')
    (workspace_dir / 'other.py').write_text('from mod import hello\nprint(hello())\n')
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def test_broker_reuses_across_processes_and_expires(project):
    ws, _ = project
    code = ('import json; from pathlib import Path; from ctx.lsp import semantic_request; '
            f'print(json.dumps(semantic_request(Path({str(ws.root)!r}), "mod.py", "hover")))')
    def query():
        return json.loads(subprocess.check_output([sys.executable, '-c', code], text=True))
    first, second = query(), query()
    assert first['pid'] == second['pid']
    time.sleep(1)
    assert query()['pid'] != first['pid']


def test_changed_documents_receive_fresh_diagnostics(project):
    ws, _ = project
    path = ws.root / 'mod.py'
    first = lsp.semantic_request(ws.root, 'mod.py', 'diagnostics', timeout=2)
    assert first['answer']['fresh'] and not first['answer']['items']
    path.write_text('BROKEN\n')
    second = lsp.semantic_request(ws.root, 'mod.py', 'diagnostics', timeout=2)
    assert second['pid'] == first['pid']
    assert second['answer']['version'] > first['answer']['version']
    assert second['answer']['items'][0]['severity'] == 1


def test_unversioned_diagnostics_cannot_prove_clean(project, monkeypatch):
    ws, _ = project
    monkeypatch.setenv('CTX_TEST_LSP_UNVERSIONED', '1')
    result = verify_post_edit(ws.root, 'mod.py', capture_baseline(ws.root, 'mod.py'),
        providers=[lsp.diagnostic_provider(ws.root, timeout=1)], run_builtin=False)
    assert result['outcome'] == 'stale'


def test_empty_pull_diagnostics_do_not_hide_versioned_push_errors(project, monkeypatch):
    ws, _ = project
    monkeypatch.setenv('CTX_TEST_LSP_PULL', '1')
    (ws.root / 'mod.py').write_text('BROKEN\n')
    result = lsp.semantic_request(ws.root, 'mod.py', 'diagnostics', timeout=2)['answer']
    assert result['fresh'] is True
    assert result['items'][0]['severity'] == 1


def test_diagnostic_error_after_display_limit_is_not_hidden(project, monkeypatch):
    import hashlib

    ws, _ = project
    digest = hashlib.sha256((ws.root / 'mod.py').read_bytes()).hexdigest()
    items = [{'message': 'warning', 'severity': 2}] * 50 + [{'message': 'error', 'severity': 1}]
    monkeypatch.setattr(lsp, 'semantic_request', lambda *a, **kw: {
        'server': 'fake', 'answer': {'fresh': True, 'digest': digest, 'items': items}})
    result = verify_post_edit(ws.root, 'mod.py', capture_baseline(ws.root, 'mod.py'),
        providers=[lsp.diagnostic_provider(ws.root)], run_builtin=False)
    assert result['outcome'] == 'issues'
    assert result['checks'][0]['omittedDiagnostics'] == 1


def test_rename_preview_apply_and_post_edit_checks(project):
    ws, store = project
    result = propose(ws, store, 'mod.py', 'rename', line=1, col=5, new_name='greet')
    plan = result['plan']
    assert len(plan['edits']) == 2
    assert 'hello' in (ws.root / 'other.py').read_text()
    assert preview_edit_plan(ws, store, plan)['outcome'] == 'ready'
    receipt = apply_edit_plan(ws, plan)
    assert receipt['outcome'] == 'applied'
    assert 'greet' in (ws.root / 'other.py').read_text()
    assert all(row['diagnostics']['outcome'] == 'clean' for row in receipt['files'])


def test_stale_rename_refuses_all_writes(project):
    ws, store = project
    plan = propose(ws, store, 'mod.py', 'rename', new_name='greet')['plan']
    (ws.root / 'other.py').write_text('new user work\n')
    with pytest.raises(EditTransactionError):
        apply_edit_plan(ws, plan)
    assert 'hello' in (ws.root / 'mod.py').read_text()


def test_semantic_plan_does_not_relocate_after_an_import_is_added(project):
    ws, store = project
    plan = propose(ws, store, 'mod.py', 'rename', new_name='greet')['plan']
    path = ws.root / 'mod.py'
    path.write_text('import something\n' + path.read_text())
    with pytest.raises(EditTransactionError, match='source file changed'):
        apply_edit_plan(ws, plan)


def test_document_version_mismatch_is_refused(project):
    ws, store = project
    path = ws.root / 'mod.py'
    edit = {'documentChanges': [{'textDocument': {'uri': path.as_uri(), 'version': 99}, 'edits': []}]}
    with pytest.raises(EditTransactionError, match='stale LSP document version'):
        workspace_edit_plan(ws, store, edit, {'mod.py': path.read_bytes()}, {path.as_uri(): 1})


def test_versioned_closed_files_are_opened_and_rename_recomputed(project, monkeypatch):
    ws, store = project
    monkeypatch.setenv('CTX_TEST_LSP_CLOSED_ZERO', '1')
    plan = propose(ws, store, 'mod.py', 'rename', new_name='greet')['plan']
    assert len(plan['edits']) == 2
    assert apply_edit_plan(ws, plan)['outcome'] == 'applied'
    events = [json.loads(line)['method'] for line in Path(os.environ['CTX_TEST_LSP_LOG']).read_text().splitlines()]
    assert events.count('textDocument/rename') == 2
    assert events.count('textDocument/didOpen') >= 2


def test_versioned_rename_refuses_more_than_document_limit(project, monkeypatch):
    ws, store = project
    monkeypatch.setenv('CTX_TEST_LSP_CLOSED_ZERO', '1')
    for index in range(64):
        (ws.root / f'extra{index}.py').write_text('from mod import hello\n')
    with pytest.raises(lsp.LspError, match='more than 64 open documents'):
        propose(ws, store, 'mod.py', 'rename', new_name='greet')
    assert 'hello' in (ws.root / 'mod.py').read_text()


def test_versioned_rename_at_document_limit_keeps_origin_open(project, monkeypatch):
    ws, store = project
    monkeypatch.setenv('CTX_TEST_LSP_CLOSED_ZERO', '1')
    for index in range(62):
        (ws.root / f'extra{index}.py').write_text('from mod import hello\n')
    plan = propose(ws, store, 'mod.py', 'rename', new_name='greet')['plan']
    assert len(plan['edits']) == 64


def test_cross_language_rename_is_refused_until_all_targets_are_observed(project, monkeypatch):
    ws, store = project
    path = ws.root / 'main.js'
    path.write_text('hello();\n')
    item = {'range': {'start': {'line': 0, 'character': 0},
                      'end': {'line': 0, 'character': 5}}, 'newText': 'greet'}
    monkeypatch.setattr(lsp, 'semantic_request', lambda *a, **kw: {
        'server': 'fake', 'versions': {}, 'answer': {'changes': {path.as_uri(): [item]}}})
    with pytest.raises(EditTransactionError, match='unobserved'):
        propose(ws, store, 'mod.py', 'rename', new_name='greet')
    assert path.read_text() == 'hello();\n'


@pytest.mark.parametrize('status', ['ready', 'loading'])
def test_rust_workspace_loading_is_bounded(project, monkeypatch, status):
    ws, _ = project
    monkeypatch.setenv('CTX_TEST_LSP_STATUS', status)
    argv = lsp.server_for('python')
    if status == 'loading':
        client = lsp.Client(argv, ws.root, timeout=.2)
        with pytest.raises(lsp.LspError, match='did not finish loading'):
            with client:
                pass
        assert client._proc is None
    else:
        with lsp.Client(argv, ws.root, timeout=.2) as client:
            assert client._server_status['quiescent'] is True


def test_pull_diagnostics_never_merge_stale_push(project, monkeypatch):
    ws, _ = project
    client = lsp.Client(['fake'], ws.root)
    uri = (ws.root / 'mod.py').as_uri()
    client.documents[uri] = (2, 'new text')
    client.capabilities['diagnosticProvider'] = True
    client.diagnostics[uri] = {'version': 1, 'diagnostics': [{'severity': 1, 'message': 'old error'}]}
    monkeypatch.setattr(client, 'request', lambda *a: {'kind': 'full', 'items': []})
    assert client.diagnostic_report(uri)['items'] == []


def test_code_action_only_plans_literal_edits(project):
    ws, store = project
    result = propose(ws, store, 'mod.py', 'actions')
    assert [a['plannable'] for a in result['actions']] == [True, False]
    assert propose(ws, store, 'mod.py', 'actions', action_index=0)['plan']
    with pytest.raises(EditTransactionError, match='unsupported command'):
        propose(ws, store, 'mod.py', 'actions', action_index=1)


def test_cli_rename_requires_plan_and_preserves_source(project, capsys):
    from ctx.cli import main

    ws, _ = project
    args = ['--workspace', str(ws.root), 'lsp', 'rename', 'mod.py:1:5', '--new-name', 'greet']
    assert main(args) == 2
    assert main([*args, '--out', 'rename.json']) == 0
    assert 'hello' in (ws.root / 'mod.py').read_text()
    plan = json.loads((ws.root / 'rename.json').read_text())
    assert plan['exactFiles'] is True
    assert main(['--workspace', str(ws.root), 'edit', 'preview', 'rename.json']) == 0
    assert main(['--workspace', str(ws.root), 'edit', 'apply', 'rename.json']) == 0
    assert 'greet' in (ws.root / 'mod.py').read_text()


def test_utf16_and_crlf_are_preserved_and_overlaps_refused(project):
    ws, store = project
    path = ws.root / 'mod.py'
    before = 'x = "😀hello"\r\n'.encode()
    path.write_bytes(before)
    item = {'range': {'start': {'line': 0, 'character': 7}, 'end': {'line': 0, 'character': 12}}, 'newText': 'greet'}
    edit = {'changes': {path.as_uri(): [item]}}
    plan = workspace_edit_plan(ws, store, edit, {'mod.py': before}, {})
    apply_edit_plan(ws, plan)
    assert path.read_bytes() == 'x = "😀greet"\r\n'.encode()
    path.write_bytes(before)
    edit['changes'][path.as_uri()].append(item)
    with pytest.raises(EditTransactionError, match='overlapping'):
        workspace_edit_plan(ws, store, edit, {'mod.py': before}, {})


@pytest.mark.parametrize('edit', [
    {'documentChanges': [{'kind': 'delete', 'uri': 'file:///etc/passwd'}]},
    {'changes': {'file:///etc/passwd': []}},
])
def test_unsafe_workspace_edits_are_refused(project, edit):
    ws, store = project
    with pytest.raises(EditTransactionError):
        workspace_edit_plan(ws, store, edit, {}, {})
