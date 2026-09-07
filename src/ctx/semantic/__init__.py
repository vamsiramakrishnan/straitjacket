"""Explicit, bounded model analysis over immutable evidence.

``prepare`` freezes and partitions evidence without invoking a model. ``run``
calls a caller-supplied worker and checkpoints each attempt. ``inspect`` reads
the latest report without executing anything. Ordinary retrieval stays pure.
"""

from ctx.semantic.contract import SemanticError
from ctx.semantic.evidence import prepare
from ctx.semantic.engine import inspect, run

__all__ = ["SemanticError", "prepare", "run", "inspect"]
