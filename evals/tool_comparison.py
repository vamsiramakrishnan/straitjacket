"""Pinned, model-free tool workflows on common fixtures and acceptance checks.

This is a scripted capability replay, not an agent leaderboard. No model is
called; usage/cost/turn savings remain unmeasured. Native edit operations are
used where available. A compressor is not silently given an oracle editor.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from evals.lsp_edit_comparison import FIXTURE, VERIFY
from ctx.session_economics import summarize

TASKS = ('rename', 'bugfix', 'noisy_logs')
FIXED_BODY = '''def compute_total(items: list[float], *, discount: float = 0.0) -> float:
    if not 0 <= discount <= 1:
        raise ValueError("discount must be between zero and one")
    return round(sum(items) * (1 - discount), 2)'''
BUG_VERIFY = '''from billing import compute_total, tax
assert compute_total([10, 20], discount=0.25) == 22.5
assert compute_total([], discount=0.5) == 0
assert compute_total([10], discount=0) == 10
assert tax(30) == 6
for invalid in (-0.1, 1.1):
    try: compute_total([10], discount=invalid)
    except ValueError: pass
    else: raise AssertionError('invalid discount accepted')
print('6 behavioral checks passed')
'''
LOG_NEEDLE = 'request=quiet-request-731 outcome=duplicate-charge source=billing.py:6'
LOGS = '\n'.join(LOG_NEEDLE if n == 1731 else f'INFO request=req-{n:05d} operation=health status=ok latency_ms={n % 97}' for n in range(4000)) + '\n'


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def files_for(task):
    if task == 'noisy_logs':
        return {'application.log': LOGS}
    files = dict(FIXTURE)
    if task == 'bugfix':
        files['billing.py'] = files['billing.py'].replace('(1 - discount)', '(1 + discount)')
    return files


def acceptance(task, root, outputs):
    if task == 'noisy_logs':
        return {'passed': LOG_NEEDLE in '\n'.join(outputs), 'criterion': 'exact quiet failure record recovered'}
    code = VERIFY if task == 'rename' else BUG_VERIFY
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=root, capture_output=True, text=True, timeout=20)
    unchanged = all((root / name).read_text() == FIXTURE[name] for name in ('shadow.py', 'z_legacy.py', 'notes.py'))
    return {'passed': result.returncode == 0 and unchanged, 'exit_code': result.returncode,
            'stdout': result.stdout, 'stderr': result.stderr, 'distractors_unchanged': unchanged}


class Trace:
    def __init__(self, encoding):
        self.encoding, self.events, self.outputs = encoding, [], []

    def record(self, operation, args, output, *, retrieval=False, retry=False):
        text = output if isinstance(output, str) else json.dumps(output, sort_keys=True)
        self.outputs.append(text)
        self.events.append({'schema': 'ctx.session-event/v1', 'kind': 'tool', 'id': f'tool-{len(self.events)}',
                            'operation': operation, 'arguments': args, 'result': text,
                            'argument_tokens': len(self.encoding.encode(json.dumps(args, sort_keys=True))),
                            'result_tokens': len(self.encoding.encode(text)), 'retrieval': retrieval, 'retry': retry})
        return text

    def schemas(self, schemas):
        text = json.dumps(schemas, sort_keys=True)
        self.events.append({'schema': 'ctx.session-event/v1', 'kind': 'tool_schema', 'id': f'schema-{len(self.events)}',
                            'tokens': len(self.encoding.encode(text)), 'sha256': sha(text)})


def run_command(argv, root, env, timeout=90):
    result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
    return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


def ctx_adapter(task, root, trace, pin, env):
    from ctx import anchors
    env = {**env, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    def call(arguments, *, request=None, retrieval=False):
        result = run_command([sys.executable, '-m', 'ctx', '--workspace', str(root), *arguments], root, env)
        trace.record('ctx ' + arguments[0], {'argv': arguments, 'request': request}, result, retrieval=retrieval)
        if result['exit_code']:
            raise RuntimeError(result['stderr'] or result['stdout'])
        return result
    if task == 'noisy_logs':
        result = call(['run', '--', sys.executable, '-c', "from pathlib import Path; print(Path('application.log').read_text(),end='')"])
        if LOG_NEEDLE not in result['stdout']:
            match = re.search(r'run:([a-f0-9]+)', result['stdout'])
            if match:
                call(['search', 'run:' + match[1] + '#stdout', 'quiet-request-731'], retrieval=True)
        return
    if task == 'rename':
        call(['lsp', 'rename', 'billing.py:3:5', '--new-name', 'calculate_total', '--out', 'plan.json'])
    else:
        call(['get', 'repo:billing.py', '--lines', '1:10'], retrieval=True)
        lines = (root / 'billing.py').read_text().splitlines()
        request = {'schema': 'ctx.edit-request/v1', 'edits': [{'path': 'billing.py',
                    'span': anchors.format_span(3, 6, anchors.anchor(lines[2:6])), 'replacement': FIXED_BODY}]}
        (root / 'request.json').write_text(json.dumps(request))
        call(['edit', 'plan', 'request.json', '--out', 'plan.json'], request=request)
    call(['edit', 'preview', 'plan.json'])
    call(['edit', 'apply', 'plan.json'])


def tokensave_adapter(task, root, trace, pin, env):
    binary = pin['command'][0]
    # No agent integration/global git hooks; disable update checks and outbound
    # HTTP including the aggregate counter for this isolated replay process.
    env = {**env, 'TOKENSAVE_UPDATE_CHECK': 'off', 'HTTP_PROXY': 'http://127.0.0.1:9',
           'HTTPS_PROXY': 'http://127.0.0.1:9', 'ALL_PROXY': 'http://127.0.0.1:9'}
    setup = run_command([binary, 'init', '--no-git-hook', '--cpu-usage', 'low', str(root)], root, env)
    if setup['exit_code']:
        raise RuntimeError('tokensave init failed: ' + setup['stderr'])
    def call(name, args):
        result = run_command([binary, 'tool', name, '--project', str(root), '--args', json.dumps(args)], root, env)
        trace.record('tokensave ' + name, args, result, retrieval=name in ('search', 'body', 'read'))
        if result['exit_code']:
            raise RuntimeError(f'tokensave {name} exited {result["exit_code"]}')
        return result
    if task == 'rename':
        found = call('search', {'query': 'compute_total', 'format': 'json', 'ids': True, 'limit': 30})
        data = json.loads(found['stdout'])
        # Select the definition by file rather than ambiguous bare symbol name.
        def nodes(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from nodes(child)
            elif isinstance(value, list):
                for child in value:
                    yield from nodes(child)
        selected = next((n for n in nodes(data) if str(n.get('file_path', n.get('file', n.get('path', '')))).endswith('billing.py')
                         and n.get('name') == 'compute_total'), None)
        if selected is None:
            raise RuntimeError('definition not found in tokensave search response')
        args = {'new_name': 'calculate_total', 'dry_run': True}
        if selected.get('id') or selected.get('node_id'):
            args['node_id'] = selected.get('id') or selected['node_id']
        else:
            args['symbol'] = selected['qualified_name']
        call('rename', args)
        call('rename', {**args, 'dry_run': False})
    elif task == 'bugfix':
        call('body', {'symbol': 'billing.py::billing.py::compute_total'})
        call('replace_symbol', {'symbol': 'billing.py::billing.py::compute_total', 'new_source': FIXED_BODY})
    else:
        result = call('search', {'query': 'quiet-request-731', 'literal': True})
        if LOG_NEEDLE not in result['stdout']:
            call('read', {'file': 'application.log', 'mode': 'full'})


async def serena_adapter_async(task, root, trace, pin, env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    command = pin['command'] + ['start-mcp-server', '--project', str(root), '--context', 'desktop-app',
                               '--agent-interface', 'tools', '--enable-web-dashboard', 'false',
                               '--open-web-dashboard', 'false', '--enable-gui-log-window', 'false']
    env = {**env, 'SERENA_HOME': str(root.parent / 'serena-state')}
    with (root.parent / 'serena-stderr.log').open('w') as errlog:
        async with stdio_client(StdioServerParameters(command=command[0], args=command[1:], env=env), errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                trace.schemas([{'name': t.name, 'description': t.description, 'inputSchema': t.inputSchema} for t in listed.tools])
                async def call(name, args, retrieval=False):
                    result = await session.call_tool(name, args)
                    text = '\n'.join(getattr(c, 'text', '') for c in result.content)
                    trace.record('serena ' + name, args, text, retrieval=retrieval)
                    if result.isError:
                        raise RuntimeError(text)
                    return text
                await call('initial_instructions', {})
                if task == 'rename':
                    await call('find_symbol', {'name_path_pattern': 'compute_total', 'relative_path': 'billing.py', 'include_body': True}, True)
                    await call('rename_symbol', {'name_path': 'compute_total', 'relative_path': 'billing.py', 'new_name': 'calculate_total'})
                elif task == 'bugfix':
                    await call('find_symbol', {'name_path_pattern': 'compute_total', 'relative_path': 'billing.py', 'include_body': True}, True)
                    await call('replace_symbol_body', {'name_path': 'compute_total', 'relative_path': 'billing.py', 'body': FIXED_BODY})
                else:
                    await call('search_for_pattern', {'substring_pattern': 'quiet-request-731', 'relative_path': 'application.log'}, True)


def headroom_adapter(task, root, trace, pin, env):
    payload = {'messages': [{'role': 'user', 'content': 'Find the full record for quiet-request-731.'},
                           {'role': 'tool', 'tool_call_id': 'logs', 'content': LOGS}]}
    path = root / 'input.json'
    path.write_text(json.dumps(payload))
    # Structural-only explicitly disables hosted/local ML; only new tool output.
    code = """import json,re,sys
