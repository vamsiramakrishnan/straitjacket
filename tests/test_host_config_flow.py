"""Acceptance evidence: isolated config fixtures, no agent or user settings."""
import json
import os
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import make_ws
from ctx.config_edit import ConfigError, Document, JSONSource
from ctx.host_config import LEDGER, plan_configuration

EXE = shlex.join([sys.executable, '-m', 'ctx'])


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def apply(root, hosts=('codex', 'opencode'), **kw):
    plan = plan_configuration(root, hosts, exe=EXE, **kw)
    assert not plan.blockers, plan.render()
    plan.apply()
    return plan


def test_preview_is_read_only_and_apply_matches_the_plan(workspace_dir):
    before = snapshot(workspace_dir)
    plan = plan_configuration(workspace_dir, ['codex', 'opencode'], exe=EXE)
    assert not plan.blockers, plan.render()
    assert '--- /dev/null' in plan.render()
    assert snapshot(workspace_dir) == before
    plan.apply()
    for change in plan.changes:
        assert (workspace_dir / change.path).read_bytes() == change.after
    installed = snapshot(workspace_dir)
    assert not apply(workspace_dir).changes
    assert snapshot(workspace_dir) == installed
    apply(workspace_dir, remove=True)
    assert snapshot(workspace_dir) == before


TOML_FIXTURES = [
    '# user note\nmodel = "user-choice"\n',
    '# comment\n[features] # features note\nother = true # inline\n[mcp_servers.other]\ncommand = "keep"\n',
    '[features.experimental]\nenabled = true\n[mcp_servers."other.with.dots"]\ncommand = "keep"\n',
    'features.other = true\nmcp_servers.other.command = "keep"\n',
    'features = { other = true } # keep\nmcp_servers = { other = { command = "keep" } }\n',
    '["features"]\n"other.key" = true\n["mcp_servers"."other"]\nargs = [\n"a", # comment\n"b",\n]\n',
    'message = """\n[features]\nhooks = false\n# not syntax\n"""\n[[unrelated]]\nx = "keep"\n',
    "message = '''\n[mcp_servers.ctx-harness]\ncommand = 'not syntax'\n'''\n",
    '# windows\r\n[features]\r\nother = true\r\n',
]


@pytest.mark.parametrize('original', TOML_FIXTURES)
def test_codex_merges_structures_without_touching_unrelated_bytes(workspace_dir, original):
    path = workspace_dir / '.codex/config.toml'
    path.parent.mkdir()
    path.write_bytes(original.encode())
    before = tomllib.loads(original)
    plan = apply(workspace_dir, ['codex'])
    assert plan.requires_review
    text = path.read_bytes().decode()
    # All existing source bytes occur in the same order. Only insertions made.
    i = 0
    for char in text:
        if i < len(original) and char == original[i]:
            i += 1
    assert i == len(original)
    data = tomllib.loads(text)
    assert data['features'].pop('hooks') is True
    server = data['mcp_servers'].pop('ctx-harness')
    assert server['args'][-2:] == ['mcp', '--bounded-only']
    if not before.get('features'):
        data.pop('features')
    if not before.get('mcp_servers'):
        data.pop('mcp_servers')
    assert data == before
    after = path.read_bytes()
    assert not apply(workspace_dir, ['codex']).changes
    assert path.read_bytes() == after
    apply(workspace_dir, ['codex'], remove=True)
    assert path.read_bytes() == original.encode()


JSONC_FIXTURES = [
    '{ // comment with } and ,\n "model": "keep",\n}',
    '{"mcp": { /* integrations */ "other": {"url": "https://example.invalid/a//b"}, }, "model": "keep"}',
    '{"mcp": {}, /* preserve */ "plugin": ["other-plugin"]}',
    '{\r\n // windows\r\n "model": "keep"\r\n}',
    '{"model": "keep" // trailing comma belongs before this comment\n}',
    '{/* only a comment */}',
    '{}',
]


