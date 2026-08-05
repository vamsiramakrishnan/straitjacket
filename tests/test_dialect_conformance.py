"""Acceptance: the hook dialect table and the host registry agree.

Two modules describe what each host's hook contract can enforce:

* ``ctx.hosts.HostSpec.input_substitution`` / ``.output_substitution`` — the
  declarative registry the orchestrator and `ctx doctor` read;
* ``ctx.hook.DIALECT_CAPS`` — the table the hot path actually branches on.

They are duplicated on purpose: :mod:`ctx.hook` has a latency contract that
forbids importing :mod:`ctx.hosts` (and with it the price table) on the
per-call path. Duplication is only safe if drift is impossible, and this
project has already shipped that exact bug once — an *assumed* Antigravity
contract that the two modules each described differently, which is how a
PostToolUse emitter came to advertise a substitution the host never supported.

So this suite is the join: every harnessable host must appear in the dialect
table under its declared flavor, with byte-identical capabilities.
"""

import pytest

from ctx.hook import DIALECT_CAPS, can_substitute_input, can_substitute_output
from ctx.hosts import all_hosts

# Only hosts harnessed *through hooks* need a dialect entry. `antigravity-sdk`
# is harnessable without one: it is ctx's own agent, and its containment lives
# in the tool implementations rather than in a hook contract, so there is no
# wire dialect to conform to.
HARNESSABLE = [h for h in all_hosts() if h.harnessable and h.supports_hooks]


def test_every_harnessable_host_has_a_dialect_entry():
    for spec in HARNESSABLE:
        assert spec.flavor in DIALECT_CAPS, (
            f"host {spec.name!r} is harnessable with hook flavor {spec.flavor!r} "
            f"but ctx.hook.DIALECT_CAPS has no entry for it — the hot path would "
            f"silently treat it as incapable of any substitution"
        )


def test_no_orphan_dialects():
    flavors = {h.flavor for h in HARNESSABLE}
    for flavor in DIALECT_CAPS:
        assert flavor in flavors, (
            f"DIALECT_CAPS declares {flavor!r}, which no harnessable host claims"
        )


@pytest.mark.parametrize("spec", HARNESSABLE, ids=lambda s: s.name)
def test_capabilities_match_the_registry(spec):
    caps = DIALECT_CAPS[spec.flavor]
    assert caps["input_substitution"] == spec.input_substitution, (
        f"{spec.name}: hosts.py says input_substitution="
        f"{spec.input_substitution}, hook.py says {caps['input_substitution']}"
    )
    assert caps["output_substitution"] == spec.output_substitution, (
        f"{spec.name}: hosts.py says output_substitution="
        f"{spec.output_substitution}, hook.py says {caps['output_substitution']}"
    )
    # and the accessors the hot path actually calls agree with both
    assert can_substitute_input(spec.flavor) is spec.input_substitution
    assert can_substitute_output(spec.flavor) is spec.output_substitution


def test_antigravity_is_the_one_host_without_an_output_gate():
    """Pins the consequence of the published contract (ADR 005) rather than
    leaving it as prose: if a future release gives that host a substitution
    field, this test is where the claim gets revisited deliberately."""
    assert can_substitute_output("antigravity") is False
    assert can_substitute_input("antigravity") is False
    assert can_substitute_output("claude-code") is True
    assert can_substitute_output("codex") is True


def test_unknown_flavor_fails_safe():
    """An unrecognised dialect must be treated as incapable, never as capable —
    the costly error is believing a substitution landed when it did not."""
    assert can_substitute_output("some-new-host") is False
    assert can_substitute_input("some-new-host") is False