from headroom import compress,CompressConfig
from headroom.cache.compression_store import get_compression_store
p=json.load(open(sys.argv[1]))
r=compress(p['messages'],model='gpt-4o',config=CompressConfig(protect_recent=0,kompress_model='disabled'))
print(json.dumps({'operation':'compress','result':{'messages':r.messages,'transforms':r.transforms_applied}}))
text=json.dumps(r.messages)
for key in dict.fromkeys(re.findall(r'hash=([a-f0-9]+)',text)):
    entry=get_compression_store().retrieve(key,query='quiet-request-731')
    print(json.dumps({'operation':'retrieve','hash':key,'result':entry.original_content if entry else None}))
"""
    result = run_command(pin['command'] + ['-c', code, str(path)], root, env, timeout=120)
    if result['exit_code']:
        trace.record('headroom error', {}, result)
        raise RuntimeError(result['stderr'])
    for line in result['stdout'].splitlines():
        record = json.loads(line)
        trace.record('headroom ' + record['operation'],
                     {'hash': record['hash']} if record['operation'] == 'retrieve' else {'protect_recent': 0, 'kompress_model': 'disabled'},
                     record['result'], retrieval=record['operation'] == 'retrieve')


def package_hash(root):
    root = Path(root)
    return sha(json.dumps({str(p.relative_to(root)): sha(p.read_bytes())
                           for p in sorted(root.rglob('*.py'))}, sort_keys=True))


def check_pin(pin):
    if pin.get('unavailable'):
        return pin['unavailable']
    executable = Path(pin['command'][0])
    if not executable.is_file():
        return 'configured executable unavailable'
    if pin.get('binary_sha256') and sha(executable.read_bytes()) != pin['binary_sha256']:
        return 'binary hash differs from pin'
    if pin.get('package_root') and package_hash(pin['package_root']) != pin['package_sha256']:
        return 'installed Python package differs from pin'
    if pin.get('repo'):
        actual = subprocess.check_output(['git', '-C', pin['repo'], 'rev-parse', 'HEAD'], text=True).strip()
        dirty = subprocess.check_output(['git', '-C', pin['repo'], 'status', '--porcelain', '--untracked-files=no'], text=True).strip()
        if actual != pin['revision'] or dirty:
            return 'source revision differs from pin or tracked checkout is dirty'
    return None


def source_hashes():
    root = Path(__file__).resolve().parents[1]
    paths = [Path(__file__), root / 'evals' / 'lsp_edit_comparison.py',
             *sorted((root / 'src' / 'ctx').rglob('*.py'))]
    return {str(p.relative_to(root)): sha(p.read_bytes()) for p in paths}


def evaluate(pins, *, repeats=1, selected=None):
    import tiktoken
    observed_sources = source_hashes()
    encoding = tiktoken.get_encoding('cl100k_base')
    rows = []
    prior = {k: os.environ.get(k) for k in ('CTX_STATE_HOME', 'CTX_LSP', 'CTX_LSP_IDLE_S')}
    try:
        with tempfile.TemporaryDirectory(prefix='ctx-tool-comparison-') as directory:
            base = Path(directory)
            os.environ['CTX_STATE_HOME'] = str(base / 'ctx-state')
            os.environ['CTX_LSP_IDLE_S'] = '2'
            for repeat in range(repeats):
                arms = list(selected or pins)
                arms = arms[repeat % len(arms):] + arms[:repeat % len(arms)]
                for arm in arms:
                    pin = pins[arm]
                    missing = check_pin(pin)
                    for task in TASKS:
                        files = files_for(task)
                        identity = {'fixture_sha256': sha(json.dumps(files, sort_keys=True)),
                                    'acceptance_sha256': sha(VERIFY if task == 'rename' else BUG_VERIFY if task == 'bugfix' else LOG_NEEDLE),
                                    'model': None, 'host': 'scripted-offline', 'budget': 180}
                        row = {'arm': arm, 'task': task, 'repeat': repeat, 'pin': pin, 'identity': identity}
                        if missing or (arm == 'headroom' and task != 'noisy_logs'):
                            rows.append({**row, 'status': 'unavailable' if missing else 'unsupported',
                                         'reason': missing or 'compressor has no native edit operation; no oracle editor supplied',
                                         'acceptance': None, 'economics': summarize([])})
                            continue
                        root = base / f'{repeat}-{arm}-{task}' / 'project'
                        root.mkdir(parents=True)
                        for name, text in files.items():
                            (root / name).write_text(text)
                        (root / 'ctx.toml').write_text('version = 1\n')
                        subprocess.run(['git', 'init', '-q', str(root)], check=True)
                        trace = Trace(encoding)
                        start = time.monotonic()
                        error = None
                        try:
                            if arm == 'serena':
                                asyncio.run(asyncio.wait_for(serena_adapter_async(task, root, trace, pin, dict(os.environ)), timeout=180))
                            else:
                                {'ctx': ctx_adapter, 'tokensave': tokensave_adapter, 'headroom': headroom_adapter}[arm](task, root, trace, pin, dict(os.environ))
                        except Exception as exc:
                            error = f'{type(exc).__name__}: {exc}'
                        checked = acceptance(task, root, trace.outputs)
                        trace.record('verify', {'task': task}, checked)
                        passed = checked['passed'] and error is None
                        refused = arm == 'tokensave' and task == 'rename' and not passed and any('refus' in text for text in trace.outputs)
                        rows.append({**row, 'status': 'passed' if passed else 'refused' if refused else 'failed', 'error': error,
                                     'seconds': round(time.monotonic() - start, 4), 'acceptance': checked,
                                     'economics': summarize(trace.events, accepted=passed), 'events': trace.events})
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if source_hashes() != observed_sources:
        raise RuntimeError('comparison implementation changed during replay; discard and rerun')
    return {'schema': 'ctx.tool-comparison/v1', 'mode': 'scripted-offline',
            'tokenizer': {'name': 'cl100k_base', 'version': importlib.metadata.version('tiktoken')},
            'runner_sha256': observed_sources['evals/tool_comparison.py'],
            'ctx_source_sha256': {k: v for k, v in observed_sources.items() if k.startswith('src/ctx/')},
            'source_unchanged': True,
            'fixture_source_sha256': observed_sources['evals/lsp_edit_comparison.py'],
            'rows': rows,
            'limitations': ['No model calls, billed usage, agent turns or whole-session savings measured.',
                            'Fixed task-specific script knows symbol/file and repair; not a discovery or reasoning benchmark.',
                            'Adapters expose different APIs; payload sizes are not a ranking of agent quality.',
                            'Headroom structural-only mode; ML disabled. No native editor, so editing tasks unsupported.',
                            'Tool schema tokens recorded when observed; absent schema observations stay null.',
                            'Cold setup included in elapsed time but index/server startup is not a model tool call.',
                            'Serena MCP protocol envelope omitted; content text retained. CLI stdout/stderr retained. Different host serialization prevents a token-efficiency ranking.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pins', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--arms', nargs='+')
    parser.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error('repeats must be 1..10')
    report = evaluate(json.loads(args.pins.read_text()), repeats=args.repeats, selected=args.arms)
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps([{k: r.get(k) for k in ('arm', 'task', 'status', 'error', 'seconds')} for r in report['rows']], indent=2))


if __name__ == '__main__':
    main()
