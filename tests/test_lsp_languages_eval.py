import json
import sys

import pytest

from evals import lsp_languages


@pytest.mark.parametrize('strict,expected', [(False, 0), (True, 1)])
def test_unversioned_diagnostics_are_explicit_and_strict_mode_fails(tmp_path, monkeypatch, strict, expected):
    output = tmp_path / 'report.json'
    monkeypatch.setattr(lsp_languages, 'run_language', lambda *a: {
        'language': 'typescript', 'passed': True, 'edit_passed': True,
        'diagnostic_status': 'unavailable_unversioned', 'status': 'passed'})
    args = ['replay', '--output', str(output), '--languages', 'typescript']
    if strict:
        args.append('--require-fresh-diagnostics')
    monkeypatch.setattr(sys, 'argv', args)
    assert lsp_languages.main() == expected
    report = json.loads(output.read_text())
    assert report['passed'] is (not strict)
    assert report['languages'][0]['diagnostic_status'] == 'unavailable_unversioned'


def test_missing_language_server_never_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(lsp_languages.lsp, 'server_for', lambda *a: None)
    row = lsp_languages.run_language('go', tmp_path / 'go')
    assert row['status'] == 'unavailable'
    assert row['passed'] is False
