from ctx.context_archive_policy import choose_archive_tier


def _safe(**overrides):
    state = {
        "exact_evidence_secured": True,
        "exact_retrieval_address": True,
        "host_image_capable": True,
        "provider": "google",
        "at_compaction_boundary": True,
        "quiet_needle_gate": True,
        "structure_recall_gate": True,
        "contains_secrets": False,
        "provider_image_budget": 8,
        "estimated_frames": 4,
        "cold_tokens": 80_000,
    }
    state.update(overrides)
    return state


def test_unsecured_evidence_remains_inline():
    assert choose_archive_tier({}) == "inline_text"
    assert choose_archive_tier(_safe(exact_evidence_secured=False)) == "inline_text"


def test_visual_tier_requires_every_admission_gate():
    assert choose_archive_tier(_safe()) == "visual_cold"
    for key, value in (
        ("host_image_capable", False),
        ("at_compaction_boundary", False),
        ("quiet_needle_gate", False),
        ("structure_recall_gate", False),
        ("contains_secrets", True),
    ):
        assert choose_archive_tier(_safe(**{key: value})) == "address_only"


def test_visual_tier_requires_measured_provider_budget_and_scale():
    assert choose_archive_tier(_safe(provider="unknown")) == "address_only"
    assert choose_archive_tier(_safe(provider_image_budget=3)) == "address_only"
    assert choose_archive_tier(_safe(cold_tokens=23_999)) == "address_only"
