# Releasing `ctx-harness`

The distribution is `ctx-harness`; `straitjacket` is the product/repository
name and is already used by an unrelated distribution on PyPI.

## One-time PyPI setup

Configure a pending Trusted Publisher in PyPI before the first release:

- PyPI project name: `ctx-harness`
- GitHub owner: `vamsiramakrishnan`
- GitHub repository: `straitjacket`
- Workflow: `publish.yml`
- Environment: `pypi`

The workflow uses GitHub OIDC and deliberately has no token secret. Protect the
`pypi` GitHub environment with required reviewers if the repository has more
than one release operator.

For a project that does not exist on PyPI yet, create that pending publisher at
<https://pypi.org/manage/account/publishing/> **before** publishing the GitHub
release. An `invalid-publisher` exchange failure means GitHub supplied a valid
OIDC token but PyPI found no publisher with matching owner, repository,
workflow, and environment claims. Retrying unchanged cannot repair that
account-side configuration; correct the pending publisher, then re-run the
failed GitHub Actions job.

## Release gate

From a clean checkout of the intended release commit, set `RELEASE_TAG` to
the exact tag you plan to publish and run the gate:

```bash
python scripts/check_release_tag.py "${RELEASE_TAG:?set RELEASE_TAG to the proposed tag}"
python -m pip install -e '.[dev]' build twine tiktoken==0.11.0
python -m pytest tests/ -q
python scripts/fix_docs_svgs.py --check
python scripts/gen_addressable_evidence_visuals.py --check
python scripts/check_docs_links.py
CTX_DOCS_REQUIRE_FIELD_TOKEN_REPLAY=1 python scripts/check_docs_facts.py
(cd site && npm ci && npm run build)
python -m build
python scripts/check_distribution.py dist/*.whl dist/*.tar.gz
python -m twine check dist/*
```

The distribution check verifies that the source archive contains every input
needed to rebuild the host assets. It then installs the wheel in a clean
temporary virtual environment, exercises `ctx --version`, renders every host
configuration, and probes the packaged Antigravity shim.

Use Node 22 for the site build, matching Docs integrity. These commands include
every documentation gate; link checks alone do not validate portable SVGs,
generated assets, token receipts, or the built site.

Before tagging, require successful **CI** and **Docs integrity** runs on the
merged commit you intend to release. CI also covers Python 3.11–3.13, minimal
dependencies, native hook parity, and isolated distribution checks. A green
run on an earlier PR commit does not validate the release commit.

Move the intended `Unreleased` notes into a dated version entry and set
`src/ctx/__init__.py` to that version, following the minor-per-mechanism-wave
policy in [CONTRIBUTING.md](../CONTRIBUTING.md). Confirm that the tag exactly
matches `ctx --version` (`vX.Y.Z` for version `X.Y.Z`). Publish a GitHub
release for that tag. The release event checks out the tag and verifies its
name against the package version before building either artifact. It then
smoke-tests the wheel, checks both distributions, and requests a PyPI publishing
credential.

If a tag names the wrong version, first verify whether PyPI accepted any
files. A fresh tag on a verified commit is the usual recovery. Moving an
existing public tag changes which source readers fetch, so confirm that
decision with the release owner first.

For a corrected tag whose GitHub release already exists, start a fresh run
with `gh workflow run publish.yml --ref "$RELEASE_TAG"`. This path requires
a tag and a published GitHub release. The workflow builds the commit SHA
recorded by the new event so the checkout and publishing provenance agree.

PyPI releases are immutable. Never upload from a dirty checkout or reuse a
version after an artifact has been accepted.
