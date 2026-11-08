"""Lossless, stdlib-only source edits for setup (never imported by hooks).

Parsers validate the whole document. Edits splice token spans, then reparse and
compare the complete semantic result. Comments and unrelated bytes are never
serialized. Ambiguous/unsupported syntax fails closed.
"""
from __future__ import annotations

import copy
import json
import math
import re
import tomllib
from dataclasses import dataclass, field


class ConfigError(ValueError):
    pass


MISSING = object()


def lookup(value, path):
    for key in path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
            value = value[key]
        else:
            return MISSING
    return value


def _string_end(text, start, *, toml=False):
    quote = text[start]
    delimiter = quote * 3 if toml and text.startswith(quote * 3, start) else quote
    i = start + len(delimiter)
    while i < len(text):
        if text[i] == "\\" and quote == '"':
            i += 2
        elif text.startswith(delimiter, i):
            i += len(delimiter)
            # TOML permits one or two extra quotes at a multiline boundary.
            if len(delimiter) == 3:
                while i < len(text) and text[i] == quote:
                    i += 1
            return i
        else:
            i += 1
    raise ConfigError(f"unterminated string at offset {start}")


@dataclass
class Node:
    start: int
    end: int
    children: dict = field(default_factory=dict)
    # Member/item ranges omit surrounding comments; comma handling is separate.
    members: dict = field(default_factory=dict)


class JSONSource:
    """JSONC: JSON plus comments and trailing commas; duplicate keys refused."""
    def __init__(self, text):
        self.text = text
        self.i = 0
        self.data, self.node = self._value()
        self._space()
        if self.i != len(text):
            raise ConfigError(f"unexpected token at offset {self.i}")

    def _space(self):
        while self.i < len(self.text):
            if self.text[self.i] in " \t\r\n":
                self.i += 1
            elif self.text.startswith("//", self.i):
                end = self.text.find("\n", self.i)
                self.i = len(self.text) if end < 0 else end + 1
            elif self.text.startswith("/*", self.i):
                end = self.text.find("*/", self.i + 2)
                if end < 0:
                    raise ConfigError(f"unterminated comment at offset {self.i}")
                self.i = end + 2
            else:
                break

    def _value(self):
        self._space()
        start = self.i
        if self.i >= len(self.text):
            raise ConfigError(f"expected value at offset {self.i}")
        char = self.text[self.i]
        if char in "{[":
            mapping = char == "{"
            close = "}" if mapping else "]"
            value = {} if mapping else []
            node = Node(start, 0)
            self.i += 1
            self._space()
            while self.i < len(self.text) and self.text[self.i] != close:
                member_start = self.i
                if mapping:
                    key, _ = self._value()
                    if not isinstance(key, str):
                        raise ConfigError(f"object key is not a string at offset {member_start}")
                    if key in value:
                        raise ConfigError(f"duplicate JSON key {key!r} at offset {member_start}")
                    self._space()
                    if self.i >= len(self.text) or self.text[self.i] != ":":
                        raise ConfigError(f"expected ':' at offset {self.i}")
                    self.i += 1
                else:
                    key = len(value)
                child, span = self._value()
                if mapping:
                    value[key] = child
                else:
                    value.append(child)
                node.children[key] = span
                node.members[key] = (member_start, self.i)
                self._space()
                if self.i < len(self.text) and self.text[self.i] == ",":
                    self.i += 1
                    self._space()
                elif self.i < len(self.text) and self.text[self.i] != close:
                    raise ConfigError(f"expected ',' at offset {self.i}")
            if self.i >= len(self.text):
                raise ConfigError(f"expected {close!r} at offset {self.i}")
            self.i += 1
            node.end = self.i
            return value, node
        if char == '"':
            self.i = _string_end(self.text, self.i)
            try:
                return json.loads(self.text[start:self.i]), Node(start, self.i)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        match = re.match(r"(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)", self.text[start:])
        if not match:
            raise ConfigError(f"invalid JSON value at offset {start}")
        self.i += match.end()
        return json.loads(match[0]), Node(start, self.i)

    def _at(self, path):
        node = self.node
        for key in path:
            node = node.children[key]
        return node

    def set(self, path, value):
        current = lookup(self.data, path)
        if current is not MISSING:
            node = self._at(path)
            return self.text[:node.start] + json.dumps(value, ensure_ascii=False) + self.text[node.end:]
        parent = lookup(self.data, path[:-1])
        if not isinstance(parent, (dict, list)):
            # Materialize only the first missing ancestor.
            for n in range(1, len(path)):
                if lookup(self.data, path[:n]) is MISSING:
                    child = value
                    for key in reversed(path[n:]):
                        child = {key: child}
                    return self.set(path[:n], child)
            raise ConfigError(f"non-object parent at {path[:-1]}")
        node = self._at(path[:-1])
        token = json.dumps(value, ensure_ascii=False)
        if isinstance(parent, dict):
            token = json.dumps(path[-1]) + ": " + token
        # Insert beside the closing delimiter, leaving all comments in place.
        at = node.end - 1
        prefix = ""
        if node.children:
            last = list(node.children)[-1]
            end = node.children[last].end
            between = self.text[end:at]
            # The lexer, rather than a regex, distinguishes commas in comments.
            probe = JSONSource.__new__(JSONSource)
            probe.text, probe.i = self.text, end
            probe._space()
            if probe.i >= at or self.text[probe.i] != ",":
                prefix = ","
            # A comma must precede a trailing // comment.
            text = self.text[:end] + prefix + self.text[end:]
            at += len(prefix)
        else:
            text = self.text
        newline = "\r\n" if "\r\n" in text else "\n"
        token = newline + "  " + token + newline
        return text[:at] + token + text[at:]

    def delete(self, path):
        node = self._at(path[:-1])
        start, end = node.members[path[-1]]
        probe = JSONSource.__new__(JSONSource)
        probe.text, probe.i = self.text, end
        probe._space()
        if probe.i < node.end - 1 and self.text[probe.i] == ",":
            # Remove the token and comma independently, preserving comments.
            return self.text[:start] + self.text[end:probe.i] + self.text[probe.i + 1:]
        keys = list(node.children)
        index = keys.index(path[-1])
        if index:
            prior = node.children[keys[index - 1]].end
            probe.i = prior
            probe._space()
            comma = probe.i
            return self.text[:comma] + self.text[comma + 1:start] + self.text[end:]
        return self.text[:start] + self.text[end:]


