"""Library-swap regressions: orjson fast path semantics, flock'd ledger."""

import json
import os
import threading

from conftest import make_ws


def test_loads_fast_matches_stdlib_on_normal_json():
    from ctx.textutil import loads_fast

    doc = '{"a": [1, 2.5, "x"], "b": {"c": null, "d": true}}'
    assert loads_fast(doc) == json.loads(doc)


def test_loads_fast_accepts_stdlib_extensions():
    from ctx.textutil import loads_fast

    # orjson rejects NaN; the stdlib retry must keep acceptance identical.
    result = loads_fast('{"x": NaN}')
    assert result["x"] != result["x"]  # NaN


def test_ledger_concurrent_charges_not_lost(tmp_path):
    from ctx.hook import _ledger_charge

    n_threads, per_thread, chunk = 8, 25, 100
    threads = [
        threading.Thread(
            target=lambda: [_ledger_charge(str(tmp_path), "sess", chunk) for _ in range(per_thread)]
        )
        for _ in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = int((tmp_path / ".ctx-session-reads" / "sess.count").read_text())
    assert total == n_threads * per_thread * chunk  # no lost updates
