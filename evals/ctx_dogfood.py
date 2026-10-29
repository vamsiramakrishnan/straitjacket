"""Run real local ctx regression workflows and retain their captured evidence.

No provider calls. This measures harness regressions, not autonomous coding success.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from ctx.collab import workspace_fingerprint
from ctx.digest import digest_output
from ctx.store import Store
from ctx.workspace import resolve_workspace

CASES = {
    'ownership-and-stale-verification': ['tests/test_ctx_collab.py', 'tests/test_collab_safety.py'],
    'events-cancellation-and-resume': ['tests/test_collab_sessions.py', 'tests/test_prewalk.py'],
    'compression-and-exact-retrieval': ['tests/test_context_compression.py', 'tests/test_block_selection.py'],
    'automatic-task-context': ['tests/test_task_context.py', 'tests/test_digest_focus_policy.py'],
    'capture-and-edit-recovery': ['tests/test_emission_gate.py', 'tests/test_edit_transactions.py'],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--session', type=Path, help='optional captured agy session directory; never launches one')
    args = parser.parse_args()
    ws = resolve_workspace(str(Path.cwd()))
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    before = workspace_fingerprint(Path(ws.root), ['src/ctx', 'tests', 'evals/ctx_dogfood.py'])
    rows = []
    for name, tests in CASES.items():
        argv = [sys.executable, '-m', 'pytest', *tests, '-q']
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=ws.root, capture_output=True, text=True, timeout=120)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            code = 124
            out = (exc.stdout or b'').decode('utf-8', errors='replace')
            err = (exc.stderr or b'').decode('utf-8', errors='replace') + '\nverification timed out'
        elapsed = time.monotonic() - started
        digest, run_id = digest_output(store, ws, ' '.join(argv), out, stderr=err)
        rows.append({'case': name, 'passed': code == 0, 'returncode': code,
                     'seconds': round(elapsed, 3), 'raw_bytes': len((out + err).encode()),
                     'visible_bytes': len(digest.encode()), 'evidence': 'run:' + run_id,
                     'tests': tests})
    report = {'schema': 'ctx.dogfood/v1', 'source_fingerprint': before,
              'source_unchanged': before == workspace_fingerprint(Path(ws.root), ['src/ctx', 'tests', 'evals/ctx_dogfood.py']),
              'cases': rows, 'passed': all(r['passed'] for r in rows),
              'measurement_scope': 'local regression workflows; not agent task-completion benchmarking',
              'agent_turns': None, 'provider_dollars': None, 'provider_tokens': None}
    if args.baseline:
        baseline = json.loads(args.baseline.read_text())
        previous = {row['case']: row for row in baseline['cases']}
        report['comparison'] = [{'case': row['case'],
            'passed_before': previous[row['case']]['passed'], 'passed_after': row['passed'],
            'visible_bytes_delta': row['visible_bytes'] - previous[row['case']]['visible_bytes'],
            'seconds_delta': round(row['seconds'] - previous[row['case']]['seconds'], 3)}
            for row in rows if row['case'] in previous]
    if args.session:
        state = json.loads((args.session / 'state.json').read_text())
        result = json.loads((args.session / 'result.json').read_text())
        report['agy_observation'] = {'session_id': state['session_id'], 'status': state['status'],
            'conversation_id': state['conversation_id'], 'task_completed': state['task_completed'],
            'reported_turns': result.get('num_turns'), 'reported_usage': result.get('usage'),
            'usage_scope': 'native conversation report; may include preceding turns'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'source_unchanged': report['source_unchanged'],
                      'cases': len(rows), 'artifact': str(args.output)}, indent=2))
    return 0 if report['passed'] and report['source_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
