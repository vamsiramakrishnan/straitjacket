import json
from pathlib import Path
import subprocess
import uuid

import pytest

from ctx.collab import Ledger, CollabError


@pytest.fixture
def project(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init')
    (tmp_path / '.gitignore').write_text('.ctx/\n')
    (tmp_path / 'src.py').write_text('original\n')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture')
    (tmp_path / 'src.py').write_text('user uncommitted work\n')
    (tmp_path / 'notes.txt').write_text('user untracked work\n')
    calls = []
    def launch(workspace, task_id, prompt, **options):
        calls.append({'workspace': workspace, 'task_id': task_id, 'prompt': prompt, **options})
        root = options['session_root'] / uuid.uuid4().hex
        root.mkdir(parents=True)
        row = {'session_id': root.name, 'status': 'completed', 'conversation_id': 'native-id',
               'directory': str(root), 'task_id': task_id}
        (root / 'state.json').write_text(json.dumps(row))
        return row
    monkeypatch.setattr('ctx.prewalk.launch', launch)
    ledger = Ledger(tmp_path / '.ctx/collab')
    task = ledger.create_task('fix src', owns=('src.py',), verify='python -V')
    ledger.claim(task, 'agy')
    ledger.plan(task, 'agy', [{'goal': 'fix source', 'check': 'python -V'}])
    return tmp_path, ledger, task, calls


def test_native_continuation_requires_real_owned_edit_and_preserves_user_work(project):
    root, ledger, task, calls = project
    guide = ledger.start_prewalk(task, model='gemini-4-argon-high')
    worktree = Path(guide['worktree'])
    assert (worktree / 'src.py').read_text() == 'user uncommitted work\n'
    assert (worktree / 'notes.txt').read_text() == 'user untracked work\n'
    with pytest.raises(CollabError, match='no edit'):
        ledger.continue_prewalk(guide['session_id'], model='gemini-3.8-flash-high')
    (worktree / 'src.py').write_text('fixed in isolation\n')
    executor = ledger.continue_prewalk(guide['session_id'], model='gemini-3.8-flash-high')
    assert calls[-1]['conversation'] == 'native-id'
    assert calls[-1]['workspace'] == worktree
    assert executor['first_edit_files'] == ['src.py']
    assert (root / 'src.py').read_text() == 'user uncommitted work\n'
    with pytest.raises(CollabError, match='already has a continuation'):
        ledger.continue_prewalk(guide['session_id'], model='gemini-3.8-flash-high')


def test_out_of_scope_first_edit_never_reaches_executor(project):
    root, ledger, task, calls = project
    guide = ledger.start_prewalk(task, model='gemini-4-argon-high')
    (Path(guide['worktree']) / 'notes.txt').write_text('unauthorized change')
    with pytest.raises(CollabError, match='outside ownership'):
        ledger.continue_prewalk(guide['session_id'], model='gemini-3.8-flash-high')
    assert len(calls) == 1
    assert (root / 'notes.txt').read_text() == 'user untracked work\n'
