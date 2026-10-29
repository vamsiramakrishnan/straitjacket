"""Real-server, model-free edits. Missing tools never count as passing.

python -m evals.lsp_languages --output evals/lsp-languages-local.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from ctx import lsp
from ctx.edit_transactions import EditTransactionError, apply_edit_plan, preview_edit_plan
from ctx.lsp_edits import propose
from ctx.store import Store
from ctx.workspace import resolve_workspace

FIXTURES = {
    'typescript': {
        'files': {
            'tsconfig.json': '{"compilerOptions":{"strict":true,"module":"commonjs","outDir":"dist"},"include":["*.ts"]}\n',
            'mod.ts': 'export function answer(): number { return 7; }\n',
            'main.ts': 'import { answer as value } from "./mod";\nif (value() !== 7) throw new Error("wrong answer");\n',
            'unrelated.ts': 'export const answer = "answer"; // answer must stay untouched\n',
        }, 'target': 'mod.ts', 'line': 1, 'col': 17,
        'bad': 'export function answer(): number { return "wrong"; }\n',
        'verify': [['tsc', '--project', '.'], ['node', 'dist/main.js']],
        'version': ['typescript-language-server', '--version'],
        'compiler_version': ['tsc', '--version'],
    },
    'go': {
        'files': {
            'go.mod': 'module example.com/ctxreplay\n\ngo 1.22\n',
            'mod.go': 'package main\nfunc answer() int { return 7 }\n',
            'main.go': 'package main\nfunc main() { if answer() != 7 { panic("wrong answer") } }\n',
            'unrelated.go': 'package main\n// answer must stay untouched\nfunc unrelated() string { answer := "answer"; return answer }\n',
        }, 'target': 'mod.go', 'line': 2, 'col': 6,
        'bad': 'package main\nfunc answer() int { return "wrong" }\n',
        'verify': [['go', 'run', '.']], 'version': ['gopls', 'version'],
        'compiler_version': ['go', 'version'],
    },
    'rust': {
        'files': {
            'Cargo.toml': '[package]\nname = "ctx-replay"\nversion = "0.1.0"\nedition = "2021"\n',
            'src/mod.rs': 'pub fn answer() -> i32 { 7 }\n',
            'src/main.rs': '#[path = "mod.rs"] mod values;\nmod unrelated;\nfn main() { assert_eq!(values::answer(), 7); }\n',
            'src/unrelated.rs': '#[allow(dead_code)]\npub fn answer() -> &\'static str { "answer" } // answer must stay untouched\n',
        }, 'target': 'src/mod.rs', 'line': 1, 'col': 8,
        'bad': 'pub fn answer() -> i32 { missing_name() }\n',
        'verify': [['cargo', 'run', '--offline', '--quiet']],
        'version': ['rust-analyzer', '--version'], 'compiler_version': ['rustc', '--version'],
    },
}


def command(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    return {'argv': argv, 'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


def run_language(language: str, root: Path) -> dict:
    fixture = FIXTURES[language]
    row = {'language': language, 'passed': False, 'agent_turns': None, 'provider_cost': None}
    started = time.monotonic()
    try:
        row['server_command'] = lsp.server_for(language)
        commands = [fixture['version'], fixture['compiler_version'], *fixture['verify']]
        missing = [cmd[0] for cmd in commands if not shutil.which(cmd[0])]
        if not row['server_command'] or missing:
            row.update(status='unavailable', reason=f'missing executables: {missing}')
            return row
        root.mkdir()
        for name, content in fixture['files'].items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (root / 'ctx.toml').write_text('version = 1\n')
        row['fixture_sha256'] = {name: hashlib.sha256(content.encode()).hexdigest()
                                  for name, content in fixture['files'].items()}
        row['server_version'] = command(fixture['version'], root)
        row['compiler_version'] = command(fixture['compiler_version'], root)
        row['baseline'] = [command(cmd, root) for cmd in fixture['verify']]
        if any(run['exit'] for run in [row['server_version'], row['compiler_version'], *row['baseline']]):
            row.update(status='failed', reason='tool/version or baseline execution failed')
            return row
        ws = resolve_workspace(str(root))
        store = Store(ws.workspace_id)
        rel = fixture['target']
        target = root / rel
        before = target.read_bytes()
        target.write_text(fixture['bad'])
        row['broken_compiler_check'] = command(fixture['verify'][0], root)
        row['diagnostics'] = lsp.semantic_request(root, rel, 'diagnostics', timeout=30)['answer']
        target.write_bytes(before)
        row['clean_diagnostics'] = lsp.semantic_request(root, rel, 'diagnostics', timeout=30)['answer']
        proposal = propose(ws, store, rel, 'rename', line=fixture['line'], col=fixture['col'],
                           new_name='renamed_answer', timeout=30)
        plan = proposal['plan']
        row['preview'] = preview_edit_plan(ws, store, plan)['outcome']
        row['renamed_files'] = sorted({edit['path'] for edit in plan['edits']})
        # Drift outside the renamed token must reject the whole transaction.
        target.write_bytes(before + b'\n')
        drifted = {name: (root / name).read_bytes() for name in fixture['files']}
        try:
            apply_edit_plan(ws, plan)
            row['stale_edit_refused'] = False
        except EditTransactionError as exc:
            row['stale_edit_refused'] = True
            row['stale_refusal'] = str(exc)
        row['refusal_wrote_nothing'] = all((root / name).read_bytes() == content for name, content in drifted.items())
        target.write_bytes(before)
        row['apply'] = apply_edit_plan(ws, plan)['outcome']
        row['verification'] = [command(cmd, root) for cmd in fixture['verify']]
        row['unrelated_unchanged'] = all((root / name).read_text() == content
                                       for name, content in fixture['files'].items() if 'unrelated' in name)
        bad, clean = row['diagnostics'], row['clean_diagnostics']
        row['fresh_error_detected'] = bad.get('fresh') and any(item.get('severity') == 1 for item in bad['items'])
        row['fresh_error_cleared'] = clean.get('fresh') and not any(item.get('severity') == 1 for item in clean['items'])
        row['edit_passed'] = bool(row['broken_compiler_check']['exit'] != 0
                             and len(row['renamed_files']) == 2 and row['preview'] == 'ready'
                             and row['stale_edit_refused'] and row['refusal_wrote_nothing']
                             and row['apply'] == 'applied' and row['unrelated_unchanged']
                             and all(run['exit'] == 0 for run in row['verification']))
        if row['fresh_error_detected'] and row['fresh_error_cleared']:
            row['diagnostic_status'] = 'passed'
        elif (not bad.get('fresh') and not clean.get('fresh') and all(
                result.get('reason') == 'server omitted diagnostic version' for result in (bad, clean))
                and any(item.get('severity') == 1 for item in bad['items'])
                and not any(item.get('severity') == 1 for item in clean['items'])):
            row['diagnostic_status'] = 'unavailable_unversioned'
        else:
            row['diagnostic_status'] = 'failed'
        row['passed'] = row['edit_passed'] and row['diagnostic_status'] != 'failed'
        row['status'] = 'passed' if row['passed'] else 'failed'
    except Exception as exc:
        row.update(status='failed', reason=f'{type(exc).__name__}: {exc}')
    finally:
        row['seconds'] = round(time.monotonic() - started, 3)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--languages', nargs='+', choices=FIXTURES, default=list(FIXTURES))
    parser.add_argument('--require-fresh-diagnostics', action='store_true',
                        help='fail even when a server explicitly omits diagnostic versions')
    args = parser.parse_args()
    previous = {key: os.environ.get(key) for key in ('CTX_STATE_HOME', 'CTX_LSP_REUSE', 'CTX_LSP_IDLE_S')}
    source = Path(lsp.__file__).parent
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
              for name in ('lsp.py', 'lsp_service.py', 'lsp_edits.py', 'edit_transactions.py')}
    try:
        with tempfile.TemporaryDirectory(prefix='ctx-lsp-languages-') as tmp:
            root = Path(tmp)
            os.environ.update(CTX_STATE_HOME=str(root / 'state'), CTX_LSP_REUSE='on', CTX_LSP_IDLE_S='5')
            rows = [run_language(language, root / language) for language in args.languages]
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    unchanged = all(hashlib.sha256((source / name).read_bytes()).hexdigest() == sha for name, sha in hashes.items())
    report = {'schema': 'ctx.lsp-languages/v1', 'scope': 'fixed local fixtures; no model calls or autonomous agent trial',
              'source_sha256': hashes, 'source_unchanged': unchanged, 'languages': rows,
              'fresh_diagnostics_required': args.require_fresh_diagnostics,
              'passed': unchanged and all(row['passed'] and (not args.require_fresh_diagnostics
                                        or row.get('diagnostic_status') == 'passed') for row in rows)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'languages': [
        {key: row.get(key) for key in ('language', 'status', 'diagnostic_status', 'reason', 'seconds')} for row in rows]}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
