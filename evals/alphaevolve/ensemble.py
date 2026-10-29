"""Managed-generation model mixture for AlphaEvolve campaigns.

Google's alphaevolve-on-googlecloud client blends a fast and a deep Gemini
model by weight (for example flash 0.7 / pro 0.3). This module builds that
mixture for every straitjacket campaign from one CLI flag or environment, so
search depth is configurable without editing experiment packs.

Supported generation models and location rules follow Google's documented
configuration surface; ``gemini-3.1-pro-preview`` is served from ``global``
only. Unknown model names fail closed before any billed experiment starts.
"""

from __future__ import annotations

import os
import math
from typing import Any

KNOWN_MODELS = frozenset({"gemini-4-argon", "gemini-3.8-flash", "gemini-3.7-flash",
                          "gemini-3.5-flash", "gemini-3.1-pro-preview"})
GLOBAL_ONLY_MODELS = frozenset({"gemini-3.1-pro-preview"})
DEFAULT_MODEL = "gemini-3.8-flash"


def parse_model_mix(spec: str) -> list[dict[str, Any]]:
    """Parse ``name:weight[,name:weight...]`` into generation settings entries."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, separator, raw_weight = part.rpartition(":")
        if not separator or not name:
            raise ValueError(
                f"model entry {part!r} must look like 'model-name:weight'"
            )
        name = name.strip()
        try:
            weight = float(raw_weight.strip())
        except ValueError as exc:
            raise ValueError(f"model entry {part!r} has a non-numeric weight") from exc
        if name not in KNOWN_MODELS:
            raise ValueError(
                f"unsupported model {name!r}; supported: {', '.join(sorted(KNOWN_MODELS))}"
            )
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"model {name!r} weight must be positive")
        if name in seen:
            raise ValueError(f"model {name!r} appears twice in the mixture")
        seen.add(name)
        entries.append({"name": name, "weight": weight})
    if not entries:
        raise ValueError("model mixture is empty")
    return entries


def _env(name: str) -> str:
    return os.environ.get(name) or ""


def model_mixture(cli_spec: str | None = None, location: str | None = None) -> list[dict[str, Any]]:
    """Build ``generation_settings.models`` from CLI, env, or the default."""
    spec = cli_spec or _env("MODEL_MIX")
    if not spec:
        first = _env("MODEL_1") or _env("MODEL")
        second = _env("MODEL_2")
        parts: list[str] = []
        if first:
            weight = _env("MODEL_1_WEIGHT") or _env("MODEL_WEIGHT")
            parts.append(f"{first}:{weight}" if weight else first)
        if second:
            weight = _env("MODEL_2_WEIGHT")
            parts.append(f"{second}:{weight}" if weight else second)
        spec = ",".join(parts)
    entries = parse_model_mix(spec) if spec else [{"name": DEFAULT_MODEL, "weight": 1.0}]

    resolved_location = location or _env("LOCATION") or "global"
    for entry in entries:
        if entry["name"] in GLOBAL_ONLY_MODELS and resolved_location != "global":
            raise ValueError(
                f"{entry['name']} is served from 'global' only; "
                f"set LOCATION=global or drop it from the mixture"
            )
    scale = max(entry["weight"] for entry in entries)
    total = sum(entry["weight"] / scale for entry in entries)
    return [
        {"name": entry["name"], "weight": (entry["weight"] / scale) / total} for entry in entries
    ]
