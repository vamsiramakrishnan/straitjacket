"""Two-stage native agy continuation in an isolated worktree.

The controller chooses when to continue; completion events never auto-apply a patch.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import uuid

from ctx.collab import CollabError, _path, _under, handoff_packet
from ctx.collab_sessions import launch, status


def _git(root, *args, input=None):
    return subprocess.run(['git', '-C', str(root), *args], input=input,
                          capture_output=True, check=True).stdout


def snapshot(root: Path) -> dict[str, str]:
    names = _git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
    result = {}
    for raw in names.split(b'\0'):
        if not raw:
            continue
        name = raw.decode('utf-8')
        if name.split('/')[0] == '.ctx':
            continue
        path = root / name
        if path.is_symlink():
            raise CollabError('prewalk currently requires a worktree without symlink files')
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'deleted'
    return result


def isolate(source: Path, destination: Path) -> dict[str, str]:
    before = snapshot(source)
    _git(source, 'worktree', 'add', '--detach', str(destination), 'HEAD')
    try:
        patch = _git(source, 'diff', '--binary', 'HEAD')
        if patch:
            _git(destination, 'apply', '--binary', '-', input=patch)
        untracked = _git(source, 'ls-files', '-z', '--others', '--exclude-standard')
        for raw in untracked.split(b'\0'):
            if raw:
                name = raw.decode('utf-8')
                if name.split('/')[0] == '.ctx':
                    continue
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, target)
        if before != snapshot(source) or before != snapshot(destination):
            raise CollabError('source changed while creating worktree; retry after writers finish')
        return before
    except BaseException:
        _git(source, 'worktree', 'remove', '--force', str(destination))
        raise


def _start(ledger, task_id: str, *, model: str, timeout: float = 300) -> dict:
    task = ledger.state().get(task_id, {})
    if task.get('holder') != 'agy' or not task.get('plan') or not task.get('owns'):
        raise CollabError('prewalk requires an agy lease, owned paths and a validated plan')
    source = ledger.dir.parent.parent.resolve()
    worktree = source / '.ctx/worktrees' / uuid.uuid4().hex
    before = isolate(source, worktree)
    prompt = handoff_packet(task, 'agy') + (
        '\nFor this guide turn only: investigate, follow the recorded plan, make one meaningful '
        'edit within your owned files, then stop. Do not declare the whole task complete. '
        'Do not commit, publish, or invoke another agent. The controller will verify the actual '
        'diff before continuing this exact conversation. Tool permission denial means stop and report it.')
    row = launch(worktree, task_id, prompt, model=model, mode='accept-edits', timeout=timeout,
                 session_root=ledger.dir / 'sessions')
    root = Path(row['directory'])
    (root / 'prewalk.json').write_text(json.dumps({'phase': 'guide', 'worktree': str(worktree),
        'baseline': before, 'task_id': task_id, 'plan': task['plan'], 'source': str(source)}, indent=2))
    ledger._append('session', task_id=task_id, session_id=row['session_id'])
    return {**row, 'worktree': str(worktree), 'phase': 'guide'}


def _continue_session(ledger, guide_id: str, *, model: str, timeout: float = 300) -> dict:
    if len(guide_id) != 32 or any(c not in '0123456789abcdef' for c in guide_id):
        raise CollabError('invalid guide session ID')
    root = ledger.dir / 'sessions' / guide_id
    meta = json.loads((root / 'prewalk.json').read_text())
    guide = status(root)
    task = ledger.state().get(meta['task_id'], {})
    if guide['status'] != 'completed' or not guide.get('conversation_id'):
        raise CollabError('guide must finish a valid native turn before continuation')
    if task.get('holder') != 'agy' or task.get('plan') != meta['plan']:
        raise CollabError('guide task lease or plan changed')
    if (root / 'continued.json').exists():
        raise CollabError('guide already has a continuation')
    worktree = Path(meta['worktree'])
    after = snapshot(worktree)
    changed = [name for name in sorted(set(after) | set(meta['baseline']))
               if after.get(name) != meta['baseline'].get(name)]
    if not changed:
        raise CollabError('guide made no edit; a plan or acknowledgement is insufficient')
    for name in changed:
        if (not any(_under(name, _path(p)) for p in task['owns'])
                or any(_under(name, _path(p)) for p in task.get('avoid', []))):
            raise CollabError(f'guide edited outside ownership: {name}')
    # A changed file alone is not evidence that the first edit works. Run the
    # first agreed check locally in the isolated tree before spending on continuation.
    check = task['plan'][0]['check']
    try:
        checked = subprocess.run(shlex.split(check), cwd=worktree, capture_output=True,
                                 timeout=min(timeout, 300))
    except subprocess.TimeoutExpired as exc:
        raise CollabError('first-edit check timed out; continuation refused') from exc
    (root / 'first-edit.stdout').write_bytes(checked.stdout)
    (root / 'first-edit.stderr').write_bytes(checked.stderr)
    proof = {'command': check, 'returncode': checked.returncode, 'snapshot': after}
    (root / 'first-edit-check.json').write_text(json.dumps(proof, indent=2))
    if checked.returncode or snapshot(worktree) != after:
        raise CollabError('first-edit check failed or changed files; continuation refused')
    prompt = (handoff_packet(task, 'agy') + '\nContinue this exact conversation and worktree. '
              'The guide-only instruction has ended. Complete remaining task steps and their '
              'validation checks. Do not repeat investigation already supported by the history. '
              'Do not commit or publish. Report denied checks honestly. First-edit files: ' + ', '.join(changed))
    row = launch(worktree, meta['task_id'], prompt, model=model, mode='accept-edits',
                 conversation=guide['conversation_id'], timeout=timeout,
                 session_root=ledger.dir / 'sessions')
    (root / 'continued.json').write_text(json.dumps(row, indent=2))
    ledger._append('session', task_id=meta['task_id'], session_id=row['session_id'])
    return {**row, 'worktree': str(worktree), 'phase': 'execute', 'first_edit_files': changed}
