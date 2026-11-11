"""The one definition of a store handle's grammar.

``ctx.refs.parse_ref`` (every verb that takes a handle) and the hook's
approval of ctx's own commands both import these patterns, so the hook can
never call something a handle that the commands would not parse as one.
Hot path: imports ``re`` only.
"""

from __future__ import annotations

import re

#: A store id: 6 to 64 hexadecimal characters (``sha256:`` may precede it).
HEX = r"[0-9a-fA-F]{6,64}"
HEX_RE = re.compile(rf"^{HEX}$")
#: Kinds ``parse_ref`` accepts by id.
HANDLE_KINDS = ("run", "blob", "snapshot", "search", "checkpoint")
#: ``run:ID[#stdout|#stderr]`` exactly.
RUN_HANDLE_RE = re.compile(rf"run:(?:sha256:)?{HEX}(?:#(?:stdout|stderr))?")
#: Any store handle; a stream suffix only on ``run:``.
ANY_HANDLE_RE = re.compile(
    rf"(?:run:(?:sha256:)?{HEX}(?:#(?:stdout|stderr))?"
    rf"|(?:blob|snapshot|search|checkpoint):(?:sha256:)?{HEX})")
#: A token that LOOKS like a handle (a known kind prefix); if it does not
#: also match ANY_HANDLE_RE it is malformed and is neither handle nor path.
HANDLE_PREFIX_RE = re.compile(r"(?:run|blob|snapshot|search|checkpoint|span|sha256):")
#: A span id as ``Store.span_id_for`` mints it.
SPAN_ID_RE = re.compile(r"[0-9a-f]{10}")

__all__ = ["HEX", "HEX_RE", "HANDLE_KINDS", "RUN_HANDLE_RE", "ANY_HANDLE_RE",
           "HANDLE_PREFIX_RE", "SPAN_ID_RE"]
