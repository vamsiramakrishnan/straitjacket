# Query-aware block ranking

Implement rank_blocks(items), returning a permutation of integer block IDs.
Items contain id, byte cost, relevance and query-hit count. Maximize retained
answer evidence under a fixed byte budget. Only the EVOLVE-BLOCK is mutable.
Segmentation, dependency closure, budget enforcement, and evaluation are fixed.
The evaluator corpus and required-evidence labels are not candidate inputs.
No I/O, subprocesses, network, or access to evaluation code is permitted.
This is a synthetic ranking experiment, not a downstream coding-success test.

Candidate code runs in the repository's restricted Python evaluator. Use only
pure functions and the allowed builtins; do not use imports outside collections,
heapq, math, re, statistics, string, typing. No classes, reflection, try/raise,
global state, or filesystem access. Return every input id exactly once as a list
of integers. Rank by relative evidence value: preferring short blocks can lose
long but necessary evidence. Scores range from 0 to 100; invalid programs receive
a negative score. The byte budget and dependency closure are fixed outside your
function, and the same candidate must produce identical results on replay.
