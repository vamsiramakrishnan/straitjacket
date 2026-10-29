"""Three-arm, model-free complex rename replay with actual ctx output token counts.

Manual is a scripted discovery/read/edit workflow, not an observed human or LLM.
Jedi's refactor result supplies correct edits to both non-LSP arms so they are not
penalized for mistakes or inflated repair loops. All arms use the same CAS, diff
retrieval and executable checks. Model reasoning and repeated history are excluded.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import importlib.metadata
import subprocess
import sys
import tempfile
import time

FIXTURE = {
    'billing.py': '''"""Invoice arithmetic shared by checkout and reporting."""

def compute_total(items: list[float], *, discount: float = 0.0) -> float:
    if not 0 <= discount <= 1:
        raise ValueError("discount must be between zero and one")
    return round(sum(items) * (1 - discount), 2)

def tax(total: float, rate: float = 0.2) -> float:
    return round(total * rate, 2)
''',
    'checkout.py': '''from billing import compute_total, tax

def checkout(items):
    subtotal = compute_total(items, discount=0.1)
    return subtotal + tax(subtotal)
''',
    'reporting.py': '''import billing

def revenue(batches):
    return sum(billing.compute_total(batch) for batch in batches)
''',
    'client.py': '''from billing import compute_total as subtotal

def preview(items):
    return subtotal(items, discount=0.25)
''',
    'exports.py': '''from billing import compute_total

def discounted(items):
    return compute_total(items, discount=0.5)
''',
    'worker.py': '''from exports import compute_total

def work(items):
    return compute_total(
        items,
        discount=0.2,
    )
''',
    'shadow.py': '''def local():
    compute_total = lambda values: 999
    return compute_total([1, 2])
''',
    'z_legacy.py': '''def compute_total(values):
    return -1

class Legacy:
    def compute_total(self, values):
        return -2
''',
    'notes.py': '''# compute_total remains a historical label, not a Python reference.
LABEL = "compute_total"
''',
}

VERIFY = '''import billing, checkout, reporting, client, exports, worker, shadow, z_legacy, notes
assert billing.calculate_total([10, 20]) == 30
assert not hasattr(billing, 'compute_total')
assert checkout.checkout([10, 20]) == 32.4
assert reporting.revenue([[10], [20]]) == 30
assert client.preview([10, 20]) == 22.5
assert exports.discounted([10, 20]) == 15
assert worker.work([10, 20]) == 24
assert shadow.local() == 999
assert z_legacy.compute_total([1]) == -1
assert z_legacy.Legacy().compute_total([1]) == -2
assert notes.LABEL == 'compute_total'
assert billing.tax(30) == 6
print('12 behavioral assertions passed')
'''


class Trace:
    def __init__(self, encoding):
        self.encoding, self.rows = encoding, []

    def record(self, operation, arguments, result):
        args = json.dumps({'operation': operation, 'arguments': arguments}, sort_keys=True)
        text = result if isinstance(result, str) else json.dumps(result, sort_keys=True)
        self.rows.append({'operation': operation, 'arguments': arguments, 'result': text,
                          'argument_tokens': len(self.encoding.encode(args)),
                          'result_tokens': len(self.encoding.encode(text))})


def rendered_receipt(ws, store, receipt):
    from ctx.commands.edit import _emit_result

    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        _emit_result(ws, store, receipt)
    return capture.getvalue()


def run_arm(root: Path, arm: str, encoding):
    import jedi
    from ctx import anchors
    from ctx.edit_transactions import create_edit_plan, preview_edit_plan, apply_edit_plan
    from ctx.lsp_edits import propose
    from ctx.retrieval import get, Selector
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    root.mkdir()
    (root / 'ctx.toml').write_text('version = 1\n')
    for name, text in FIXTURE.items():
        (root / name).write_text(text)
    ws = resolve_workspace(str(root))
    store = Store(ws.workspace_id)
    trace = Trace(encoding)
    start = time.monotonic()
    os.environ['CTX_LSP'] = 'off'  # Equal builtin post-edit checks in all three arms.
    try:
        if arm == 'lsp':
            result = propose(ws, store, 'billing.py', 'rename', line=3, col=5,
                             new_name='calculate_total')
            plan = result['plan']
        else:
            script = jedi.Script(path=str(root / 'billing.py'), project=jedi.Project(str(root)))
            if arm == 'manual_ctx':
                env = {**os.environ, 'CTX_LSP': 'off'}
                refs = subprocess.run([sys.executable, '-m', 'ctx', '--workspace', str(root),
                                       'refs', 'compute_total'], env=env, capture_output=True, text=True, timeout=30)
                if refs.returncode:
                    raise RuntimeError(refs.stderr)
                trace.record('ctx refs', {'symbol': 'compute_total', 'lsp': False}, refs.stdout)
            changed = script.rename(3, 4, new_name='calculate_total').get_changed_files()
            requests = []
            for path, change in sorted(changed.items(), key=lambda item: str(item[0])):
                rel = path.relative_to(root).as_posix()
                original = path.read_text()
                lines = original.splitlines()
                if arm == 'manual_ctx':
                    trace.record('ctx get', {'ref': 'repo:' + rel, 'lines': f'1:{len(lines)}'},
                                 get(store, ws, 'repo:' + rel, Selector(lines=(1, len(lines)))))
                requests.append({'path': rel, 'span': anchors.format_span(1, len(lines), anchors.anchor(lines)),
                                 'replacement': change.get_new_code()})
            plan = create_edit_plan(ws, store, {'schema': 'ctx.edit-request/v1', 'edits': requests}, exact_files=True)
        summary = {'plan': 'plan.json', 'files': [item['path'] for item in plan['edits']], 'status': 'planned'}
        args = {'target': 'billing.py:3:5', 'new_name': 'calculate_total'}
        if arm == 'manual_ctx':
            args = {'schema': 'ctx.edit-request/v1', 'edits': requests}
        trace.record('plan edits' if arm == 'manual_ctx' else arm + ' rename', args, summary)
        preview = preview_edit_plan(ws, store, plan)
        trace.record('ctx edit preview', {'plan': 'plan.json'}, rendered_receipt(ws, store, preview))
        patch = store.get_blob(preview['patch'].removeprefix('blob:')).decode()
        patch_lines = len(patch.splitlines())
        for begin in range(1, patch_lines + 1, 25):
            end = min(begin + 24, patch_lines)
            trace.record('ctx get diff', {'ref': preview['patch'], 'lines': f'{begin}:{end}'},
                         get(store, ws, preview['patch'], Selector(lines=(begin, end))))
        receipt = apply_edit_plan(ws, plan)
        trace.record('ctx edit apply', {'plan': 'plan.json'}, rendered_receipt(ws, store, receipt))
        checked = subprocess.run([sys.executable, '-c', VERIFY], cwd=root, capture_output=True, text=True, timeout=10)
        trace.record('verify', {'check': '12 behavioral assertions'}, {
            'exit': checked.returncode, 'stdout': checked.stdout, 'stderr': checked.stderr})
        distractors = all((root / name).read_text() == FIXTURE[name] for name in ('shadow.py', 'z_legacy.py', 'notes.py'))
        passed = checked.returncode == 0 and distractors
        changed_files = [item['path'] for item in plan['edits']]
        error = None
    except Exception as exc:
        passed, changed_files, error = False, [], f'{type(exc).__name__}: {exc}'
    return {'arm': arm, 'passed': passed, 'error': error, 'changed_files': changed_files,
            'seconds': round(time.monotonic() - start, 4), 'scripted_tool_calls': len(trace.rows),
            'tool_result_tokens': sum(row['result_tokens'] for row in trace.rows),
            'tool_argument_tokens': sum(row['argument_tokens'] for row in trace.rows),
            'trace': trace.rows}


def main():
    import jedi
    import tiktoken
    from ctx import lsp

    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error('repeats must be 1 to 10')
    if not lsp.server_for('python'):
        parser.error('a Python language server is required')
    encoding = tiktoken.get_encoding('cl100k_base')
    arms = ['manual_ctx', 'jedi', 'lsp']
    saved = {key: os.environ.get(key) for key in ('CTX_STATE_HOME', 'CTX_LSP', 'CTX_LSP_IDLE_S')}
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix='ctx-lsp-comparison-') as directory:
            base = Path(directory)
            os.environ['CTX_STATE_HOME'] = str(base / 'state')
            os.environ['CTX_LSP_IDLE_S'] = '2'
            for repeat in range(args.repeats):
                for arm in arms[repeat % 3:] + arms[:repeat % 3]:
                    row = run_arm(base / f'{arm}-{repeat}', arm, encoding)
                    row['repeat'] = repeat
                    rows.append(row)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    summaries = {}
    for arm in arms:
        selected = [row for row in rows if row['arm'] == arm]
        summaries[arm] = {key: statistics.median(row[key] for row in selected)
                          for key in ('seconds', 'scripted_tool_calls', 'tool_result_tokens', 'tool_argument_tokens')}
        summaries[arm]['passed'] = all(row['passed'] for row in selected)
    comparisons = {}
    for baseline in ('manual_ctx', 'jedi'):
        before, after = summaries[baseline], summaries['lsp']
        saved_tokens = before['tool_result_tokens'] - after['tool_result_tokens']
        comparisons[baseline] = {'tool_result_tokens_saved': saved_tokens,
            'tool_result_reduction_pct': round(100 * saved_tokens / before['tool_result_tokens'], 2) if before['tool_result_tokens'] else None,
            'illustrative_input_savings_per_1000_tasks_usd': {
                str(rate): round(saved_tokens * rate / 1000, 4) for rate in (1, 3, 10)},
            'rate_unit': 'USD per million uncached input tokens; scenarios, not Gemini prices'}
    report = {'schema': 'ctx.lsp-edit-comparison/v1', 'fixture_files': len(FIXTURE),
              'fixture_sha256': hashlib.sha256(json.dumps(FIXTURE, sort_keys=True).encode()).hexdigest(),
              'tokenizer': 'cl100k_base', 'jedi_version': jedi.__version__,
              'tiktoken_version': importlib.metadata.version('tiktoken'),
              'source_sha256': {name: hashlib.sha256(Path(lsp.__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ('lsp.py', 'lsp_service.py', 'lsp_edits.py', 'edit_transactions.py')},
              'repeats': args.repeats, 'summaries': summaries, 'comparisons': comparisons,
              'rows': rows, 'passed': all(row['passed'] for row in rows),
              'model_calls': 0, 'agent_turns': None, 'actual_provider_cost': None,
              'limits': ['one synthetic Python refactor', 'scripted actions are not agent turns',
                         'manual edits use Jedi to avoid penalizing the baseline for mistakes',
                         'Jedi rename is a benchmark adapter, not a newly shipped ctx command',
                         'tool arguments and results counted separately; no hidden reasoning or history replay',
                         'cl100k_base is a proxy, not the Gemini tokenizer',
                         'all arms use builtin checks; separate replay covers fresh LSP diagnostics']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'rows'}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