@pytest.mark.parametrize('original', JSONC_FIXTURES)
@pytest.mark.parametrize('name', ['opencode.json', 'opencode.jsonc'])
def test_opencode_merges_jsonc_comments_and_integrations(workspace_dir, original, name):
    path = workspace_dir / name
    path.write_bytes(original.encode())
    before = JSONSource(original).data
    apply(workspace_dir, ['opencode'])
    text = path.read_bytes().decode()
    assert not (workspace_dir / ('opencode.json' if name.endswith('jsonc') else 'opencode.jsonc')).exists()
    after = JSONSource(text).data
    assert after['mcp'].pop('ctx-harness')['enabled'] is True
    if 'mcp' not in before:
        after.pop('mcp')
    assert after == before
    for comment in ['// comment with } and ,', '/* integrations */', '/* preserve */', '// windows', '// trailing comma belongs before this comment', '/* only a comment */']:
        if comment in original:
            assert comment in text
    assert not apply(workspace_dir, ['opencode']).changes
    apply(workspace_dir, ['opencode'], remove=True)
    assert path.read_bytes() == original.encode()


@pytest.mark.parametrize('host,name,original,field', [
    ('codex', '.codex/config.toml', '[features]\nhooks = false # explicit choice\n', 'features.hooks'),
    ('codex', '.codex/config.toml', '[mcp_servers.ctx-harness]\ncommand = "my-server"\nargs = []\n', 'mcp_servers.ctx-harness.command'),
    ('codex', '.codex/config.toml', 'features = false\n', 'features'),
    ('opencode', 'opencode.jsonc', '{"mcp": {"ctx-harness": {"enabled": false}}}', 'mcp.ctx-harness.enabled'),
    ('opencode', 'opencode.jsonc', '{"mcp": []}', 'mcp'),
])
def test_explicit_conflict_gets_exact_diff_and_cannot_be_forced(workspace_dir, host, name, original, field):
    path = workspace_dir / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(original)
    before = snapshot(workspace_dir)
    plan = plan_configuration(workspace_dir, [host], exe=EXE)
    assert plan.blockers
    output = plan.render()
    assert field in output
    assert '--- a/' + name in output and '+++ b/' + name in output
    assert '--apply will not override' in output
    with pytest.raises(ConfigError):
        plan.apply()
    assert snapshot(workspace_dir) == before


@pytest.mark.parametrize('host,name,content', [
    ('codex', '.codex/config.toml', '[features\nhooks=true'),
    ('codex', '.codex/config.toml', '[features]\nhooks=true\nhooks=false\n'),
    ('opencode', 'opencode.jsonc', '{/* unterminated'),
    ('opencode', 'opencode.jsonc', '{"mcp": {}, "mcp": {}}'),
    ('opencode', 'opencode.jsonc', '[]'),
    ('codex', '.codex/hooks.json', '{"hooks": []}'),
])
def test_malformed_or_ambiguous_input_refuses_without_writes(workspace_dir, host, name, content):
    path = workspace_dir / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    before = snapshot(workspace_dir)
    plan = plan_configuration(workspace_dir, [host], exe=EXE)
    assert plan.blockers
    with pytest.raises(ConfigError):
        plan.apply()
    assert snapshot(workspace_dir) == before


def test_codex_repairs_each_missing_lifecycle_stage_and_doctor_detects_it(workspace_dir, monkeypatch):
    import ctx.installer as installer
    monkeypatch.setattr(installer, '_ctx_executable', lambda: EXE)
    apply(workspace_dir, ['codex'])
    path = workspace_dir / '.codex/hooks.json'
    original = json.loads(path.read_text())
    for stage in original['hooks']:
        partial = json.loads(json.dumps(original))
        partial['hooks'].pop(stage)
        partial['hooks']['UserStage'] = [{'hooks': [{'type': 'command', 'command': 'other-integration'}]}]
        path.write_text(json.dumps(partial))
        checks = {n: (ok, detail) for n, ok, detail in installer.doctor_checks(make_ws(workspace_dir))}
        assert checks['codex hooks'][0] is False
        assert stage in checks['codex hooks'][1]
        apply(workspace_dir, ['codex'])
        result = json.loads(path.read_text())
        assert result['hooks'][stage] == original['hooks'][stage]
        assert result['hooks']['UserStage'] == partial['hooks']['UserStage']
        assert installer._codex_hooks_contract(path, EXE)[0]


def test_owned_executable_upgrade_repairs_without_owning_user_settings(workspace_dir):
    path = workspace_dir / '.codex/config.toml'
    path.parent.mkdir()
    original = '# own comment\n[features]\nother = true\n'
    path.write_text(original)
    apply(workspace_dir, ['codex'])
    plan = plan_configuration(workspace_dir, ['codex'], exe='/new/env/ctx')
    assert not plan.blockers, plan.render()
    plan.apply()
    assert tomllib.loads(path.read_text())['mcp_servers']['ctx-harness']['command'] == '/new/env/ctx'
    assert '# own comment' in path.read_text()
    remove = plan_configuration(workspace_dir, ['codex'], remove=True)
    assert not remove.blockers, remove.render()
    remove.apply()
    assert path.read_text() == original


