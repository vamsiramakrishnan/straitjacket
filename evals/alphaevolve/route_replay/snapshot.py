"""Refresh or check the privacy-safe route replay observation snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ctx.route_telemetry import export_route_observations


def build_snapshot(
    workspaces: list[Path], *, existing: Path | None = None
) -> dict:
    """Merge observations by run ID, with freshly exported rows winning.

    Disposable live workspaces are often removed after a campaign.  Preserving
    an existing frozen snapshot prevents a later one-workspace refresh from
    silently erasing those reviewed observations.
    """
    observations: dict[str, dict] = {}
    if existing is not None and existing.is_file():
        try:
            current = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        for observation in current.get("observations", []):
            if isinstance(observation, dict) and isinstance(
                observation.get("run_id"), str
            ):
                observations[observation["run_id"]] = observation
    for workspace in workspaces:
        exported = export_route_observations(workspace.resolve())
        for observation in exported["observations"]:
            observations[observation["run_id"]] = observation
    return {
        "schema": "ctx.route-replay-observations/v1",
        "observations": [observations[key] for key in sorted(observations)],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspaces", nargs="*", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="discard observations not present in the supplied workspaces",
    )
    args = parser.parse_args()
    workspaces = args.workspaces or [Path.cwd()]
    existing = None if args.replace or args.check else args.output
    snapshot = build_snapshot(workspaces, existing=existing)
    rendered = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    if args.check:
        current = json.loads(args.check.read_text(encoding="utf-8"))
        if current != json.loads(rendered):
            raise SystemExit(f"snapshot differs: {args.check}")
        print(f"snapshot matches: {args.check}")
    elif args.output:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()


__all__ = ["build_snapshot", "main"]
