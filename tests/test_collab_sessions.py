import json
import os
from pathlib import Path
import sys

import pytest

from ctx.collab_sessions import launch, watch, status, recover, _native_result, result_status


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    binary = tmp_path / 'bin'
    binary.mkdir()
    agy = binary / 'agy'
    agy.write_text(f'#!{sys.executable}\n' + '''import json, sys, time
model = sys.argv[sys.argv.index('--model') + 1]
conversation = 'native-conversation'
print(json.dumps({'event': 'init', 'conversation_id': conversation}), flush=True)
if model == 'hang':
    time.sleep(30)
result = {'conversation_id': conversation, 'status': 'SUCCESS', 'response': 'review done'}
if model == 'denied':
    result['denied_actions'] = [{'action': 'command'}]
print(json.dumps({'event': 'result', 'result': result}), flush=True)
''')
    agy.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    return tmp_path


def test_native_result_envelope_matches_live_agy():
    payload = {'conversation_id': 'c', 'status': 'SUCCESS', 'response': 'ok'}
    assert _native_result({'event': 'result', 'result': payload}) == payload
    assert result_status(payload, 0) == 'completed'
    assert result_status({**payload, 'denied_actions': [{}]}, 0) == 'blocked'
    assert result_status({**payload, 'response': ''}, 0) == 'blocked'


def test_async_any_all_and_cancellation(fake_agy):
    first = launch(fake_agy, 'task1', 'read only', model='ok', timeout=10)
    second = launch(fake_agy, 'task2', 'read only', model='hang', timeout=10)
    roots = [Path(row['directory']) for row in (first, second)]
    try:
        any_result = watch(roots, until='any', timeout=5)
        assert any_result['satisfied']
        assert status(roots[0])['status'] == 'completed'
        assert not watch(roots, until='all', timeout=0)['satisfied']
        (roots[1] / 'cancel').touch()
        assert watch(roots, until='all', timeout=5)['satisfied']
        assert status(roots[1])['status'] == 'cancelled'
        assert all(not status(root)['task_completed'] for root in roots)
        events = [json.loads(line) for line in (roots[0] / 'events.jsonl').read_text().splitlines()]
        assert sum(event['kind'] == 'terminal' for event in events) == 1
    finally:
        for root in roots:
            (root / 'cancel').touch()


@pytest.mark.parametrize('model,expected', [('denied', 'blocked'), ('hang', 'timed_out')])
def test_denial_and_deadline_are_not_success(fake_agy, model, expected):
    row = launch(fake_agy, 'task', 'read', model=model, timeout=0.3)
    root = Path(row['directory'])
    assert watch([root], timeout=5)['satisfied']
    assert status(root)['status'] == expected


def test_conversation_identity_must_match(fake_agy):
    row = launch(fake_agy, 'task', 'read', model='ok', conversation='wrong', timeout=5)
    root = Path(row['directory'])
    assert watch([root], timeout=5)['satisfied']
    assert status(root)['status'] == 'failed'
    assert 'different conversation' in status(root)['error']


def test_recovery_replays_capture_without_another_agent_call(tmp_path):
    from ctx import relay

    relay.watch(tmp_path, subscriber='codex', topic='task', selector='review')
    root = tmp_path / '.ctx/collab/sessions/session'
    root.mkdir(parents=True)
    os.mkfifo(root / 'wake')
    (root / 'state.json').write_text(json.dumps({'status': 'blocked', 'returncode': 0,
        'conversation_id': 'c', 'seq': 1, 'task_completed': False, 'task_id': 'review'}))
    payload = {'conversation_id': 'c', 'status': 'SUCCESS', 'response': 'verified review'}
    (root / 'stdout.jsonl').write_text(json.dumps({'event': 'result', 'result': payload}) + '\n')
    assert recover(root)['status'] == 'completed'
    assert recover(root)['seq'] == 2
    assert (root / 'response.md').read_text() == 'verified review'
    signals = relay.pending(tmp_path, 'codex')
    assert len(signals) == 1
    assert signals[0]['ref'] == 'repo:.ctx/collab/sessions/session/state.json'
