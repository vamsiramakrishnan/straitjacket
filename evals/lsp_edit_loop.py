"""Local real-server replay: reuse, fresh errors, cross-file rename, verification.

No model calls. Latency and evidence size are observations, not agent turn/cost claims.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def main():
    from ctx import lsp
    from ctx.edit_transactions import apply_edit_plan, preview_edit_plan
    from ctx.lsp_edits import propose
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not lsp.server_for('python'):
        parser.error('install a Python language server before running this replay')
    previous = {name: os.environ.get(name) for name in ('CTX_LSP_REUSE', 'CTX_LSP_IDLE_S', 'CTX_STATE_HOME')}
    inputs = {name: hashlib.sha256(Path(lsp.__file__).with_name(name).read_bytes()).hexdigest()
              for name in ('lsp.py', 'lsp_service.py', 'lsp_edits.py', 'edit_transactions.py')}
    try:
        with tempfile.TemporaryDirectory(prefix='ctx-lsp-replay-') as directory:
            root = Path(directory)
            os.environ['CTX_STATE_HOME'] = str(root / 'state')
            os.environ['CTX_LSP_IDLE_S'] = '5'
            project = root / 'project'
            project.mkdir()
            (project / 'ctx.toml').write_text('version = 1\n')
            module = project / 'mod.py'
            module.write_text('def hello() -> int:\n    return 7\n')
            (project / 'other.py').write_text('from mod import hello\nassert hello() == 7\n')
            rows = []
            for reuse in ('off', 'on'):
                os.environ['CTX_LSP_REUSE'] = reuse
                for repeat in range(3):
                    start = time.monotonic()
                    result = lsp.semantic_request(project, 'mod.py', 'hover', line=1, col=5)
                    rows.append({'reuse': reuse, 'repeat': repeat, 'pid': result['pid'],
                                 'seconds': round(time.monotonic() - start, 4),
                                 'answer': result['answer'],
                                 'evidence_bytes': len(json.dumps(result['answer']).encode())})
            module.write_text('def hello() -> int:\n    return missing_name\n')
            diagnostic = lsp.semantic_request(project, 'mod.py', 'diagnostics', timeout=10)['answer']
            detects_error = diagnostic['fresh'] and any(item.get('severity') == 1 for item in diagnostic['items'])
            module.write_text('def hello() -> int:\n    return 7\n')
            ws = resolve_workspace(str(project))
            store = Store(ws.workspace_id)
            plan = propose(ws, store, 'mod.py', 'rename', line=1, col=5, new_name='greet')['plan']
            preview = preview_edit_plan(ws, store, plan)
            applied = apply_edit_plan(ws, plan)
            verification = subprocess.run([sys.executable, 'other.py'], cwd=project,
                                          capture_output=True, timeout=10)
            warm = [row for row in rows if row['reuse'] == 'on']
            cold = [row for row in rows if row['reuse'] == 'off']
            report = {'schema': 'ctx.lsp-replay/v1', 'server': result['server'],
                      'source_sha256': inputs, 'server_command': lsp.server_for('python'),
                      'queries': rows, 'warm_processes': len({row['pid'] for row in warm}),
                      'cold_processes': len({row['pid'] for row in cold}),
                      'cold_median_seconds': statistics.median(row['seconds'] for row in cold),
                      'warm_repeat_median_seconds': statistics.median(row['seconds'] for row in warm[1:]),
                      'same_answers': all(row['answer'] == rows[0]['answer'] for row in rows),
                      'fresh_error_detected': detects_error, 'renamed_files': len(plan['edits']),
                      'preview_outcome': preview['outcome'], 'apply_outcome': applied['outcome'],
                      'verification_exit': verification.returncode,
                      'agent_turns': None, 'provider_cost': None,
                      'scope': 'fixed local Python replay; not autonomous coding success'}
            report['passed'] = (report['warm_processes'] == 1 and report['same_answers'] and detects_error
                                and report['renamed_files'] == 2 and verification.returncode == 0)
            report['source_unchanged'] = all(hashlib.sha256(Path(lsp.__file__).with_name(name).read_bytes()).hexdigest() == digest
                                             for name, digest in inputs.items())
            report['passed'] = report['passed'] and report['source_unchanged']
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps({k: v for k, v in report.items() if k != 'queries'}, indent=2))
            return 0 if report['passed'] else 1
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == '__main__':
    raise SystemExit(main())
