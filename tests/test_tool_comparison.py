import json

from evals.tool_comparison import acceptance, check_pin, files_for, FIXED_BODY, LOG_NEEDLE, Trace


def test_bug_fixture_really_fails_then_passes(tmp_path):
    for name, text in files_for('bugfix').items():
        (tmp_path / name).write_text(text)
    assert not acceptance('bugfix', tmp_path, [])['passed']
    text = (tmp_path / 'billing.py').read_text().replace('(1 + discount)', '(1 - discount)')
    (tmp_path / 'billing.py').write_text(text)
    assert acceptance('bugfix', tmp_path, [])['passed']
    (tmp_path / 'notes.py').write_text('LABEL = "oops"')
    assert not acceptance('bugfix', tmp_path, [])['passed']


def test_log_acceptance_requires_full_evidence_not_only_handle(tmp_path):
    assert not acceptance('noisy_logs', tmp_path, ['hash=abc'])['passed']
    assert not acceptance('noisy_logs', tmp_path, ['quiet-request-731'])['passed']
    assert acceptance('noisy_logs', tmp_path, [LOG_NEEDLE])['passed']


def test_changed_executable_refused(tmp_path):
    binary = tmp_path / 'tool'
    binary.write_bytes(b'changed')
    assert check_pin({'command': [str(binary)], 'binary_sha256': 'wrong'}) == 'binary hash differs from pin'
    assert check_pin({'unavailable': 'license missing'}) == 'license missing'


def test_trace_counts_actual_serialized_response():
    class Encoding:
        def encode(self, text):
            return list(text)
    trace = Trace(Encoding())
    response = {'content': 'actual tool output', 'isError': False}
    trace.record('tool', {'q': 'x'}, response)
    assert trace.events[0]['result_tokens'] == len(json.dumps(response, sort_keys=True))
