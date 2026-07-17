"""Generalized reference grammar (SPEC §6.1).

    run:7bd91f2a4c3d           captured invocation manifest
    run:7bd91f2a4c3d#stdout    exact stdout stream
    blob:fe21c91ad4e8          raw immutable content
    repo:                      current workspace
    repo:src/service.py        current file (snapshot-on-read)
    repo:services/payments     current subtree
    ws:api/repo:src/main.py    explicit workspace alias
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class RefError(Exception):
    pass


@dataclass(frozen=True)
class Ref:
    kind: str  # run | blob | repo | snapshot
    id: str | None = None  # short or full hex for run/blob/snapshot
    stream: str | None = None  # stdout | stderr for run refs
    path: str | None = None  # repo-relative path for repo refs
    workspace_alias: str | None = None

    def display(self) -> str:
        if self.kind == "repo":
            base = f"repo:{self.path or ''}"
        else:
            base = f"{self.kind}:{(self.id or '')[:12]}"
            if self.stream:
                base += f"#{self.stream}"
        if self.workspace_alias:
            return f"ws:{self.workspace_alias}/{base}"
        return base


_HEX_RE = re.compile(r"^[0-9a-fA-F]{6,64}$")
_WS_RE = re.compile(r"^ws:(?P<alias>[A-Za-z0-9_.-]+)/(?P<rest>.+)$")


def parse_ref(text: str) -> Ref:
    text = text.strip()
    alias = None
    m = _WS_RE.match(text)
    if m:
        alias = m.group("alias")
        text = m.group("rest")

    if text == "repo:" or text == "repo":
        return Ref(kind="repo", path=None, workspace_alias=alias)
    if text.startswith("repo:"):
        path = text[len("repo:") :].strip("/")
        if ".." in path.split("/"):
            raise RefError(f"repo reference must not contain '..': {text!r}")
        return Ref(kind="repo", path=path or None, workspace_alias=alias)

    for kind in ("run", "blob", "snapshot", "search"):
        prefix = kind + ":"
        if text.startswith(prefix):
            rest = text[len(prefix) :]
            stream = None
            if "#" in rest:
                rest, stream = rest.split("#", 1)
                if stream not in ("stdout", "stderr"):
                    raise RefError(f"unknown stream {stream!r}; use #stdout or #stderr")
            rest = rest.removeprefix("sha256:")
            if not _HEX_RE.match(rest):
                raise RefError(
                    f"invalid {kind} id {rest!r}: need 6-64 hex characters"
                )
            if stream and kind != "run":
                raise RefError(f"streams only apply to run: references, got {text!r}")
            return Ref(kind=kind, id=rest.lower(), stream=stream, workspace_alias=alias)

    raise RefError(
        f"unrecognized reference {text!r}; expected run:<id>[#stdout|#stderr], "
        "blob:<id>, snapshot:<id>, repo:[path], or ws:<alias>/repo:<path>"
    )
