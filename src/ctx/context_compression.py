"""Local evidence extraction with a fixed budget and reversible store companion."""
from __future__ import annotations

import hashlib
import json
import re

from ctx.digest.block_selection import prepare_evidence, features, select_evidence


def question_entities(query: str) -> tuple[str, ...]:
    patterns = r'"([^"\n]+)"|\b([A-Z][a-z]+[A-Z]\w*|[A-Z_]{3,}|\d+(?:\.\d+)*|[\w.-]+/[^\s,;]+)'
    return tuple(sorted({a or b for a, b in re.findall(patterns, query)}))


def compress_context(text: str, query: str, budget_bytes: int = 2048,
                     required_lines: tuple[int, ...] = ()) -> dict:
    blocks = prepare_evidence(text)
    if type(budget_bytes) is not int or budget_bytes < 0:
        raise ValueError("budget_bytes must be a nonnegative integer")
    if any(type(line) is not int or not 1 <= line <= len(text.splitlines()) for line in required_lines):
        raise ValueError("required line outside input")
    items = features(blocks, query)
    order = [i["id"] for i in sorted(items, key=lambda i: (-i["relevance"], i["id"]))]
    required = {i for i, b in enumerate(blocks) if any(b.start <= line <= b.end for line in required_lines)}
    entities = [entity for entity in question_entities(query) if entity in text]
    selected = select_evidence(blocks, order, budget_bytes, required)
    retries = 0
    # The verifier may only add protection, never weaken the byte limit.
    for entity in entities:
        kept_text = ''.join(blocks[i].text for i in selected['kept'])
        if entity in kept_text:
            continue
        choices = [i for i, b in enumerate(blocks) if entity in b.text]
        required.add(min(choices, key=lambda i: (blocks[i].cost, i)))
        selected = select_evidence(blocks, order, budget_bytes, required)
        retries += 1
    kept = set(selected['kept'])
    compressed = ''.join(b.text for i, b in enumerate(blocks) if i in kept)
    recall_ok = all(entity in compressed for entity in entities)
    spans = lambda ids: [{"start": blocks[i].start, "end": blocks[i].end} for i in sorted(ids)]
    return {"schema": "ctx.compression/v1", "status": selected['status'],
            "text": compressed, "kept": spans(kept),
            "omitted": spans(set(range(len(blocks))) - kept),
            "required": spans(selected['required']),
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "input_bytes": len(text.encode()), "output_bytes": len(compressed.encode()),
            "budget_bytes": budget_bytes, "required_bytes": selected['required_bytes'],
            "entity_recall_ok": recall_ok, "verifier_retries": retries,
            "quality_claim": "structural and literal entity checks; not semantic answerability"}


def cache_wrap(stable_context: str, evidence: str, query: str) -> dict[str, str]:
    """Keep the reusable prefix independent of the query and selected evidence."""
    prefix = json.dumps({"ctx_prompt_version": 1, "reference_context": stable_context},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    suffix = json.dumps({"evidence": evidence, "query": query}, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"))
    return {"prefix": prefix, "suffix": suffix, "prompt": prefix + suffix,
            "prefix_sha256": hashlib.sha256(prefix.encode()).hexdigest()}


def compress_stored(store, text: str, query: str, budget_bytes: int = 2048,
                    required_lines: tuple[int, ...] = ()) -> dict:
    original = store.put_blob(text.encode())
    store.lease(original, "compression", ttl_days=store.retention_days)
    result = compress_context(text, query, budget_bytes, required_lines)
    result['original_ref'] = 'blob:' + original
    result['retrieve'] = f"ctx get blob:{original} --lines A:B"
    return result
