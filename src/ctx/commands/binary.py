"""Typed binary evidence commands: image digest and image render comparison."""

from __future__ import annotations

import sys


def _read_workspace_file(ws, value: str) -> tuple[str, bytes] | None:
    """Read one policy-confined file and return its stable relative path."""
    try:
        path = ws.confine(value, must_exist=True)
        relative = ws.relativize(path)
        if ws.is_ignored(relative):
            print(
                f"ctx image: path is excluded from capture by policy: {relative}",
                file=sys.stderr,
            )
            return None
        return relative, path.read_bytes()
    except OSError as exc:
        print(f"ctx image: cannot read {value}: {exc}", file=sys.stderr)
        return None


def cmd_image(ws, ns) -> int:
    """`ctx image digest|diff` — bounded structure, identity, and dHash delta."""
    from ctx import binfmt

    if ns.image_cmd == "digest":
        for value in ns.files:
            loaded = _read_workspace_file(ws, value)
            if loaded is None:
                return 1
            relative, data = loaded
            print(f"[{relative}]")
            print(binfmt.render_digest(binfmt.inspect(data)))
        return 0

    if len(ns.files) != 2:
        print("ctx image: diff requires exactly two files", file=sys.stderr)
        return 2

    loaded = [_read_workspace_file(ws, value) for value in ns.files]
    if any(item is None for item in loaded):
        return 1
    (a_path, a_data), (b_path, b_data) = loaded  # type: ignore[misc]
    a_info, b_info = binfmt.inspect(a_data), binfmt.inspect(b_data)
    if not a_info.perceptual_hash or not b_info.perceptual_hash:
        print(
            "ctx image: diff requires two decodable images and the `image` extra "
            "(pip install 'ctx-harness[image]')",
            file=sys.stderr,
        )
        return 1

    distance = binfmt.phash_distance(a_info.perceptual_hash, b_info.perceptual_hash)
    if distance is None:  # defensive: inspect() emitted validated 64-bit hashes
        print("ctx image: invalid dHash value", file=sys.stderr)
        return 1
    verdict = (
        "identical dHash" if distance == 0
        else "near-identical dHash" if distance <= 5
        else "minor dHash change" if distance <= 12
        else "substantial dHash change"
    )
    print(f"a: {a_path} · {a_info.width}×{a_info.height} {a_info.format} · {a_info.perceptual_hash}")
    print(f"b: {b_path} · {b_info.width}×{b_info.height} {b_info.format} · {b_info.perceptual_hash}")
    print(f"dhash distance: {distance}/64 — {verdict}")
    if a_info.sha256 == b_info.sha256:
        print("byte identity: equal")
    else:
        print("byte identity: different")
    return 0


__all__ = ["cmd_image"]
