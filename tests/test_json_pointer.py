"""One RFC 6901 JSON-pointer evaluator (R10).

There were two implementations and they disagreed on ``"/"``:

* ``ctx.query._json_pointer`` split on ``/`` and dropped the first (empty)
  element, so ``"/"`` yielded the segment ``""`` and looked up the member
  with the *empty-string key* — which is what RFC 6901 §5 says.
* ``ctx._retrieval.get`` short-circuited ``pointer not in ("", "/")``, so
  ``"/"`` returned the *whole document*, and it used ``lstrip("/")`` which
  additionally collapsed ``"//a"`` (empty key, then ``a``) to ``"/a"``.

RFC 6901 is unambiguous: ``""`` is the whole document, ``"/"`` is the
member whose key is the empty string. query's behaviour is the correct
one; both now route through ``ctx.textutil.json_pointer``.

The document below is the RFC's own example (§5).
"""

import json

import pytest

from ctx.textutil import JsonPointerError, json_pointer

# RFC 6901 §5, verbatim.
RFC_DOC = json.loads(
    r"""
    {
      "foo": ["bar", "baz"],
      "": 0,
      "a/b": 1,
      "c%d": 2,
      "e^f": 3,
      "g|h": 4,
      "i\\j": 5,
      "k\"l": 6,
      " ": 7,
      "m~n": 8
    }
    """
)


@pytest.mark.parametrize(
    "pointer,expected",
    [
        ("", RFC_DOC),          # whole document
        ("/foo", ["bar", "baz"]),
        ("/foo/0", "bar"),
        ("/", 0),               # the EMPTY-STRING key, not the document
        ("/a~1b", 1),           # ~1 decodes to "/"
        ("/c%d", 2),
        ("/e^f", 3),
        ("/g|h", 4),
        ("/i\\j", 5),
        ('/k"l', 6),
        ("/ ", 7),
        ("/m~0n", 8),           # ~0 decodes to "~"
    ],
)
def test_rfc6901_examples(pointer, expected):
    assert json_pointer(RFC_DOC, pointer) == expected


def test_slash_is_the_empty_key_not_the_whole_document():
    """The exact divergence. get.py returned the document for "/"."""
    doc = {"": "empty-key", "real": 1}
    assert json_pointer(doc, "/") == "empty-key"
    assert json_pointer(doc, "") == doc


def test_double_slash_is_two_empty_keys():
    """`lstrip("/")` collapsed leading slashes; only the first is the root."""
    assert json_pointer({"": {"": "deep"}}, "//") == "deep"
    assert json_pointer({"": {"a": 1}}, "//a") == 1


def test_escape_order_tilde_one_then_tilde_zero():
    """RFC 6901 §4: ~1 first, then ~0 — so `~01` decodes to `~1`, not `/`."""
    assert json_pointer({"~1": "tilde-one"}, "/~01") == "tilde-one"


def test_array_indices_and_the_dash():
    doc = {"a": [10, 20, 30]}
    assert json_pointer(doc, "/a/0") == 10
    assert json_pointer(doc, "/a/2") == 30
    # "-" names the nonexistent element after the last: an evaluation error.
    with pytest.raises(JsonPointerError):
        json_pointer(doc, "/a/-")
    with pytest.raises(JsonPointerError):
        json_pointer(doc, "/a/3")
    # Leading zeros / signs are not valid array indices (RFC 6901 §4 ABNF).
    with pytest.raises(JsonPointerError):
        json_pointer(doc, "/a/01")
    with pytest.raises(JsonPointerError):
        json_pointer(doc, "/a/-1")


def test_missing_key_and_scalar_descent_are_errors():
    with pytest.raises(JsonPointerError):
        json_pointer({"a": 1}, "/nope")
    with pytest.raises(JsonPointerError):
        json_pointer({"a": 1}, "/a/b")


def test_pointer_must_start_with_a_slash():
    with pytest.raises(JsonPointerError):
        json_pointer({"a": 1}, "a")


# ---------------------------------------------------------------- consumers
def test_get_resolves_slash_to_the_empty_key(state_home, workspace_dir):
    """`ctx get --json-pointer /` used to return the entire document."""
    from conftest import make_store, make_ws

    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(json.dumps({"": "empty-key", "other": 1}).encode())
    out = get(store, ws, f"blob:{blob}", Selector(json_pointer="/"))
    assert '"empty-key"' in out
    assert "other" not in out


def test_query_and_get_agree_on_every_rfc_pointer(state_home, workspace_dir):
    """The two implementations must now be one."""
    from ctx.query import _json_pointer

    for ptr in ("", "/foo", "/foo/0", "/", "/a~1b", "/m~0n", "/ "):
        assert _json_pointer(RFC_DOC, ptr) == json_pointer(RFC_DOC, ptr)
