"""Audit whole sessions from per-request usage and observed tool events.

Input JSONL uses ctx.session-event/v1. Model usage is disjoint uncached input,
cache read, cache write, and output. Reasoning is a subset of output, never added
again. Schema/retrieval/argument tokens describe tool payloads, not additional
billable tokens. Missing observations remain null. No provider calls are made.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

BILLED = ('input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens')
USAGE = (*BILLED, 'reasoning_tokens')


def _number(value: Any, key: str, *, integer: bool = True):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{key} must be a finite nonnegative number or null')
    if integer and int(value) != value:
        raise ValueError(f'{key} must be an integer')
    return int(value) if integer else float(value)


def normalize_usage(provider: str, usage: dict) -> dict:
    """Normalize one final response usage object; do not pass stream snapshots.

    OpenAI input includes cache reads; Gemini prompt includes cached content and
    thoughts are separate from candidates. Anthropic categories are disjoint.
    Unsupported/missing categories stay null rather than inferred zero.
    """
    if provider == 'anthropic':
        raw = dict(input_tokens=usage.get('input_tokens'),
                   cache_read_tokens=usage.get('cache_read_input_tokens'),
                   cache_write_tokens=usage.get('cache_creation_input_tokens'),
                   output_tokens=usage.get('output_tokens'), reasoning_tokens=None)
    elif provider == 'openai':
        details = usage.get('input_tokens_details') or {}
        raw = dict(input_tokens=usage.get('input_tokens'), cache_read_tokens=details.get('cached_tokens'),
                   cache_write_tokens=0, output_tokens=usage.get('output_tokens'),
                   reasoning_tokens=(usage.get('output_tokens_details') or {}).get('reasoning_tokens'))
    elif provider == 'gemini':
        candidates, thoughts = usage.get('candidatesTokenCount'), usage.get('thoughtsTokenCount')
        raw = dict(input_tokens=usage.get('promptTokenCount'), cache_read_tokens=usage.get('cachedContentTokenCount'),
                   cache_write_tokens=None,
                   output_tokens=(None if candidates is None or thoughts is None else candidates + thoughts),
                   reasoning_tokens=thoughts)
    else:
        raise ValueError(f'unsupported provider: {provider}')
    result = {key: _number(raw.get(key), key) for key in USAGE}
    if provider in ('openai', 'gemini'):
        total, cached = result['input_tokens'], result['cache_read_tokens']
        if total is not None and cached is not None:
            if cached > total:
                raise ValueError('cache reads exceed total input')
            result['input_tokens'] = total - cached
        else:
            result['input_tokens'] = None
    return result


def _complete_sum(values):
    values = list(values)
    return sum(values) if values and all(v is not None for v in values) else None


def summarize(events: list[dict], *, rates: dict | None = None, accepted: bool | None = None) -> dict:
    """Aggregate each unique final response once; include failed/retried requests.

    Rates are USD per million tokens supplied by the caller for this exact model.
    Cache-write tokens must share a single TTL/rate; declare cache_write_ttls
    when importing mixed TTL usage, or supply the provider-reported cost.
    Mixed models require host-reported per-response cost; one rate card is rejected.
    """
    models, tools, schemas = [], [], []
    ids = set()
    for event in events:
        if event.get('schema') != 'ctx.session-event/v1':
            raise ValueError('expected ctx.session-event/v1')
        event_id = event.get('id')
        if not isinstance(event_id, str) or not event_id or event_id in ids:
            raise ValueError('every event needs a unique nonempty id; do not mix cumulative and per-request usage')
        ids.add(event_id)
        kind = event.get('kind')
        if kind == 'model':
            if not isinstance(event.get('model'), str) or not event['model']:
                raise ValueError('model events require the resolved model identifier')
            raw_usage = (normalize_usage(event['provider'], event['provider_usage'])
                         if 'provider_usage' in event else event.get('usage') or {})
            usage = {key: _number(raw_usage.get(key), key) for key in USAGE}
            creation = (event.get('provider_usage') or {}).get('cache_creation') or {}
            mixed_creation = sum(bool(_number(creation.get(k), k)) for k in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens')) > 1
            mixed_ttls = bool(event.get('cache_write_ttls')) and len(set(event['cache_write_ttls'])) > 1
            if (mixed_ttls or mixed_creation) and rates is not None and event.get('cost_usd') is None:
                raise ValueError('mixed cache-write TTLs require provider-reported cost or separately priced events')
            if usage['reasoning_tokens'] is not None and usage['output_tokens'] is not None and usage['reasoning_tokens'] > usage['output_tokens']:
                raise ValueError('reasoning is a subset of output')
            models.append({**event, 'usage': usage, 'cost_usd': _number(event.get('cost_usd'), 'cost_usd', integer=False)})
        elif kind == 'tool':
            if any(event.get(k) is not None and type(event[k]) is not bool for k in ('retry', 'retrieval')):
                raise ValueError('tool retry/retrieval must be boolean or null')
            tools.append({**event, **{k: _number(event.get(k), k) for k in ('argument_tokens', 'result_tokens')}})
        elif kind == 'tool_schema':
            schemas.append(_number(event.get('tokens'), 'tokens'))
        else:
            raise ValueError(f'unknown event kind: {kind}')
    if accepted is not None and type(accepted) is not bool:
        raise ValueError('accepted must be boolean or null')
    if rates is not None:
        if len({e['model'] for e in models}) > 1:
            raise ValueError('one rate card cannot price mixed models')
        rates = {key: _number(rates.get(key), key, integer=False) for key in BILLED}
    costs = []
    for event in models:
        cost = event['cost_usd']
        if cost is None and rates is not None:
            parts = [None if event['usage'][k] is None or rates[k] is None else event['usage'][k] * rates[k] / 1_000_000 for k in BILLED]
            cost = _complete_sum(parts)
        costs.append(cost)
    totals = {key: _complete_sum(e['usage'][key] for e in models) for key in USAGE}
    return {
        'schema': 'ctx.session-economics/v1', 'accepted': accepted,
        'model_calls': len(models), 'model_turns': _complete_sum(_number(e.get('turns'), 'turns') for e in models),
        'models': sorted({e['model'] for e in models}), 'usage': totals,
        'usage_complete': bool(models) and all(totals[k] is not None for k in BILLED),
        'cost_usd': _complete_sum(costs), 'cost_basis': 'per_response_usage_or_reported_cost',
        'tool_calls': len(tools), 'tool_argument_tokens': _complete_sum(e['argument_tokens'] for e in tools),
        'tool_result_tokens': _complete_sum(e['result_tokens'] for e in tools),
        'retrieval_calls': _complete_sum(None if e.get('retrieval') is None else int(e['retrieval']) for e in tools),
        'retry_calls': _complete_sum(None if e.get('retry') is None else int(e['retry']) for e in tools),
        'tool_schema_tokens': _complete_sum(schemas),
        'accounting_note': 'Payload counts are descriptive; do not add them to provider usage. Reasoning is included in output. No model events means usage and cost are unmeasured.',
    }


def compare(baseline: dict, candidate: dict, *, baseline_identity: dict, candidate_identity: dict) -> dict:
    """Only accepted, completely observed, matched sessions support cost savings."""
    required = ('fixture_sha256', 'acceptance_sha256', 'model', 'host', 'budget')
    matched = all(baseline_identity.get(k) is not None and baseline_identity.get(k) == candidate_identity.get(k) for k in required)
    valid = matched and baseline.get('accepted') is True and candidate.get('accepted') is True
    before, after = baseline.get('cost_usd'), candidate.get('cost_usd')
    measurable = valid and baseline.get('usage_complete') and candidate.get('usage_complete') and before is not None and after is not None
    return {'matched': matched, 'cost_savings_usd': before - after if measurable else None,
            'cost_savings_ratio': (before - after) / before if measurable and before > 0 else None,
            'reason': 'matched successful sessions' if measurable else 'needs matching identity, task success and complete usage/cost'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('events', type=Path)
    parser.add_argument('--rates', type=Path, help='JSON USD/million disjoint usage categories for the resolved model')
    parser.add_argument('--accepted', choices=('true', 'false', 'unknown'), default='unknown')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    events = [json.loads(line) for line in args.events.read_text().splitlines() if line.strip()]
    report = summarize(events, rates=json.loads(args.rates.read_text()) if args.rates else None,
                       accepted={'true': True, 'false': False, 'unknown': None}[args.accepted])
    rendered = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.write_text(rendered)
    else:
        print(rendered, end='')


if __name__ == '__main__':
    main()