def test_user_modification_of_owned_value_is_never_repaired_away(workspace_dir):
    apply(workspace_dir, ['codex'])
    path = workspace_dir / '.codex/config.toml'
    path.write_text(path.read_text().replace('hooks = true', 'hooks = false'))
    before = snapshot(workspace_dir)
    for remove in (False, True):
        plan = plan_configuration(workspace_dir, ['codex'], exe=EXE, remove=remove)
        assert plan.blockers
        with pytest.raises(ConfigError):
            plan.apply()
    assert snapshot(workspace_dir) == before


@pytest.mark.parametrize('host', ['codex', 'opencode'])
def test_removal_preserves_settings_added_after_install(workspace_dir, host):
    apply(workspace_dir, [host])
    name = '.codex/config.toml' if host == 'codex' else 'opencode.json'
    path = workspace_dir / name
    if host == 'codex':
        path.write_text(path.read_text() + '\n[my.integration]\nvalue = "retain" # later note\n')
    else:
        doc = Document(path.read_text(), 'json')
        path.write_text(doc.edit(('other',), {'value': 'retain'}).text)
    plan = plan_configuration(workspace_dir, [host], remove=True)
    assert not plan.blockers, plan.render()
    plan.apply()
    text = path.read_text()
    assert 'retain' in text
    data = tomllib.loads(text) if host == 'codex' else JSONSource(text).data
    assert 'ctx-harness' not in json.dumps(data)
    assert not (workspace_dir / LEDGER).exists()
    assert not plan_configuration(workspace_dir, [host], remove=True).changes


def test_preexisting_matching_configuration_is_not_claimed_or_removed(workspace_dir):
    apply(workspace_dir, ['codex'])
    (workspace_dir / LEDGER).unlink()
    before = snapshot(workspace_dir)
    assert not apply(workspace_dir, ['codex']).changes
    assert not plan_configuration(workspace_dir, ['codex'], remove=True).changes
    assert snapshot(workspace_dir) == before


@pytest.mark.parametrize('relative', ['.codex/config.toml', '.codex/hooks.json', 'AGENTS.md', 'opencode.jsonc', '.ctx/host-config.json'])
def test_symlinks_refuse_without_writing_target(workspace_dir, tmp_path, relative):
    outside = tmp_path / 'external'
    outside.write_text('{}\n' if relative.endswith(('json', 'jsonc')) else '# unchanged\n')
    path = workspace_dir / relative
    path.parent.mkdir(exist_ok=True)
    path.symlink_to(outside)
    before = outside.read_bytes()
    plan = plan_configuration(workspace_dir, ['codex', 'opencode'], exe=EXE)
    assert any('symlink' in p for p in plan.blockers)
    with pytest.raises(ConfigError):
        plan.apply()
    assert path.is_symlink() and outside.read_bytes() == before


def test_snapshot_preconditions_refuse_changes_before_any_write(workspace_dir):
    plan = plan_configuration(workspace_dir, ['codex', 'opencode'], exe=EXE)
    # Creating an alternative OpenCode config invalidates the entire plan.
    (workspace_dir / 'opencode.jsonc').write_text('{}')
    before = snapshot(workspace_dir)
    with pytest.raises(ConfigError, match='changed since preview'):
        plan.apply()
    assert snapshot(workspace_dir) == before


def test_two_opencode_configs_are_never_merged_or_shadowed(workspace_dir):
    for name in ['opencode.json', 'opencode.jsonc']:
        (workspace_dir / name).write_text('{}')
    before = snapshot(workspace_dir)
    plan = plan_configuration(workspace_dir, ['opencode'], exe=EXE)
    assert plan.blockers and 'Both' in plan.render()
    assert snapshot(workspace_dir) == before


def test_file_permissions_and_crlf_are_preserved(workspace_dir):
    path = workspace_dir / '.codex/config.toml'
    path.parent.mkdir()
    original = b'# windows\r\n[features]\r\nother = true\r\n'
    path.write_bytes(original)
    path.chmod(0o640)
    apply(workspace_dir, ['codex'])
    assert path.stat().st_mode & 0o777 == 0o640
    text = path.read_bytes()
    assert b'\n' not in text.replace(b'\r\n', b'')
    apply(workspace_dir, ['codex'], remove=True)
    assert path.read_bytes() == original


