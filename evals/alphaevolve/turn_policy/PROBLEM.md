# Minimize turns while completing the evidence journey

Evolve `choose_action(state)` to select exactly one available bounded operation.
A hidden deterministic simulator applies that operation, reveals facts, and
calls the policy again until the task is complete or its turn cap is exhausted.

Complete every task. Prefer compound and structural operations (`fails_last`,
`map`, `refs`, `diff`) when they answer the current uncertainty in one call;
use focused retrieval only after a target is known; verify after enough evidence
exists. Avoid repeated, irrelevant, unsafe, or broad raw reads.

The completion gate dominates. Among completing trajectories, higher score
means fewer turns and fewer model-visible tokens. State contains the task goal,
signals, already-known facts, history, and available action metadata. Return an
action ID from `available_actions`. Change only the single `EVOLVE-BLOCK`.