def _toml_key(text):
    doc = tomllib.loads(text.strip() + " = 0")
    keys = []
    while isinstance(doc, dict):
        key, doc = next(iter(doc.items()))
        keys.append(key)
    return tuple(keys)


def _toml_value_end(text, start, delimiters="\n#"):
    i, stack = start, []
    while i < len(text):
        char = text[i]
        if char in "\"'":
            i = _string_end(text, i, toml=True)
            continue
        if not stack and char in delimiters:
            break
        if char == "#":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack:
                break
            stack.pop()
        i += 1
    return i


class TOMLSource:
    def __init__(self, text):
        self.text = text
        try:
            self.data = tomllib.loads(text)
            self.values, self.lines, self.tables = {}, {}, {(): 0}
            self.headers = {}
            scope, i = (), 0
            while i < len(text):
                start = i
                while i < len(text) and text[i] in " \t\r\n":
                    i += 1
                if i == len(text):
                    break
                if text[i] == "#":
                    end = text.find("\n", i)
                    i = len(text) if end < 0 else end + 1
                    continue
                statement = i
                if text[i] == "[":
                    end = _toml_value_end(text, i)
                    header = text[i:end].strip()
                    array = header.startswith("[[")
                    scope = _toml_key(header[2:-2] if array else header[1:-1])
                    if array:
                        # Unrelated array tables remain opaque, never writable.
                        scope += (None,)
                    newline = text.find("\n", end)
                    self.tables[scope] = len(text) if newline < 0 else newline + 1
                    self.headers[scope] = (statement, end)
                else:
                    eq = i
                    while text[eq] != "=":
                        if text[eq] in "\"'":
                            eq = _string_end(text, eq, toml=True)
                        else:
                            eq += 1
                    path = scope + _toml_key(text[i:eq])
                    val = eq + 1
                    while text[val] in " \t":
                        val += 1
                    end = _toml_value_end(text, val)
                    trimmed = end
                    while trimmed > val and text[trimmed - 1].isspace():
                        trimmed -= 1
                    self.values[path] = (val, trimmed)
                    self.lines[path] = (statement, end)
                newline = text.find("\n", end)
                i = len(text) if newline < 0 else newline + 1
        except (ValueError, IndexError, StopIteration) as exc:
            raise ConfigError(f"invalid TOML: {exc}") from exc

    def _inline(self, start, end):
        text, i, nodes = self.text, start + 1, {}
        while i < end - 1:
            while text[i] in " \t\r\n,":
                i += 1
            if text[i] == "}":
                break
            key_start = i
            while text[i] != "=":
                i = _string_end(text, i, toml=True) if text[i] in "\"'" else i + 1
            key = _toml_key(text[key_start:i])
            i += 1
            while text[i].isspace():
                i += 1
            value_start = i
            i = _toml_value_end(text, i, ",}")
            value_end = i
            while text[value_end - 1].isspace():
                value_end -= 1
            nodes[key] = (key_start, value_start, value_end, i)
        return nodes

    @staticmethod
    def _render(value):
        if isinstance(value, dict):
            return "{ " + ", ".join(json.dumps(k) + " = " + TOMLSource._render(v) for k, v in value.items()) + " }"
        if isinstance(value, list):
            return "[" + ", ".join(TOMLSource._render(v) for v in value) + "]"
        return json.dumps(value, ensure_ascii=False)

    def _inline_edit(self, start, end, path, value=MISSING):
        nodes = self._inline(start, end)
        if path in nodes:
            key_start, a, b, stop = nodes[path]
            if value is not MISSING:
                return self.text[:a] + self._render(value) + self.text[b:]
            if self.text[stop] == ",":
                stop += 1
            else:
                keys = list(nodes)
                idx = keys.index(path)
                if idx:
                    key_start = nodes[keys[idx - 1]][3]
            return self.text[:key_start] + self.text[stop:]
        for key, (_, a, b, _) in nodes.items():
            if path[:len(key)] == key and self.text[a] == "{":
                return self._inline_edit(a, b, path[len(key):], value)
        if value is MISSING:
            raise ConfigError(f"cannot locate TOML member {path}")
        token = ".".join(json.dumps(k) for k in path) + " = " + self._render(value)
        at = end - 1
        return self.text[:at] + (", " if nodes else " ") + token + self.text[at:]

    def set(self, path, value):
        if path in self.values:
            a, b = self.values[path]
            return self.text[:a] + self._render(value) + self.text[b:]
        # Inline tables are closed in TOML; splice inside them, never append
        # a duplicate dotted/table declaration.
        for n in range(len(path) - 1, 0, -1):
            if path[:n] in self.values:
                a, b = self.values[path[:n]]
                if self.text[a] != "{":
                    raise ConfigError(f"non-table TOML parent at {path[:n]}")
                return self._inline_edit(a, b, path[n:], value)
        # The nearest explicit parent table fixes dotted-key scope. Insert
        # directly after its header, before any subsequent nested table.
        n = len(path) - 1
        while path[:n] not in self.tables:
            n -= 1
        at = self.tables[path[:n]]
        key = ".".join(json.dumps(k) for k in path[n:])
        newline = "\r\n" if "\r\n" in self.text else "\n"
        token = key + " = " + self._render(value) + newline
        if at and self.text[at - 1] != "\n":
            token = newline + token
        return self.text[:at] + token + self.text[at:]

    def delete(self, path):
        if path in self.values:
            a, b = self.lines[path]
            # Keep a trailing user comment, even when its setting is removed.
            if b < len(self.text) and self.text[b] == "\n":
                b += 1
            return self.text[:a] + self.text[b:]
        for n in range(len(path) - 1, 0, -1):
            if path[:n] in self.values:
                a, b = self.values[path[:n]]
                return self._inline_edit(a, b, path[n:], MISSING)
        # A table owns only its leaves, never following unrelated tables.
        current = lookup(self.data, path)
        if isinstance(current, dict):
            text = self.text
            for key in current:
                text = TOMLSource(text).delete(path + (key,))
            source = TOMLSource(text)
            if path in source.headers:
                a, b = source.headers[path]
                if b < len(text) and text[b] == "\n":
                    b += 1
                text = text[:a] + text[b:]
            # Deleting the last implicit child must retain its empty parent
            # until the caller explicitly removes that owned container.
            parsed = TOMLSource(text)
            for n in range(1, len(path)):
                prefix = path[:n]
                if lookup(parsed.data, prefix) is MISSING:
                    newline = "\r\n" if "\r\n" in text else "\n"
                    text += ("" if not text or text.endswith("\n") else newline)
                    text += "[" + ".".join(json.dumps(k) for k in prefix) + "]" + newline
                    parsed = TOMLSource(text)
            return text
        raise ConfigError(f"cannot locate TOML value at {path}")