def test_cli_default_preview_apply_receipt_and_clean_removal(workspace_dir, monkeypatch, capsys):
    from ctx.cli import main
    from ctx.setup_telemetry import load_setup_receipt
    import ctx.installer as installer
    monkeypatch.setattr(installer, '_ctx_executable', lambda: EXE)
    # Keep this test focused on real host config checks, without PATH/store checks.
    monkeypatch.setattr(installer, 'doctor_checks', lambda ws: [
        ('codex hooks', *installer._codex_hooks_contract(ws.root / '.codex/hooks.json', EXE)),
        ('codex MCP', *installer._codex_mcp_contract(ws.root / '.codex/config.toml', EXE)),
    ])
    path = workspace_dir / '.codex/config.toml'
    path.parent.mkdir()
    path.write_text('model = "keep"\n')
    prefix = ['--workspace', str(workspace_dir), 'setup', '--host', 'codex']
    before = snapshot(workspace_dir)
    assert main(prefix + ['--preview']) == 0
    assert snapshot(workspace_dir) == before
    assert main(prefix) == 1
    assert snapshot(workspace_dir) == before
    assert main(prefix + ['--apply']) == 0
    assert load_setup_receipt(workspace_dir)['success'] is True
    installed = snapshot(workspace_dir)
    assert main(prefix) == 0
    assert 'already ready' in capsys.readouterr().out
    # receipt timestamp can change; host configuration is byte-identical.
    assert (workspace_dir / '.codex/config.toml').read_bytes() == installed['.codex/config.toml']
    assert main(prefix + ['--remove']) == 0
    assert (workspace_dir / '.codex/hooks.json').exists()
    assert main(prefix + ['--remove', '--apply']) == 0
    assert path.read_text() == 'model = "keep"\n'
    assert not (workspace_dir / '.codex/hooks.json').exists()


def test_configuration_ledger_does_not_backup_secrets(workspace_dir):
    path = workspace_dir / 'opencode.jsonc'
    path.write_text('{"token": "secret-do-not-copy", "mcp": {}}')
    apply(workspace_dir, ['opencode'])
    assert 'secret-do-not-copy' not in (workspace_dir / LEDGER).read_text()


def test_removal_keeps_hooks_added_by_another_integration(workspace_dir):
    apply(workspace_dir, ['codex'])
    path = workspace_dir / '.codex/hooks.json'
    current = json.loads(path.read_text())
    foreign = {'matcher': '*', 'hooks': [{'type': 'command', 'command': 'audit-tool pre'}]}
    current['hooks']['PreToolUse'].append(foreign)
    path.write_text(json.dumps(current))
    apply(workspace_dir, ['codex'], remove=True)
    remaining = json.loads(path.read_text())
    assert remaining['hooks']['PreToolUse'] == [foreign]


def test_conflicting_hook_diff_preserves_foreign_hooks_in_the_same_group(workspace_dir):
    path = workspace_dir / '.codex/hooks.json'
    path.parent.mkdir()
    foreign = {'type': 'command', 'command': 'audit-tool'}
    path.write_text(json.dumps({'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [foreign,
        {'type': 'command', 'command': '/old/ctx hook codex pre-tool-use', 'timeout': 10}]}]}}))
    before = snapshot(workspace_dir)
    plan = plan_configuration(workspace_dir, ['codex'], exe=EXE)
    assert plan.blockers
    proposed = next(c.after for c in plan.changes if c.path == '.codex/hooks.json')
    hooks = json.loads(proposed)['hooks']['PreToolUse']
    assert hooks[0]['hooks'] == [foreign]
    commands = [h['command'] for group in hooks for h in group['hooks']]
    assert len([c for c in commands if 'hook codex pre-tool-use' in c]) == 1
    assert snapshot(workspace_dir) == before


def test_doctor_reports_deleted_integration_components(workspace_dir, monkeypatch):
    import ctx.installer as installer
    from ctx.mcp_hosts import checks
    monkeypatch.setattr(installer, '_ctx_executable', lambda: EXE)
    apply(workspace_dir)
    for relative in ['.codex/config.toml', '.codex/hooks.json', 'AGENTS.md', 'opencode.json', '.opencode/plugins/straitjacket.js']:
        (workspace_dir / relative).unlink()
    rows = {name: ok for name, ok, _ in installer.doctor_checks(make_ws(workspace_dir))}
    assert rows['codex MCP'] is rows['codex hooks'] is rows['codex instructions'] is False
    assert dict((n, ok) for n, ok, _ in checks(workspace_dir))['opencode MCP'] is False
    # Missing files from a previous partial install are safe to reconstruct.
    apply(workspace_dir)
    assert installer._codex_mcp_contract(workspace_dir / '.codex/config.toml', EXE)[0]


