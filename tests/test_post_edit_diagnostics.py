"""Post-edit checks prove which document bytes their diagnostics describe."""

from __future__ import annotations

import json

import pytest

from ctx.post_edit_diagnostics import (
    MAX_DIAGNOSTICS,
    Diagnostic,
    DiagnosticSnapshot,
    capture_baseline,
    load_receipt,
    verify_post_edit,
)


def test_valid_python_produces_a_fresh_content_addressed_receipt(
    state_home, workspace_dir
):
    target = workspace_dir / "example.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    baseline = capture_baseline(workspace_dir, "example.py")
    target.write_text("answer = 2\n", encoding="utf-8")

    first = verify_post_edit(workspace_dir, target, baseline)
    second = verify_post_edit(workspace_dir, target, baseline)

    assert first == second
    assert first["changeObserved"] is True
    assert first["outcome"] == "clean"
    assert first["checks"][0]["freshness"] == "fresh"
    assert load_receipt(workspace_dir, first["receiptId"]) == first


def test_python_syntax_issue_is_fresh_and_does_not_create_bytecode(
    state_home, workspace_dir
):
    target = workspace_dir / "broken.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    baseline = capture_baseline(workspace_dir, target)
    target.write_text("def broken(:\n", encoding="utf-8")

    receipt = verify_post_edit(workspace_dir, target, baseline)

    assert receipt["outcome"] == "issues"
    assert receipt["checks"][0]["diagnostics"][0]["code"] == "syntax-error"
    assert not (workspace_dir / "__pycache__").exists()


@pytest.mark.parametrize(
    "name,before,after,source",
    [
        ("data.json", '{"ok": true}\n', '{"ok": }\n', "json-parser"),
        ("config.toml", 'name = "ok"\n', "name = [\n", "toml-parser"),
    ],
)
def test_structured_document_parsers_report_fresh_issues(
    state_home, workspace_dir, name, before, after, source
):
    target = workspace_dir / name
    target.write_text(before, encoding="utf-8")
    baseline = capture_baseline(workspace_dir, target)
    target.write_text(after, encoding="utf-8")

    receipt = verify_post_edit(workspace_dir, target, baseline)

    assert receipt["outcome"] == "issues"
    assert receipt["checks"][0]["source"] == source
    assert receipt["checks"][0]["freshness"] == "fresh"


def test_available_but_unproven_external_diagnostics_are_stale(
    state_home, workspace_dir
):
    target = workspace_dir / "app.ts"
    target.write_text("const value = 1;\n", encoding="utf-8")
    baseline = capture_baseline(
        workspace_dir,
        target,
        diagnostics=DiagnosticSnapshot(source="lsp", version=7),
    )
    target.write_text("const value = 2;\n", encoding="utf-8")

    def same_version(path, captured):
        return DiagnosticSnapshot(source="typescript-lsp", version=7)

    receipt = verify_post_edit(
        workspace_dir,
        target,
        baseline,
        providers=[same_version],
        run_builtin=False,
    )

    assert receipt["outcome"] == "stale"
    assert receipt["checks"][0]["reason"] == "freshness_unproven"


def test_advanced_lsp_version_is_fresh_when_digest_is_not_available(
    state_home, workspace_dir
):
    target = workspace_dir / "app.ts"
    target.write_text("const value = 1;\n", encoding="utf-8")
    baseline = capture_baseline(
        workspace_dir,
        target,
        diagnostics=DiagnosticSnapshot(source="lsp", version=7),
    )
    target.write_text("const value = nope;\n", encoding="utf-8")

    def advanced(path, captured):
        return DiagnosticSnapshot(
            source="typescript-lsp",
            version=8,
            diagnostics=(Diagnostic("unknown identifier", code="ts2304"),),
        )

    receipt = verify_post_edit(
        workspace_dir,
        target,
        baseline,
        providers=[advanced],
        run_builtin=False,
    )

    assert receipt["outcome"] == "issues"
    assert receipt["checks"][0]["freshness"] == "fresh"


@pytest.mark.parametrize("current", [6, "different-opaque-version"])
def test_regressed_or_unordered_versions_are_not_fresh(
    state_home, workspace_dir, current
):
    target = workspace_dir / "app.ts"
    target.write_text("const value = 1;\n", encoding="utf-8")
    baseline = capture_baseline(
        workspace_dir,
        target,
        diagnostics=DiagnosticSnapshot(source="lsp", version=7),
    )
    target.write_text("const value = 2;\n", encoding="utf-8")

    def provider(path, captured):
        return DiagnosticSnapshot(source="typescript-lsp", version=current)

    receipt = verify_post_edit(
        workspace_dir,
        target,
        baseline,
        providers=[provider],
        run_builtin=False,
    )

    assert receipt["outcome"] == "stale"
    assert receipt["checks"][0]["reason"] == "freshness_unproven"


def test_provider_failures_are_bounded_unavailable_evidence(
    state_home, workspace_dir
):
    target = workspace_dir / "app.ts"
    target.write_text("const value = 1;\n", encoding="utf-8")
    baseline = capture_baseline(workspace_dir, target)

    def unavailable(path, captured):
        raise TimeoutError("secret provider details must not reach the receipt")

    receipt = verify_post_edit(
        workspace_dir,
        target,
        baseline,
        providers=[unavailable],
        run_builtin=False,
    )

    assert receipt["outcome"] == "unavailable"
    assert receipt["checks"][0]["reason"] == "provider_error:TimeoutError"
    assert "secret provider details" not in json.dumps(receipt)


def test_diagnostic_payload_is_bounded_but_full_set_has_a_fingerprint(
    state_home, workspace_dir
):
    target = workspace_dir / "app.ts"
    target.write_text("const value = 1;\n", encoding="utf-8")
    baseline = capture_baseline(workspace_dir, target)
    digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()

    def noisy(path, captured):
        return DiagnosticSnapshot(
            source="noisy-lsp",
            document_digest=digest,
            diagnostics=tuple(
                Diagnostic(
                    f"problem {index}",
                    severity="error" if index == 79 else "warning",
                )
                for index in range(80)
            ),
        )

    receipt = verify_post_edit(
        workspace_dir,
        target,
        baseline,
        providers=[noisy],
        run_builtin=False,
    )
    check = receipt["checks"][0]

    assert check["diagnosticCount"] == 80
    assert len(check["diagnostics"]) == MAX_DIAGNOSTICS
    assert check["omittedDiagnostics"] == 30
    assert check["hasErrors"] is True
    assert receipt["outcome"] == "issues"
    assert len(check["diagnosticFingerprint"]) == 64


def test_target_must_stay_inside_the_workspace(state_home, workspace_dir, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("ok = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inside the workspace"):
        capture_baseline(workspace_dir, outside)