class Document:
    def __init__(self, text, kind):
        self.text, self.kind = text, kind
        self.source = TOMLSource(text) if kind == "toml" else JSONSource(text)
        self.data = self.source.data
        if not isinstance(self.data, dict):
            raise ConfigError("configuration must be an object/table")

    def fragment(self, path):
        """Exact value source, including comments inside arrays/objects."""
        path = tuple(path)
        if self.kind != "toml":
            node = self.source._at(path)
            return self.text[node.start:node.end]
        for n in range(len(path), 0, -1):
            if path[:n] not in self.source.values:
                continue
            a, b = self.source.values[path[:n]]
            rest = path[n:]
            while rest:
                nodes = self.source._inline(a, b)
                key = next((k for k in nodes if rest[:len(k)] == k), None)
                if key is None:
                    raise ConfigError(f"cannot locate TOML source for {path}")
                _, a, b, _ = nodes[key]
                rest = rest[len(key):]
            return self.text[a:b]
        raise ConfigError(f"cannot locate source for {path}")

    def edit(self, path, value=MISSING, *, append=False):
        path = tuple(path)
        expected = copy.deepcopy(self.data)
        obj = expected
        for key in path[:-1]:
            if isinstance(obj, dict):
                obj = obj.setdefault(key, {})
            else:
                obj = obj[key]
        if append:
            obj[path[-1]].append(value)
            array = lookup(self.data, path)
            text = self.source.set(path + (len(array),), value)
        elif value is MISSING:
            del obj[path[-1]]
            text = self.source.delete(path)
        else:
            obj[path[-1]] = value
            text = self.source.set(path, value)
        result = Document(text, self.kind)
        if not _equal(result.data, expected):
            raise ConfigError(f"edit at {path} changed unrelated configuration")
        return result


def _equal(left, right):
    """TOML permits NaN; a reparse must still recognize an unchanged value."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and math.isnan(left):
        return math.isnan(right)
    return left == right
