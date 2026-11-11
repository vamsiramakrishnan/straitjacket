"""`ctx coverage`: the governance table and what the coverage ledger observed."""

from __future__ import annotations


def cmd_coverage(ws, ns) -> int:
    import json

    from ctx import coverage

    data = coverage.report(ws.root, host=ns.coverage_host, session=ns.coverage_session)
    if ns.coverage_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(coverage.render(data), end="")
    return 0
