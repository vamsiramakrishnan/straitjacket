import json

from ctx.context_compression import compress_context, compress_stored, cache_wrap
from conftest import make_ws, make_store


def test_verifier_protects_literal_entity_and_fence():
    text = '# Runtime\n```python\nAuthToken = None\n\nDEBUG = False\n```\n\n' + 'noise ' * 100
    result = compress_context(text, 'AuthToken DEBUG', 80)
    assert result['status'] == 'selected'
    assert 'AuthToken = None\n\nDEBUG = False' in result['text']
    assert result['text'].count('```') == 2
    assert result['entity_recall_ok']
    assert result['output_bytes'] <= 80


def test_overflow_requests_exact_retrieval_not_broken_code():
    result = compress_context('# Config\n```\nAPI_TOKEN = None\n```\n', 'API_TOKEN', 5)
    assert result['status'] == 'needs_retrieval'
    assert result['text'] == ''
    assert not result['entity_recall_ok']
    assert result['required_bytes'] > 5


def test_cache_prefix_is_independent_of_dynamic_query():
    a = cache_wrap('stable rules', 'first evidence', 'first query')
    b = cache_wrap('stable rules', 'second evidence', 'second query')
    assert a['prefix'] == b['prefix']
    assert a['prefix_sha256'] == b['prefix_sha256']
    assert a['suffix'] != b['suffix']
    assert json.loads(a['suffix'])['query'] == 'first query'


def test_original_round_trip_is_exact(state_home, workspace_dir):
    store = make_store(make_ws(workspace_dir))
    text = 'first\r\n\r\nAuthToken=abc\r\n\r\nlast\r\n'
    result = compress_stored(store, text, 'AuthToken', 20)
    assert store.get_blob(result['original_ref'].removeprefix('blob:')).decode() == text
