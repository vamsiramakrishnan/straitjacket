# Minimize wasted recovery attempts

Evolve `choose_recovery(state) -> action_id` after a host attempt fails or
explicitly reports non-completion.

Use the typed `failure_kind`, attempt count, remaining dollar budget, and the
provided action metadata. Missing evidence should trigger focused retrieval;
an incomplete contract or failed verification should replan; a genuine model
capability limit may escalate; a first transient transport failure may retry.
Authentication, permission, and safety denials cannot be repaired by spending
on a stronger model and must stop with an honest blocked result. Never exceed
the remaining budget.

Correct task recovery or correct terminal disposition is a hard gate. Only
then minimize added dollars, model attempts, and latency. Return an action ID
from `state['actions']`. Keep the policy deterministic and pure. Change only
the single `EVOLVE-BLOCK`.
