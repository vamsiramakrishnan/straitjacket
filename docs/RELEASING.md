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

## Release gate

From a clean checkout of the intended release commit:

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/ -q
python scripts/check_docs_links.py
python scripts/check_docs_facts.py
python -m build
python scripts/check_distribution.py dist/*.whl dist/*.tar.gz
python -m twine check dist/*
```

The distribution check verifies that the source archive contains every input
needed to rebuild the host assets. It then installs the wheel in a clean
temporary virtual environment, exercises `ctx --version`, renders every host
configuration, and probes the packaged Antigravity shim.

Confirm that `CHANGELOG.md` describes the version and that the tag exactly
matches `ctx --version` (`v0.32.1` for version `0.32.1`). Publish a GitHub
release for that tag. The release event checks out the tag, rebuilds and
smoke-tests both artifacts, checks the tag/version match, and only then asks
PyPI to mint a short-lived publishing credential.

PyPI releases are immutable. Never upload from a dirty checkout or reuse a
version after an artifact has been accepted.
