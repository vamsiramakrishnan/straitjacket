"""Addressed paragraph/fence selection. Ranking never changes source bytes."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    text: str
    heading: int | None

    @property
    def cost(self) -> int:
        # Conservative byte budget, not a claim about provider tokenization.
        return len(self.text.encode("utf-8"))


def prepare_evidence(text: str) -> tuple[Block, ...]:
    lines = text.splitlines(keepends=True)
    blocks = []
    start = 0
    heading = None
    headings = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        heading_match = re.match(r"^(#{1,6})\s", line) if fence is None else None
        is_heading = heading_match is not None
        if is_heading and index > start:
            blocks.append(Block(start + 1, index, "".join(lines[start:index]), heading))
            start = index
        if heading_match:
            level = len(heading_match[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            heading = headings[-1][1] if headings else None
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif (token[0] == fence[0] and len(token) >= len(fence)
                  and not line[marker.end():].strip()):
                fence = None
        if fence is None and (not line.strip() or is_heading):
            blocks.append(Block(start + 1, index + 1, "".join(lines[start:index + 1]), heading))
            if is_heading:
                heading = len(blocks) - 1
                headings.append((level, heading))
            start = index + 1
    if start < len(lines):
        blocks.append(Block(start + 1, len(lines), "".join(lines[start:]), heading))
    return tuple(blocks)


def features(blocks: tuple[Block, ...], query: str) -> list[dict]:
    terms = set(re.findall(r"[\w./:-]{2,}", query.lower())) - {
        "the", "and", "for", "with", "this", "that", "what", "please", "fix",
    }
    hits = [set(term for term in terms if term in block.text.lower()) for block in blocks]
    df = {term: sum(term in row for row in hits) for term in terms}
    return [{"id": i, "cost": b.cost,
             "relevance": sum(1 / df[term] for term in sorted(hits[i])),
             "hits": len(hits[i])} for i, b in enumerate(blocks)]


def select_evidence(blocks: tuple[Block, ...], order: list[int], budget: int,
                    required: set[int]) -> dict:
    """Protected verifier closes dependencies; overflow returns retrieval needs."""
    if (type(budget) is not int or budget < 0 or not isinstance(order, list)
            or any(type(i) is not int for i in order)
            or len(order) != len(blocks) or set(order) != set(range(len(blocks)))):
        raise ValueError("ranking must be a permutation and budget nonnegative")
    if not required <= set(range(len(blocks))):
        raise ValueError("invalid required block")

    def close(ids):
        result = set(ids)
        for i in list(result):
            parent = blocks[i].heading
            while parent is not None and parent not in result:
                result.add(parent)
                parent = blocks[parent].heading
        return result

    kept = close(required)
    mass = sum(blocks[i].cost for i in kept)
    if mass > budget:
        return {"status": "needs_retrieval", "kept": [], "required": sorted(kept),
                "bytes": 0, "required_bytes": mass}
    for i in order:
        proposed = close(kept | {i})
        size = sum(blocks[j].cost for j in proposed)
        if size <= budget:
            kept, mass = proposed, size
    return {"status": "selected", "kept": sorted(kept), "required": sorted(close(required)),
            "bytes": mass, "required_bytes": sum(blocks[i].cost for i in close(required))}
