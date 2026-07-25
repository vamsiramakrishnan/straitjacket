"""Constants that exist in both Python and Rust (R8).

The native post-tool-use shim (``native/ctx-hook-native``) reimplements
``ctx.hook.main_post_tool_use`` in Rust for the ~29 ms CPython startup it
saves. Reimplementation means every literal it shares with Python is a
duplicate that can drift, and ``tests/test_native_hook.py`` — the only thing
that would catch it — SKIPS whenever the binary has not been built. These
tests read the sources instead, so they run on every machine, built or not.

* ``emission_nudge_tokens``'s default (R8). It appeared as a bare literal in
  four places across the two languages. Each language now names it once
  (``ctx.engagement.EMISSION_NUDGE_TOKENS_DEFAULT`` and
  ``EMISSION_NUDGE_TOKENS_DEFAULT`` in ``main.rs``); this test is what keeps
  the two names equal.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN_RS = REPO / "native" / "ctx-hook-native" / "src" / "main.rs"
SRC = REPO / "src" / "ctx"


def _rs() -> str:
    return MAIN_RS.read_text(encoding="utf-8")


# ------------------------------------------------------------------ R8
def test_emission_nudge_default_is_named_once_per_language():
    from ctx.config import Engagement
    from ctx.engagement import EMISSION_NUDGE_TOKENS_DEFAULT
    from ctx.hook import _load_guard_policy

    # Python: one constant, every reader derives from it.
    assert Engagement().emission_nudge_tokens == EMISSION_NUDGE_TOKENS_DEFAULT
    assert (
        _load_guard_policy(None)["emission_nudge_tokens"]
        == EMISSION_NUDGE_TOKENS_DEFAULT
    )

    # Rust: one named const, and it equals the Python one.
    m = re.search(
        r"const EMISSION_NUDGE_TOKENS_DEFAULT: i64 = ([0-9_]+);", _rs()
    )
    assert m, "main.rs must name the default as EMISSION_NUDGE_TOKENS_DEFAULT"
    assert int(m.group(1).replace("_", "")) == EMISSION_NUDGE_TOKENS_DEFAULT


def test_no_bare_emission_default_literal_survives_in_python():
    """The literal used to appear in config.py and twice in hook.py. Only
    ctx.engagement may spell the number; every other mention of
    ``emission_nudge_tokens`` must reference the constant."""
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name == "engagement.py":
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "emission_nudge_tokens" not in line:
                continue
            # A literal default on the same line as the key is the drift risk.
            if re.search(r"emission_nudge_tokens[^#]*?\b\d{3,}\b", line):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "the emission-nudge default must come from "
        f"ctx.engagement.EMISSION_NUDGE_TOKENS_DEFAULT, not a literal: {offenders}"
    )


def test_ctx_toml_collapse_has_one_python_reader():
    """`[guard] collapse` was read by three separate Python parsers
    (config.load_config, hook._load_guard_policy, wrap._collapse_enabled).
    wrap now defers to the typed loader; hook's stdlib-only hot-path reader
    stays and is pinned by tests/test_config_hook_parity.py."""
    text = (SRC / "wrap.py").read_text(encoding="utf-8")
    assert "import tomllib" not in text, "ctx.wrap must not parse ctx.toml itself"


def test_wrap_collapse_matches_the_typed_loader(tmp_path):
    from ctx.config import load_config
    from ctx.wrap import _collapse_enabled

    for body, expected in (
        (None, True),                                   # no ctx.toml
        ("version = 1\n", True),                        # unset
        ("version = 1\n[guard]\ncollapse = false\n", False),
        ("version = 1\n[guard]\ncollapse = true\n", True),
        ("this is not toml [[[\n", True),               # malformed → fail-open
    ):
        root = tmp_path / f"w{abs(hash(body))}"
        root.mkdir()
        if body is not None:
            (root / "ctx.toml").write_text(body, encoding="utf-8")
        assert _collapse_enabled(root) is expected
        if body is not None:
            assert load_config(root).guard.collapse is expected
