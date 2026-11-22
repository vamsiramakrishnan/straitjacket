"""Check a proposed release tag against the version Hatch will package."""

import argparse
from pathlib import Path

from check_docs_facts import product_version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="proposed Git tag, including the leading v")
    tag = parser.parse_args().tag
    version = product_version(Path(__file__).resolve().parents[1])
    expected = f"v{version}"
    if tag != expected:
        raise SystemExit(
            f"Release tag {tag!r} does not match package version {version}. "
            f"Expected tag {expected!r} for this checkout."
        )
    print(f"Release tag {tag} matches package version {version}.")
