import pytest

from ctx.session_economics import compare, normalize_usage, summarize


def model(**kwargs):
    return {'schema': 'ctx.session-event/v1', 'kind': 'model', 'id': 'response-1', 'model': 'test-pinned', **kwargs}


def test_missing_usage_is_not_free():
    report = summarize([model()])
    assert report['cost_usd'] is None
    assert report['usage']['input_tokens'] is None
    assert report['model_turns'] is None
    assert not report['usage_complete']
    assert summarize([])['cost_usd'] is None


def test_cached_input_and_reasoning_are_not_double_charged():
    usage = normalize_usage('openai', {'input_tokens': 1000, 'input_tokens_details': {'cached_tokens': 800},
                                    'output_tokens': 100, 'output_tokens_details': {'reasoning_tokens': 80}})
    report = summarize([model(usage=usage, turns=1)], rates={'input_tokens': 3, 'cache_read_tokens': 0.3,
                                                          'cache_write_tokens': 0, 'output_tokens': 12})
    assert usage['input_tokens'] == 200
    assert report['cost_usd'] == pytest.approx(.00204)
    assert report['usage']['reasoning_tokens'] == 80
    assert report['usage']['output_tokens'] == 100


def test_payload_counts_do_not_inflate_provider_bill():
    report = summarize([model(cost_usd=.1), {'schema': 'ctx.session-event/v1', 'kind': 'tool', 'id': 'tool-1',
                         'argument_tokens': 10, 'result_tokens': 2000, 'retrieval': True, 'retry': True},
                        {'schema': 'ctx.session-event/v1', 'kind': 'tool_schema', 'id': 'schema-1', 'tokens': 500}])
    assert report['cost_usd'] == .1
    assert report['retrieval_calls'] == report['retry_calls'] == 1
    assert report['tool_schema_tokens'] == 500


@pytest.mark.parametrize('usage', [{'input_tokens': -1}, {'output_tokens': float('nan')},
                                  {'output_tokens': 3, 'reasoning_tokens': 4}, {'input_tokens': True}])
def test_invalid_usage_rejected(usage):
    with pytest.raises(ValueError):
        summarize([model(usage=usage)])


def test_duplicate_final_responses_rejected():
    with pytest.raises(ValueError, match='unique'):
        summarize([model(), model()])


def test_failed_or_mismatched_session_has_no_savings():
    identity = dict(fixture_sha256='abc', acceptance_sha256='def', model='x', host='h', budget=5)
    baseline = dict(accepted=True, cost_usd=1, usage_complete=True, models=['x'])
    candidate = dict(accepted=False, cost_usd=.1, usage_complete=True, models=['x'])
    assert compare(baseline, candidate, baseline_identity=identity, candidate_identity=identity)['cost_savings_usd'] is None
    candidate['accepted'] = True
    assert compare(baseline, candidate, baseline_identity=identity, candidate_identity=identity)['cost_savings_usd'] == .9
    assert compare(baseline, candidate, baseline_identity=identity, candidate_identity={**identity, 'model': 'y'})['cost_savings_usd'] is None


def test_comparison_requires_observed_model_to_match_declared_identity():
    identity = dict(fixture_sha256='abc', acceptance_sha256='def', model='claimed', host='h', budget=5)
    baseline = dict(accepted=True, cost_usd=1, usage_complete=True, models=['actual-A'])
    candidate = dict(accepted=True, cost_usd=.5, usage_complete=True, models=['actual-B'])
    for observed in (['actual-B'], ['actual-A'], [], None):
        candidate['models'] = observed
        result = compare(baseline, candidate, baseline_identity=identity, candidate_identity=identity)
        assert not result['matched']
        assert result['cost_savings_usd'] is None


@pytest.mark.parametrize('bad', [True, -1, 0.5, '3', float('nan')])
@pytest.mark.parametrize('field', ['candidatesTokenCount', 'thoughtsTokenCount'])
def test_gemini_validates_each_output_component_before_combining(field, bad):
    usage = {'candidatesTokenCount': 3, 'thoughtsTokenCount': 2, field: bad}
    with pytest.raises(ValueError):
        normalize_usage('gemini', usage)


def test_normalizers_keep_missing_categories_unknown():
    assert normalize_usage('anthropic', {'input_tokens': 3})['cache_read_tokens'] is None
    assert normalize_usage('openai', {'input_tokens': 30})['input_tokens'] is None
    usage = normalize_usage('gemini', {'promptTokenCount': 100, 'cachedContentTokenCount': 30,
                                      'candidatesTokenCount': 20, 'thoughtsTokenCount': 10})
    assert usage['input_tokens'] == 70
    assert usage['output_tokens'] == 30
    assert usage['cache_write_tokens'] is None


def test_raw_provider_event_and_strict_flags():
    report = summarize([model(provider='openai', provider_usage={'input_tokens': 10,
                        'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 4})])
    assert report['usage']['input_tokens'] == 10
    for bad in (0, 1, 'true'):
        with pytest.raises(ValueError):
            summarize([], accepted=bad)
        with pytest.raises(ValueError):
            summarize([{'schema': 'ctx.session-event/v1', 'kind': 'tool', 'id': 't', 'retry': bad}])


def test_mixed_cache_write_rates_require_explicit_cost():
    event = model(cache_write_ttls=['5m', '1h'])
    with pytest.raises(ValueError, match='TTL'):
        summarize([event], rates={})
    event['cost_usd'] = .2
    assert summarize([event], rates={})['cost_usd'] == .2


def test_raw_anthropic_mixed_ttls_reject_one_rate():
    event = model(provider='anthropic', provider_usage={'input_tokens': 10, 'cache_creation_input_tokens': 30,
                  'cache_creation': {'ephemeral_5m_input_tokens': 10, 'ephemeral_1h_input_tokens': 20}})
    with pytest.raises(ValueError, match='TTL'):
        summarize([event], rates={})