def test_opencode_explicit_disable_requires_reconciliation(workspace_dir):
    from ctx.mcp_hosts import checks
    path = workspace_dir / 'opencode.jsonc'
    path.write_text('{"disabledServers": ["other", "ctx-harness"]}')
    plan = plan_configuration(workspace_dir, ['opencode'], exe=EXE)
    assert plan.blockers and 'disabledServers' in plan.render()
    assert not dict((n, ok) for n, ok, _ in checks(workspace_dir))['opencode MCP']
    proposed = next(c.after for c in plan.changes if c.path == path.name)
    assert JSONSource(proposed.decode()).data['disabledServers'] == ['other']


@pytest.mark.parametrize('host,name,original', [
    ('codex', '.codex/config.toml', '# no final newline\n[features]\nother=true'),
    ('opencode', 'opencode.jsonc', '{"model":"keep"}'),
])
def test_preview_diff_is_an_exact_applicable_patch(workspace_dir, host, name, original):
    import subprocess
    path = workspace_dir / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(original)
    plan = plan_configuration(workspace_dir, [host], exe=EXE)
    assert not plan.blockers, plan.render()
    change = next(c for c in plan.changes if c.path == name)
    result = subprocess.run(['git', 'apply', '--unidiff-zero', '-'], input=change.diff(), text=True, cwd=workspace_dir, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == change.after


def test_plain_setup_and_wrap_cannot_bypass_preview(workspace_dir, monkeypatch, capsys):
    from ctx.cli import main
    from ctx.mcp_hosts import wrap
    path = workspace_dir / 'opencode.jsonc'
    path.write_text('{"model": "keep"}')
    monkeypatch.setenv('CTX_SETUP_PLAIN', '1')
    before = snapshot(workspace_dir)
    assert main(['--workspace', str(workspace_dir), 'setup', '--host', 'opencode']) == 1
    assert wrap('opencode', workspace_dir) == 1
    assert snapshot(workspace_dir) == before
    assert '--apply' in capsys.readouterr().out


def test_cli_repair_rechecks_and_restores_partial_setup(workspace_dir, monkeypatch, capsys):
    from ctx.cli import main
    import ctx.installer as installer
    from ctx.setup_telemetry import load_setup_receipt
    monkeypatch.setattr(installer, '_ctx_executable', lambda: EXE)
    monkeypatch.setattr(installer, 'doctor_checks', lambda ws: [('hooks', *installer._codex_hooks_contract(ws.root / '.codex/hooks.json', EXE))])
    prefix = ['--workspace', str(workspace_dir), 'setup', '--host', 'codex']
    assert main(prefix) == 0
    path = workspace_dir / '.codex/hooks.json'
    data = json.loads(path.read_text())
    del data['hooks']['PostToolUse']
    path.write_text(json.dumps(data))
    assert main(prefix + ['--repair']) == 0
    assert installer._codex_hooks_contract(path, EXE)[0]
    assert load_setup_receipt(workspace_dir)['success'] is True
    assert 'already ready' not in capsys.readouterr().out


def test_codex_preserves_toml_nan_dates_and_multiline_strings(workspace_dir):
    path = workspace_dir / '.codex/config.toml'
    path.parent.mkdir()
    original = 'not_a_number = nan\ncreated = 2026-11-09T00:00:00Z\ntext = """line 1\n[features]\nhooks=false\n"""\n'
    path.write_text(original)
    apply(workspace_dir, ['codex'])
    assert original in path.read_text()
    apply(workspace_dir, ['codex'], remove=True)
    assert path.read_text() == original


@pytest.mark.parametrize('binary', ['env with spaces/ctx', 'env with "quotes"/ctx', "env with 'quotes'/ctx"])
def test_installed_executable_paths_are_quoted_for_shell_and_stdio(workspace_dir, monkeypatch, binary):
    import ctx.installer as installer
    path = workspace_dir / binary
    import ctx.selfcmd as selfcmd
    monkeypatch.setattr(selfcmd, 'which_absolute', lambda _name, path_=None: str(path))
    plan = plan_configuration(workspace_dir, ['codex'])
    assert not plan.blockers, plan.render()
    plan.apply()
    data = tomllib.loads((workspace_dir / '.codex/config.toml').read_text())
    assert data['mcp_servers']['ctx-harness']['command'] == str(path)
    hooks = json.loads((workspace_dir / '.codex/hooks.json').read_text())
    command = hooks['hooks']['PreToolUse'][0]['hooks'][0]['command']
    assert shlex.split(command) == [str(path), 'hook', 'codex', 'pre-tool-use']


def test_codex_remote_mcp_is_a_conflict_not_a_ready_stdio_integration(workspace_dir, monkeypatch):
    import ctx.installer as installer
    monkeypatch.setattr(installer, '_ctx_executable', lambda: EXE)
    apply(workspace_dir, ['codex'])
    path = workspace_dir / '.codex/config.toml'
    path.write_text(path.read_text().replace('[mcp_servers.ctx-harness]', '[mcp_servers.ctx-harness]\nurl = "https://example.invalid/mcp"'))
    plan = plan_configuration(workspace_dir, ['codex'], exe=EXE)
    assert plan.blockers and '-url = ' in plan.render()
    assert not installer._codex_mcp_contract(path, EXE)[0]


def test_repeated_partial_repairs_do_not_duplicate_ownership(workspace_dir):
    apply(workspace_dir, ['codex'])
    count = len(json.loads((workspace_dir / LEDGER).read_text())['hosts']['codex']['.codex/hooks.json']['items'])
    path = workspace_dir / '.codex/hooks.json'
    for _ in range(3):
        doc = json.loads(path.read_text())
        del doc['hooks']['SessionStart']
        path.write_text(json.dumps(doc))
        apply(workspace_dir, ['codex'])
        items = json.loads((workspace_dir / LEDGER).read_text())['hosts']['codex']['.codex/hooks.json']['items']
        assert len(items) == count


def test_apply_failure_rolls_back_without_losing_user_settings(workspace_dir, monkeypatch):
    import ctx.host_config as config
    path = workspace_dir / '.codex/config.toml'
    path.parent.mkdir()
    path.write_text('model = "keep"\n')
    before = snapshot(workspace_dir)
    plan = plan_configuration(workspace_dir, ['codex'], exe=EXE)
    replace = config.os.replace
    count = 0
    def fail_second(src, dst):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('injected replacement failure')
        return replace(src, dst)
    monkeypatch.setattr(config.os, 'replace', fail_second)
    with pytest.raises(OSError, match='injected'):
        plan.apply()
    assert snapshot(workspace_dir) == before
    assert not list(workspace_dir.rglob('.ctx-config-*'))


def test_rollback_preserves_a_concurrent_user_edit(workspace_dir, monkeypatch):
    import ctx.host_config as config
    path = workspace_dir / '.codex/config.toml'
    path.parent.mkdir()
    path.write_text('model = "keep"\n')
    plan = plan_configuration(workspace_dir, ['codex'], exe=EXE)
    replace = config.os.replace
    count = 0
    def concurrent_edit(src, dst):
        nonlocal count
        count += 1
        if count == 2:
            path.write_text('model = "concurrent-choice"\n')
            raise OSError('injected failure')
        return replace(src, dst)
    monkeypatch.setattr(config.os, 'replace', concurrent_edit)
    with pytest.raises(ConfigError, match='concurrently changed'):
        plan.apply()
    assert path.read_text() == 'model = "concurrent-choice"\n'
    assert not (workspace_dir / LEDGER).exists()


@pytest.mark.parametrize('content', ['{broken', '{"schema":"other", "hosts":{}}', '{"schema":"ctx.host-config/v1", "hosts":{"codex":[]}}'])
def test_invalid_ownership_metadata_refuses_safely(workspace_dir, content):
    path = workspace_dir / LEDGER
    path.parent.mkdir()
    path.write_text(content)
    before = snapshot(workspace_dir)
    plan = plan_configuration(workspace_dir, ['codex'], exe=EXE)
    assert plan.blockers
    with pytest.raises(ConfigError):
        plan.apply()
    assert snapshot(workspace_dir) == before


def test_doctor_rejects_incomplete_instruction_block(workspace_dir, monkeypatch):
    import ctx.installer as installer
    monkeypatch.setattr(installer, '_ctx_executable', lambda: EXE)
    apply(workspace_dir, ['codex'])
    path = workspace_dir / 'AGENTS.md'
    path.write_text('<!-- ctx-harness:start -->\n<!-- ctx-harness:end -->\n')
    rows = {n: ok for n, ok, _ in installer.doctor_checks(make_ws(workspace_dir))}
    assert rows['codex instructions'] is False


def test_repair_and_removal_preserve_comments_added_inside_owned_values(workspace_dir):
    path = workspace_dir / '.codex/config.toml'
    apply(workspace_dir, ['codex'])
    path.write_text(path.read_text().replace('args = ', 'args = [ # retain this user annotation\n').replace('["-m", "ctx", "mcp", "--bounded-only"]', '"-m", "ctx", "mcp", "--bounded-only"\n]'))
    # No semantic configuration change: the source annotation is user-owned.
    assert tomllib.loads(path.read_text())['mcp_servers']['ctx-harness']['args'][-1] == '--bounded-only'
    before = snapshot(workspace_dir)
    for remove in (False, True):
        plan = plan_configuration(workspace_dir, ['codex'], exe='/upgraded/ctx', remove=remove)
        assert plan.blockers and 'source changed' in plan.render()
        assert '--- a/.codex/config.toml' in plan.render()
        with pytest.raises(ConfigError):
            plan.apply()
    assert snapshot(workspace_dir) == before


def test_repair_and_removal_preserve_jsonc_comments_inside_owned_command(workspace_dir):
    path = workspace_dir / 'opencode.jsonc'
    path.write_text('{}')
    apply(workspace_dir, ['opencode'])
    path.write_text(path.read_text().replace('"command": [', '"command": [ /* keep this annotation */ '))
    before = snapshot(workspace_dir)
    for remove in (False, True):
        plan = plan_configuration(workspace_dir, ['opencode'], exe='/upgraded/ctx', remove=remove)
        assert plan.blockers and 'source changed' in plan.render()
        assert '--- a/opencode.jsonc' in plan.render()
        with pytest.raises(ConfigError):
            plan.apply()
    assert snapshot(workspace_dir) == before


@pytest.mark.parametrize('original', ['', '# Project rules\n\nKeep project rules.\n'])
@pytest.mark.parametrize('remove', [False, True])
@pytest.mark.parametrize('edit', ['add', 'change', 'comment'])
def test_modified_instruction_block_requires_reconciliation(workspace_dir, original, remove, edit):
    path = workspace_dir / 'AGENTS.md'
    if original:
        path.write_text(original)
    apply(workspace_dir, ['codex'])
    text = path.read_text()
    if edit == 'add':
        text = text.replace('<!-- ctx-harness:end -->', 'Always use the workspace staging service.\n<!-- ctx-harness:end -->')
    elif edit == 'change':
        text = text.replace('## Context containment', '## Custom workspace instructions')
    else:
        text = text.replace('<!-- ctx-harness:end -->', '<!-- Retain this workspace annotation. -->\n<!-- ctx-harness:end -->')
    path.write_text(text)
    before = snapshot(workspace_dir)
    plan = plan_configuration(workspace_dir, ['codex'], exe=EXE, remove=remove)
    assert plan.blockers, plan.render()
    assert 'AGENTS.md' in plan.render() and 'block' in plan.render()
    assert '--- a/AGENTS.md' in plan.render()
    with pytest.raises(ConfigError):
        plan.apply()
    assert snapshot(workspace_dir) == before


def test_remove_keeps_changes_outside_unchanged_instruction_block(workspace_dir):
    path = workspace_dir / 'AGENTS.md'
    path.write_text('# Original project instructions\n')
    apply(workspace_dir, ['codex'])
    installed = path.read_text()
    outside_before = '# Added project rule\n\n'
    outside_after = '\n\n# Added project appendix\n'
    path.write_text(outside_before + installed + outside_after)
    start = path.read_text().index('<!-- ctx-harness:start -->')
    end = path.read_text().index('<!-- ctx-harness:end -->') + len('<!-- ctx-harness:end -->')
    expected = path.read_text()[:start] + path.read_text()[end:]
    apply(workspace_dir, ['codex'], remove=True)
    assert path.read_text() == expected
    assert not (workspace_dir / LEDGER).exists()


def test_cli_remove_apply_refuses_modified_instruction_block(workspace_dir, capsys):
    from ctx.cli import main
    apply(workspace_dir, ['codex'])
    path = workspace_dir / 'AGENTS.md'
    path.write_text(path.read_text().replace('<!-- ctx-harness:end -->', 'Keep the user-added constraint.\n<!-- ctx-harness:end -->'))
    before = snapshot(workspace_dir)
    assert main(['--workspace', str(workspace_dir), 'setup', '--host', 'codex', '--remove', '--apply']) == 1
    assert 'AGENTS.md' in capsys.readouterr().out
    assert snapshot(workspace_dir) == before


def test_legacy_instruction_provenance_migrates_only_from_an_unchanged_file(workspace_dir):
    path = workspace_dir / 'AGENTS.md'
    apply(workspace_dir, ['codex'])
    installed = path.read_bytes()
    ledger = workspace_dir / LEDGER
    state = json.loads(ledger.read_text())
    expected_hash = state['hosts']['codex']['AGENTS.md'].pop('block_hash')
    ledger.write_text(json.dumps(state))

    plan = apply(workspace_dir, ['codex'])
    assert [change.path for change in plan.changes] == [LEDGER]
    assert path.read_bytes() == installed
    assert json.loads(ledger.read_text())['hosts']['codex']['AGENTS.md']['block_hash'] == expected_hash
    path.write_bytes(b'# Added later\n' + installed)
    apply(workspace_dir, ['codex'], remove=True)
    assert path.read_bytes() == b'# Added later\n\n'


@pytest.mark.parametrize('inside', [False, True])
def test_edited_legacy_instruction_file_cannot_acquire_unverified_block_ownership(workspace_dir, inside):
    apply(workspace_dir, ['codex'])
    path = workspace_dir / 'AGENTS.md'
    ledger = workspace_dir / LEDGER
    state = json.loads(ledger.read_text())
    state['hosts']['codex']['AGENTS.md'].pop('block_hash')
    ledger.write_text(json.dumps(state))
    text = path.read_text()
    path.write_text(text.replace('<!-- ctx-harness:end -->', 'Preserve this custom instruction.\n<!-- ctx-harness:end -->') if inside else '# Custom outside instructions\n' + text)
    before = snapshot(workspace_dir)

    removal = plan_configuration(workspace_dir, ['codex'], remove=True)
    assert removal.blockers and 'block hash is missing' in removal.render()
    with pytest.raises(ConfigError):
        removal.apply()
    setup = plan_configuration(workspace_dir, ['codex'], exe=EXE)
    if setup.blockers:
        with pytest.raises(ConfigError):
            setup.apply()
    else:
        setup.apply()
    assert snapshot(workspace_dir) == before


def test_owned_instruction_upgrade_preserves_outside_edits_and_updates_block_provenance(workspace_dir, monkeypatch):
    import ctx.installer as installer
    path = workspace_dir / 'AGENTS.md'
    path.write_text('# Project guidance\n')
    apply(workspace_dir, ['codex'])
    old_hash = json.loads((workspace_dir / LEDGER).read_text())['hosts']['codex']['AGENTS.md']['block_hash']
    path.write_text('# Added before upgrade\n' + path.read_text())
    render = installer._render_codex_file
    def upgraded(name, exe):
        text = render(name, exe)
        return text.replace('<!-- ctx-harness:end -->', 'New ctx-owned guidance.\n<!-- ctx-harness:end -->') if name == 'AGENTS.md' else text
    monkeypatch.setattr(installer, '_render_codex_file', upgraded)

    apply(workspace_dir, ['codex'])
    new_hash = json.loads((workspace_dir / LEDGER).read_text())['hosts']['codex']['AGENTS.md']['block_hash']
    assert new_hash != old_hash
    assert 'New ctx-owned guidance.' in path.read_text()
    assert not apply(workspace_dir, ['codex']).changes
    path.write_text(path.read_text() + '# Added after upgrade\n')
    text = path.read_text()
    a, b = text.index('<!-- ctx-harness:start -->'), text.index('<!-- ctx-harness:end -->') + len('<!-- ctx-harness:end -->')
    apply(workspace_dir, ['codex'], remove=True)
    assert path.read_text() == text[:a] + text[b:]


def test_already_removed_instruction_block_preserves_remaining_user_text(workspace_dir):
    apply(workspace_dir, ['codex'])
    path = workspace_dir / 'AGENTS.md'
    path.write_text('# User instructions after removing the ctx block\n')
    apply(workspace_dir, ['codex'], remove=True)
    assert path.read_text() == '# User instructions after removing the ctx block\n'
    assert not plan_configuration(workspace_dir, ['codex'], remove=True).changes
