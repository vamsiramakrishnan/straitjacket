"""Previewable workspace configuration for Codex and OpenCode.

A plan is an immutable byte snapshot plus surgical source edits. No host is
launched. Ownership is per contribution, not inferred from a file's banner.
Readiness receipts remain content-free; this separate, workspace-local ledger
contains only owned values and inverse edit fragments for removal.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ctx.config_edit import ConfigError, Document, JSONSource, MISSING, lookup

HOSTS = ("codex", "opencode")
LEDGER = ".ctx/host-config.json"
SCHEMA = "ctx.host-config/v1"


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _source_hash(doc, path, item):
    if item.get("array"):
        value = lookup(doc.data, path)
        path = tuple(path) + (value.index(item["value"]),)
    return _hash(doc.fragment(path).encode())


def _safe_path(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ConfigError(f"invalid workspace config path: {relative}")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ConfigError(f"refusing symlink: {relative}")
    return path


def _read(root, relative):
    path = _safe_path(root, relative)
    if path.exists() and not path.is_file():
        raise ConfigError(f"configuration is not a regular file: {relative}")
    return path.read_bytes() if path.exists() else None


def opencode_path(root):
    # Two project configs have ambiguous precedence. Never create a shadow file.
    existing = [p for p in ("opencode.json", "opencode.jsonc") if (root / p).exists() or (root / p).is_symlink()]
    if len(existing) > 1:
        raise ConfigError("Both opencode.json and opencode.jsonc exist; select one project config before setup (neither was changed).")
    return existing[0] if existing else "opencode.json"


def owned_hosts(root):
    """Presence signal only; doctor still validates every active component."""
    try:
        raw = _read(root, LEDGER)
        doc = json.loads(raw) if raw else {}
        return set(doc.get("hosts", {})) if doc.get("schema") == SCHEMA else set()
    except (ConfigError, OSError, ValueError, AttributeError, TypeError):
        return set()


@dataclass(frozen=True)
class Change:
    path: str
    before: bytes | None
    after: bytes | None

    def diff(self):
        before = (self.before or b"").decode("utf-8")
        after = (self.after or b"").decode("utf-8")
        lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="a/" + self.path if self.before is not None else "/dev/null",
            tofile="b/" + self.path if self.after is not None else "/dev/null", n=0)
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


@dataclass
class ConfigPlan:
    root: Path
    hosts: tuple[str, ...]
    remove: bool = False
    changes: list[Change] = field(default_factory=list)
    snapshots: dict = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    requires_review: bool = False

    def render(self):
        lines = ["ctx configuration " + ("removal" if self.remove else "preview")]
        for change in self.changes:
            if change.path == LEDGER:
                lines.append(f"ownership: {'remove' if change.after is None else 'update'} {LEDGER}")
                continue
            lines.append(change.diff().rstrip("\n"))
        if self.blockers:
            lines += ["Review required; --apply will not override these conflicts:", *self.blockers]
            lines.append("Reconcile the named entries using the proposed diff, then rerun ctx setup --preview.")
        elif not self.changes:
            lines.append("No configuration changes needed.")
        else:
            command = "ctx setup " + " ".join("--host " + h for h in self.hosts)
            lines.append("Apply: " + command + (" --remove" if self.remove else "") + " --apply")
        return "\n".join(lines)

    def apply(self):
        if self.blockers:
            raise ConfigError("\n".join(self.blockers))

        def check_snapshots():
            for relative, before in self.snapshots.items():
                if _read(self.root, relative) != before:
                    raise ConfigError(f"{relative} changed since preview; rerun ctx setup --preview")

        def stage(relative, data, mode):
            path = _safe_path(self.root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".ctx-config-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(name, mode)
                return Path(name)
            except BaseException:
                Path(name).unlink(missing_ok=True)
                raise

        # Stage every replacement before modifying the first file. Check all
        # reads, including the absence of OpenCode's alternate config, twice.
        check_snapshots()
        staged, modes, applied = {}, {}, []
        try:
            for change in self.changes:
                path = _safe_path(self.root, change.path)
                modes[change.path] = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
                if change.after is not None:
                    staged[change.path] = stage(change.path, change.after, modes[change.path])
            check_snapshots()
            for change in self.changes:
                path = _safe_path(self.root, change.path)
                if _read(self.root, change.path) != change.before:
                    raise ConfigError(f"{change.path} changed while applying; rerun preview")
                if change.after is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(staged[change.path], path)
                applied.append(change)
        except BaseException as exc:
            failed = []
            for change in reversed(applied):
                try:
                    if _read(self.root, change.path) != change.after:
                        raise ConfigError("concurrent edit; preserved")
                    path = _safe_path(self.root, change.path)
                    if change.before is None:
                        path.unlink(missing_ok=True)
                    else:
                        restore = stage(change.path, change.before, modes[change.path])
                        try:
                            os.replace(restore, path)
                        finally:
                            restore.unlink(missing_ok=True)
                except (OSError, ConfigError):
                    failed.append(change.path)
            if failed:
                raise ConfigError("Apply interrupted; these concurrently changed or unwritable files were preserved: " + ", ".join(failed) + "; run ctx doctor and preview repair") from exc
            raise
        finally:
            for path in staged.values():
                path.unlink(missing_ok=True)


def _undo(before, after):
    # Only replaced/inserted fragments, never a backup of the user's config.
    a, b = after.decode(), (before or b"").decode()
    return [[i, j, b[x:y]] for tag, i, j, x, y in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if tag != "equal"]


def _restore(after, edits):
    text = after.decode()
    for a, b, value in reversed(edits):
        text = text[:a] + value + text[b:]
    return text.encode()


class _Builder:
    def __init__(self, root, hosts, remove):
        self.plan = ConfigPlan(root, tuple(hosts), remove)
        raw = self.read(LEDGER)
        try:
            self.ledger = json.loads(raw) if raw else {"schema": SCHEMA, "hosts": {}}
            if self.ledger.get("schema") != SCHEMA or not isinstance(self.ledger.get("hosts"), dict):
                raise ValueError("unsupported ownership schema")
        except (ValueError, AttributeError) as exc:
            raise ConfigError(f"cannot read {LEDGER}: {exc}; restore its valid ownership ledger before setup/removal") from exc
        self.original = copy.deepcopy(self.ledger)

    def read(self, relative):
        self.current_path = relative
        raw = _read(self.plan.root, relative)
        self.plan.snapshots[relative] = raw
        return raw

    def finish_file(self, host, relative, before, after, items=None, parents=None, *, raw=False):
        if before == after:
            return
        files = self.ledger["hosts"].setdefault(host, {})
        old = files.get(relative, {})
        self.plan.changes.append(Change(relative, before, after))
        if before is not None and not old:
            self.plan.requires_review = True
        record = copy.deepcopy(old)
        record.setdefault("created", before is None)
        if items is not None:
            record["items"] = items
            record["parents"] = sorted([list(p) for p in parents], key=lambda p: (len(p), str(p)))
        record["raw"] = raw
        # Preserve exact clean removal across ctx-only upgrades. Once the user
        # edits surrounding text, removal uses semantic contributions instead.
        baseline = before
        if old:
            if before is not None and _hash(before) == old.get("hash") and "undo" in old:
                baseline = _restore(before, old["undo"])
                if old.get("created"):
                    baseline = None
            else:
                baseline = MISSING
        if baseline is not MISSING:
            record["undo"] = _undo(baseline, after)
        else:
            record.pop("undo", None)
        record["hash"] = _hash(after)
        files[relative] = record

    def document(self, host, relative, kind, requirements, *, initial=None):
        before = self.read(relative)
        text = before.decode("utf-8") if before is not None else (initial or ("" if kind == "toml" else "{}\n"))
        doc = Document(text, kind)
        old = self.ledger["hosts"].get(host, {}).get(relative, {})
        items = copy.deepcopy(old.get("items", []))
        parents = {tuple(p) for p in old.get("parents", [])}
        # A fresh full template owns its leaves (including its optional TUI).
        if before is None and initial:
            def leaves(value, prefix=()):
                for key, val in value.items():
                    path = prefix + (key,)
                    if isinstance(val, dict):
                        parents.add(path)
                        yield from leaves(val, path)
                    else:
                        item = {"path": list(path), "value": val}
                        item["source_hash"] = _source_hash(doc, path, item)
                        yield item
            items = list(leaves(doc.data))

        def ensure(path, wanted, *, array=False):
            nonlocal doc, items
            path = tuple(path)
            value = lookup(doc.data, path)
            prior = next((item for item in items if tuple(item["path"]) == path and bool(item.get("array")) == array), None)
            if wanted is MISSING:
                if value is not MISSING:
                    self.plan.blockers.append(f"{relative}: {'.'.join(path)} selects a remote transport; remove it only after reviewing the proposed local-server diff.")
                    doc = doc.edit(path)
                return
            if array:
                if not isinstance(value, list):
                    if value is MISSING:
                        ensure(path, [])
                        container = next((item for item in items if tuple(item["path"]) == path and not item.get("array")), None)
                        if container:
                            container["container"] = True
                        value = []
                    else:
                        self.plan.blockers.append(f"{relative}: {'.'.join(path)} must be an array; choose how to preserve its current value.")
                        doc = doc.edit(path, [])
                        value = []
                if wanted in value:
                    return
                # Existing ctx entries with a different command/matcher must
                # never silently become a second competing registration.
                stale = [i for i, group in enumerate(value) if isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(
                    isinstance(h, dict) and re.search(r"(?:^|\s)hook codex(?:\s|$)", str(h.get("command", "")))
                    for h in group["hooks"])]
                previous = next((item for item in items if item.get("array") and tuple(item["path"]) == path and item["value"] in value), None)
                if previous:
                    i = value.index(previous["value"])
                    if previous.get("source_hash") and _source_hash(doc, path, previous) != previous["source_hash"]:
                        self.plan.blockers.append(f"{relative}: owned {'.'.join(path)} hook source changed, including comments; review its exact diff.")
                    doc = doc.edit(path + (i,), wanted)
                    previous["value"] = wanted
                    previous["source_hash"] = _source_hash(doc, path, previous)
                    return
                if stale:
                    self.plan.blockers.append(f"{relative}: {'.'.join(path)} contains a different Codex ctx hook; reconcile it before applying the proposed registration.")
                    for index in reversed(stale):
                        hooks = lookup(doc.data, path + (index, "hooks"))
                        for j in reversed(range(len(hooks))):
                            hook = hooks[j]
                            if isinstance(hook, dict) and re.search(r"(?:^|\s)hook codex(?:\s|$)", str(hook.get("command", ""))):
                                doc = doc.edit(path + (index, "hooks", j))
                        if lookup(doc.data, path + (index, "hooks")) == []:
                            doc = doc.edit(path + (index,))
                doc = doc.edit(path, wanted, append=True)
                if prior:
                    prior["value"] = wanted
                    prior["source_hash"] = _source_hash(doc, path, prior)
                else:
                    item = {"path": list(path), "value": wanted, "array": True}
                    item["source_hash"] = _source_hash(doc, path, item)
                    items.append(item)
                return
            if value is not MISSING and type(value) is type(wanted) and value == wanted:
                return
            # A scalar/array ancestor is an explicit user conflict. Show the
            # exact replacement proposal, but never authorize it automatically.
            for n in range(1, len(path)):
                parent = lookup(doc.data, path[:n])
                if parent is MISSING:
                    parents.add(path[:n])
                elif not isinstance(parent, dict):
                    replacement = wanted
                    for key in reversed(path[n:]):
                        replacement = {key: replacement}
                    self.plan.blockers.append(f"{relative}: {'.'.join(path[:n])} is not a table/object; proposed replacement requires your reconciliation.")
                    doc = doc.edit(path[:n], replacement)
                    return
            if value is not MISSING and not (prior and type(value) is type(prior["value"]) and value == prior["value"]):
                self.plan.blockers.append(f"{relative}: {'.'.join(path)} conflicts with the required integration value; user setting preserved.")
            elif value is not MISSING and prior and prior.get("source_hash") and _source_hash(doc, path, prior) != prior["source_hash"]:
                self.plan.blockers.append(f"{relative}: owned {'.'.join(path)} source changed, including comments; review its exact diff.")
            doc = doc.edit(path, wanted)
            if prior:
                prior["value"] = wanted
                prior["source_hash"] = _source_hash(doc, path, prior)
            else:
                item = {"path": list(path), "value": wanted}
                item["source_hash"] = _source_hash(doc, path, item)
                items.append(item)

        for path, value, array in requirements:
            ensure(path, value, array=array)
        self.finish_file(host, relative, before, doc.text.encode(), items, parents)

    def raw_file(self, host, relative, expected, *, block=False):
        before = self.read(relative)
        after = expected.encode()
        old = self.ledger["hosts"].get(host, {}).get(relative, {})
        if block and before is not None:
            start, end = "<!-- ctx-harness:start -->", "<!-- ctx-harness:end -->"
            text = before.decode()
            if start in text or end in text:
                if text.count(start) != 1 or text.count(end) != 1 or text.index(start) >= text.index(end):
                    raise ConfigError(f"{relative}: ambiguous ctx-harness block markers; repair the marker pair before setup")
                a, b = text.index(start), text.index(end) + len(end)
                after = (text[:a] + expected.strip() + text[b:]).encode()
            else:
                sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
                after = (text + sep + expected.strip() + "\n").encode()
        elif before is not None and before != after and _hash(before) != old.get("hash"):
            self.plan.blockers.append(f"{relative}: existing unmanaged or modified plugin differs from the owned version; review its proposed diff before replacing it.")
        self.finish_file(host, relative, before, after, raw=True)

    def remove_host(self, host):
        files = self.ledger["hosts"].get(host, {})
        allowed = {"codex": {".codex/config.toml", ".codex/hooks.json", "AGENTS.md"},
                   "opencode": {"opencode.json", "opencode.jsonc", ".opencode/plugins/straitjacket.js"}}[host]
        for relative, record in files.items():
            if relative not in allowed:
                raise ConfigError(f"invalid owned path for {host}: {relative}")
            before = self.read(relative)
            if before is None:
                continue
            if _hash(before) == record.get("hash") and "undo" in record:
                after = None if record["created"] else _restore(before, record["undo"])
            elif record.get("raw"):
                if relative == "AGENTS.md":
                    text = before.decode()
                    start, end = "<!-- ctx-harness:start -->", "<!-- ctx-harness:end -->"
                    if text.count(start) == text.count(end) == 1 and text.index(start) < text.index(end):
                        a, b = text.index(start), text.index(end) + len(end)
                        after = (text[:a] + text[b:]).encode()
                    elif start not in text and end not in text:
                        after = before
                    else:
                        raise ConfigError(f"{relative}: ambiguous managed block; preserve it and reconcile markers")
                else:
                    self.plan.blockers.append(f"{relative}: owned plugin was modified; preserve it and reconcile before removal.")
                    continue
            else:
                kind = "toml" if relative.endswith(".toml") else "json"
                doc = Document(before.decode(), kind)
                for item in reversed(record.get("items", [])):
                    path, wanted = tuple(item["path"]), item["value"]
                    actual = lookup(doc.data, path)
                    if actual is MISSING:
                        continue
                    owned_value_present = (item["value"] in actual if item.get("array") and isinstance(actual, list)
                                           else type(actual) is type(wanted) and actual == wanted)
                    if (owned_value_present and not item.get("container") and item.get("source_hash")
                            and _source_hash(doc, path, item) != item["source_hash"]):
                        self.plan.blockers.append(f"{relative}: owned {'.'.join(path)} source changed, including comments; preserve it and reconcile the removal diff.")
                    if item.get("array"):
                        if isinstance(actual, list) and wanted in actual:
                            doc = doc.edit(path + (actual.index(wanted),))
                        elif actual:
                            self.plan.blockers.append(f"{relative}: owned {'.'.join(path)} hook changed; preserve it and reconcile before removal.")
                    elif type(actual) is type(wanted) and actual == wanted:
                        if item.get("container"):
                            # Setup introduces no comments inside empty stage
                            # arrays. Keep an empty container now annotated by
                            # the user instead of deleting its comments.
                            fragment = doc.fragment(path)
                            if "//" in fragment or "/*" in fragment:
                                continue
                        doc = doc.edit(path)
                    elif item.get("container") and isinstance(actual, list):
                        # A later integration now uses this stage too.
                        continue
                    else:
                        self.plan.blockers.append(f"{relative}: owned {'.'.join(path)} changed; preserve it and reconcile before removal.")
                for path in sorted((tuple(p) for p in record.get("parents", [])), key=len, reverse=True):
                    if lookup(doc.data, path) == {}:
                        doc = doc.edit(path)
                after = doc.text.encode()
            if before != after:
                self.plan.changes.append(Change(relative, before, after))
        self.ledger["hosts"].pop(host, None)

    def finish(self):
        if self.ledger != self.original:
            before = self.plan.snapshots[LEDGER]
            after = (json.dumps(self.ledger, indent=2, ensure_ascii=False) + "\n").encode() if self.ledger["hosts"] else None
            self.plan.changes.append(Change(LEDGER, before, after))
        return self.plan


def plan_configuration(root: Path, hosts, *, remove=False, exe=None):
    root = root.resolve()
    hosts = tuple(dict.fromkeys(h for h in hosts if h in HOSTS))
    try:
        builder = _Builder(root, hosts, remove)
        for host in hosts:
            if remove:
                builder.remove_host(host)
            elif host == "codex":
                from ctx.installer import _ctx_executable, _render_codex_file
                executable = exe or _ctx_executable()
                initial = _render_codex_file("config.toml", executable)
                from ctx.config_edit import TOMLSource
                cfg = TOMLSource(initial).data
                requirements = [(("features", "hooks"), True, False)]
                requirements += [(("mcp_servers", "ctx-harness", k), v, False) for k, v in cfg["mcp_servers"]["ctx-harness"].items()]
                existing = builder.read(".codex/config.toml")
                if existing:
                    actual = Document(existing.decode(), "toml").data
                    if lookup(actual, ("mcp_servers", "ctx-harness", "enabled")) is False:
                        requirements.append((("mcp_servers", "ctx-harness", "enabled"), True, False))
                    if lookup(actual, ("mcp_servers", "ctx-harness", "url")) is not MISSING:
                        requirements.append((("mcp_servers", "ctx-harness", "url"), MISSING, False))
                builder.document(host, ".codex/config.toml", "toml", requirements, initial=initial)
                hooks = json.loads(_render_codex_file("hooks.json", executable))["hooks"]
                existing_hooks = builder.read(".codex/hooks.json")
                if existing_hooks is not None:
                    try:
                        json.loads(existing_hooks)
                    except ValueError as exc:
                        raise ConfigError(f".codex/hooks.json: invalid JSON: {exc}") from exc
                builder.document(host, ".codex/hooks.json", "json", [(("hooks", stage), group, True) for stage, groups in hooks.items() for group in groups])
                builder.raw_file(host, "AGENTS.md", _render_codex_file("AGENTS.md", executable), block=True)
            else:
                from ctx.mcp_hosts import configuration
                from ctx.native_hooks import render, PATHS
                relative = opencode_path(root)
                # Read absence of the alternate configuration as part of CAS.
                builder.read("opencode.jsonc" if relative == "opencode.json" else "opencode.json")
                server = configuration(host, root, exe)["mcp"]["ctx-harness"]
                requirements = [(("mcp", "ctx-harness", k), v, False) for k, v in server.items()]
                existing = builder.read(relative)
                if existing:
                    data = Document(existing.decode(), "json").data
                    disabled = data.get("disabledServers", [])
                    if not isinstance(disabled, list):
                        raise ConfigError(f"{relative}: disabledServers must be an array")
                    if "ctx-harness" in disabled:
                        requirements.append((("disabledServers",), [name for name in disabled if name != "ctx-harness"], False))
                builder.document(host, relative, "json", requirements)
                builder.raw_file(host, PATHS[host], render(host, exe))
        return builder.finish()
    except (ValueError, OSError, TypeError, KeyError, IndexError, AttributeError, RecursionError) as exc:
        result = builder.plan if "builder" in locals() else ConfigPlan(root, hosts, remove)
        relative = getattr(builder, "current_path", "") if "builder" in locals() else ""
        detail = str(exc)
        result.blockers.append(f"{relative}: {detail}" if relative and relative not in detail else detail)
        return result
