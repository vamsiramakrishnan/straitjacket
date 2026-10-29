import pytest

from ctx.digest.block_selection import prepare_evidence, select_evidence
from evals.alphaevolve.block_selection import evaluate
from evals.alphaevolve import portfolio


def test_blocks_preserve_exact_bytes_and_fences():
    text = '# Auth\r\n```python\r\nDEBUG = None\r\n\r\nlicense = ""\r\n```\r\n\r\ntail'
    blocks = prepare_evidence(text)
    assert ''.join(block.text for block in blocks) == text
    assert 'DEBUG = None\r\n\r\nlicense' in blocks[1].text
    assert (blocks[1].start, blocks[1].end) == (2, 7)


def test_dependency_overflow_requires_retrieval_instead_of_silent_cut():
    blocks = prepare_evidence('# Auth\ntoken=None\n')
    result = select_evidence(blocks, [1, 0], 12, {1})
    assert result['status'] == 'needs_retrieval'
    assert result['required'] == [0, 1]
    assert result['kept'] == []
    result = select_evidence(blocks, [1, 0], 100, {1})
    assert result['kept'] == [0, 1]
    assert result['bytes'] == sum(b.cost for b in blocks)


def test_heading_dependencies_follow_nesting_not_unrelated_sections():
    blocks = prepare_evidence('# Old\nold\n\n# Current\n## Details\nanswer\n')
    result = select_evidence(blocks, list(range(len(blocks))), 29, {4})
    assert result['required'] == [2, 3, 4]
    assert result['kept'] == [2, 3, 4]


@pytest.mark.parametrize('order', [[0, 0], [1], [False, True], [0.0, 1.0], '01', [[0], [1]]])
def test_reject_malformed_candidate_ranking(order):
    with pytest.raises(ValueError):
        select_evidence(prepare_evidence('first\n\nsecond'), order, 100, set())


def test_bad_candidate_cannot_win_by_omitting_everything():
    code = '# EVOLVE-BLOCK-START\ndef rank_blocks(items):\n    return []\n# EVOLVE-BLOCK-END'
    result = evaluate.score_candidate(code)
    assert result['error']
    assert result['score'] < 0


def test_synthetic_results_do_not_unlock_promotion():
    _, row = portfolio._experiment_row('block-selection')
    assert not row['promotion_ready']
    assert all(gate['passed'] for gate in row['gates'].values())


def test_bad_seed_prevents_cloud_resource_creation(monkeypatch):
    def forbidden(*args):
        pytest.fail('cloud touched before local preflight')
    monkeypatch.setattr(portfolio, '_cloud_objects', forbidden)
    monkeypatch.setattr(evaluate, 'score_candidate', lambda code: {'error': 'invalid seed'})
    with pytest.raises(RuntimeError, match='invalid seed'):
        portfolio.start(evaluate, 6, 1)
