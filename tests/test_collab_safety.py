import concurrent.futures
import json
import subprocess

import pytest

from ctx.collab import Ledger, CollabError, spawn
from ctx.hook import classify_command


def test_quoted_chain_does_not_reject_its_own_capture_wrapper():
    command = "ctx run --shell -- 'set -a; echo x; set +a'"
    assert classify_command(command, {})['decision'] == 'allow'
    decision = classify_command(command + '; rm -rf /tmp/example', {'deny_commands': ['rm']})
    assert decision['decision'] != 'allow'


def test_parallel_claim_is_atomic(tmp_path):
    ledger = Ledger(tmp_path / '.ctx/collab')
    tid = ledger.create_task('fix', owns=('src',))
    def claim(name):
        try:
            Ledger(ledger.dir).claim(tid, name)
            return True
        except CollabError:
            return False
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(claim, ['codex', 'agy'])) == 1


def test_overlapping_tasks_and_unowned_completion_rejected(tmp_path):
    ledger = Ledger(tmp_path / '.ctx/collab')
    a = ledger.create_task('a', owns=('src',))
    b = ledger.create_task('b', owns=('src/one.py',))
    ledger.claim(a, 'codex')
    with pytest.raises(CollabError, match='overlaps'):
        ledger.claim(b, 'agy')
    with pytest.raises(CollabError, match='holder'):
        ledger.complete(a, 'agy', status='done', summary='false')
    with pytest.raises(CollabError, match='outside'):
        ledger.complete(a, 'codex', status='done', summary='false', files_changed=('other.py',))


def test_torn_tail_is_preserved_and_next_write_remains_readable(tmp_path):
    ledger = Ledger(tmp_path / '.ctx/collab')
    task = ledger.create_task('a')
    with ledger.path.open('a') as handle:
        handle.write('{"broken":')
    ledger.claim(task, 'codex')
    assert ledger.state()[task]['holder'] == 'codex'
    assert len(list(ledger.dir.glob('torn-*.json'))) == 1
    for line in ledger.path.read_text().splitlines():
        json.loads(line)


def test_stale_verification_cannot_integrate(tmp_path):
    ledger = Ledger(tmp_path / '.ctx/collab')
    source = tmp_path / 'file.py'
    source.write_text('old')
    task = ledger.create_task('fix', owns=('file.py',), verify='python -V')
    ledger.claim(task, 'codex')
    ledger.plan(task, 'codex', [{'goal': 'fix', 'check': 'python -V'}])
    with pytest.raises(CollabError, match='planned steps'):
        ledger.complete(task, 'codex', status='done', summary='premature')
    ledger.progress(task, 'codex', 0, 'done', 'test receipt')
    ledger.complete(task, 'codex', status='done', summary='fixed')
    with pytest.raises(CollabError, match='independent'):
        ledger.review(task, 'codex', verdict='approve', evidence='self review')
    ledger.verify(task, tmp_path)
    ledger.review(task, 'agy', verdict='approve', evidence='reviewed file.py')
    source.write_text('changed after test')
    with pytest.raises(CollabError, match='verification'):
        ledger.integrate(task, accepted=True, note='stale')


def test_agy_flags_precede_prompt_and_keep_explicit_conversation():
    seen = []
    def runner(argv, **kwargs):
        seen.extend(argv)
        return subprocess.CompletedProcess(argv, 0)
    spawn('agy', 'review', model='gemini-4-argon-high', mode='plan',
          conversation='conversation-1', structured=True, runner=runner)
    assert seen[-2:] == ['-p', 'review']
    assert seen[seen.index('--conversation') + 1] == 'conversation-1'
    assert '--dangerously-skip-permissions' not in seen


def test_quoted_command_substitution_never_uses_simple_chain_parser():
    from ctx.hook import _split_simple_chain

    assert _split_simple_chain('echo "$(touch marker)"; echo done') is None
    assert _split_simple_chain('echo "`touch marker`"; echo done') is None
    assert _split_simple_chain("echo 'literal; text'; echo done") == [
        "echo 'literal; text'", 'echo done']
