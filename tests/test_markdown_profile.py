"""Tests for markdown/v1 digest profile, whole-file Markdown outlines, and
universal delimited/v1 detection across tool outputs."""

from __future__ import annotations

import re
import time

import pytest
from conftest import make_store, make_ws
from shape_helpers import KEY20, SECRET, assert_in_budget, csv_text

from ctx.digest import digest_output
from ctx.digest.mdprof import looks_like_markdown, outline_file, parse_markdown
from ctx.intake import intake_bytes
from ctx.retrieval import Selector, get


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def _rich_markdown(sections: int = 12, include_frontmatter: bool = True) -> str:
    out: list[str] = []
    if include_frontmatter:
        out.extend([
            "---",
            "name: platform-architecture",
            "title: Platform Architecture Design Doc",
            "description: End-to-end storage and retrieval plan",
            "---",
            "",
        ])
    out.append("# Platform Architecture\n")
    out.append("This document describes ERROR handling, CRITICAL retry budgets, and FAILURE recovery.\n")
    for i in range(1, sections + 1):
        out.append(f"## Section {i}: Subsystem {i}\n")
        out.append(f"- Key requirement {i} with **bold** emphasis and `inline_code`.\n")
        out.append(f"- Reference link: [spec-{i}](https://example.com/spec/{i}).\n")
        out.append(
            "Prose paragraph discussing ERROR rates, TIMEOUT budgets, and CRITICAL alerts "
            f"for subsystem {i}. " * 12 + "\n"
        )
        if i % 3 == 0:
            out.extend([
                "### Metrics Table",
                "| Metric | Target | Notes |",
                "| --- | --- | --- |",
                f"| latency_p99 | 120ms | subsystem {i} |",
                "",
                "```python",
                "# Not a markdown heading inside a code fence",
                "## Also not a heading inside a code fence",
                f"def handler_{i}(x):",
                "    return x + 1",
                "```",
                "",
            ])
    return "\n".join(out) + "\n"


def test_markdown_profile_digests_tool_output_with_heading_outline(env):
    ws, store = env
    text = _rich_markdown(14)
    digest, short = digest_output(store, ws, "read_file", text)

    assert digest.splitlines()[0].endswith("profile=markdown/v1]")
    assert "frontmatter:" in digest
    assert 'name="platform-architecture"' in digest
    assert "markdown:" in digest and "headings" in digest
    assert "tables" in digest and "code blocks" in digest
    assert "evidence map" not in digest.lower()
    assert "# Not a markdown heading" not in digest
    assert "## Also not a heading" not in digest
    assert len(digest.encode("utf-8")) <= ws.config.budgets.digest_tokens * 4

    # Follow-up --lines command resolves accurately.
    m = re.search(r"ctx get (run:[0-9a-f]+#stdout) --lines (\d+):(\d+)", digest)
    assert m, digest
    ref, a, b = m.group(1), int(m.group(2)), int(m.group(3))
    out = get(store, ws, ref, Selector(lines=(a, b)))
    assert "# Platform Architecture" in out


def test_markdown_profile_ignores_headings_inside_tilde_and_backtick_fences():
    text = (
        "# Real Title\n\n"
        "## Real Section\n\n"
        "- item one\n\n"
        "~~~bash\n"
        "# Comment inside tilde fence\n"
        "## Subcomment inside tilde fence\n"
        "echo ok\n"
        "~~~\n\n"
        "## Final Section\n\n"
        "Body text.\n"
    )
    doc = parse_markdown(text)
    assert [s.title for s in doc.sections] == ["Real Title", "Real Section", "Final Section"]
    assert doc.code_blocks == 1
    assert looks_like_markdown(doc)


def test_markdown_profile_declines_python_and_shell_source_files(env):
    ws, store = env
    py_src = (
        "# Module header\n"
        "## Section 1\n"
        "import os\nimport sys\n\n"
        + "\n".join(
            f"# Helper {i}\ndef compute_{i}(x):\n    return x + {i}\n"
            for i in range(120)
        )
    )
    sh_src = (
        "#!/usr/bin/env bash\n"
        "# Main script\n"
        "## Setup\n"
        "set -euo pipefail\n"
        + "\n".join(f"# Step {i}\necho step_{i}\n" for i in range(250))
    )
    for src in (py_src, sh_src):
        digest, _ = digest_output(store, ws, "read_file", src)
        assert "profile=markdown/v1]" not in digest.splitlines()[0]


def test_markdown_profile_declines_small_documents(env):
    ws, store = env
    small = "# Title\n\n## Intro\n\n- short note\n"
    digest, _ = digest_output(store, ws, "read_file", small)
    assert "profile=markdown/v1]" not in digest.splitlines()[0]


def test_markdown_profile_redacts_secrets_in_headings_and_frontmatter(env):
    ws, store = env
    text = (
        "---\n"
        f"title: Spec for {KEY20}\n"
        "---\n\n"
        f"# Architecture {SECRET}\n\n"
        "## Details\n\n"
        + ("- Bullet item with `code` and **text**.\n" * 200)
    )
    digest, _ = digest_output(store, ws, "read_file", text)
    assert digest.splitlines()[0].endswith("profile=markdown/v1]")
    assert KEY20 not in digest
    assert SECRET not in digest
    assert "redaction: applied" in digest


def test_markdown_profile_works_for_intake_and_fits_budget(env):
    ws, store = env
    text = _rich_markdown(80)
    rec = intake_bytes(store, ws, text.encode("utf-8"), label="prd")
    assert rec.profile == "markdown/v1"
    assert "more headings omitted" in rec.digest
    assert_in_budget(ws, rec)


def test_markdown_profile_linear_time_on_pathological_input(env):
    ws, store = env
    huge = ("# " + "a" * 200_000 + "\n## " + "b" * 200_000 + "\n- x\n")
    t0 = time.perf_counter()
    digest, _ = digest_output(store, ws, "read_file", huge)
    assert time.perf_counter() - t0 < 5.0
    assert len(digest.encode("utf-8")) <= ws.config.budgets.digest_tokens * 4


def test_outline_file_returns_bounded_outline(tmp_path):
    md = tmp_path / "design.md"
    md.write_text(_rich_markdown(10), encoding="utf-8")
    from ctx.config import Redaction

    out = outline_file(str(md), Redaction())
    assert out is not None
    assert "frontmatter:" in out
    assert "L" in out and "# \"Platform Architecture\"" in out


def test_delimited_profile_digests_csv_tool_output(env):
    ws, store = env
    text = csv_text(300)
    digest, short = digest_output(store, ws, "read_file", text)
    assert digest.splitlines()[0].endswith("profile=delimited/v1]")
    assert "delimited: comma · 6 columns" in digest
    assert "300 data rows (exact)" in digest
    assert "command: read_file" in digest
    assert len(digest.encode("utf-8")) <= ws.config.budgets.digest_tokens * 4
