"""Well-known credential files: the one list the guard and retrieval share.

Each tool's documented place for a login, a registry or API token, or a
cluster key: netrc for curl/git/ftp, npm, PyPI's twine, git's credential
store, docker login, kubectl, the GitHub CLI, cargo, RubyGems, Terraform's
CLI config, and Terraform variable files (``*.tfvars``: the conventional
home of a provider password or key; GitHub's Terraform ``.gitignore``
excludes them as "likely to contain sensitive data").

A name matches in any letter case, at any depth, and with a backup suffix
(``.npmrc.bak``, ``.netrc~``, ``.npmrc.old``). A template copy
(``.npmrc.example``, ``terraform.tfvars.sample``) is not a credential file:
see :data:`TEMPLATE_SUFFIXES`.

The hook (``ctx.hook._SECRET_PATH_RE``) embeds :data:`CREDENTIAL_PATTERN`;
``Workspace.is_ignored`` and the ripgrep engine use :func:`is_credential_file`,
so ``ctx get``/``ctx search`` refuse exactly what the
guard asks about. Stdlib ``re`` only: this module is on the hook's hot path.
"""

from __future__ import annotations

import re

#: Exact relative names (POSIX), matched at any depth and in any case.
CREDENTIAL_NAMES = (
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".terraformrc",
    "terraform.rc", ".docker/config.json", ".kube/config", ".config/gh/hosts.yml",
    ".config/gh/hosts.yaml", ".cargo/credentials", ".cargo/credentials.toml",
    ".gem/credentials",
)
#: Suffixes that make a copy a template, not a credential file.
#: (Not `dist` or `defaults`: a `.npmrc.dist` or `.netrc.defaults` is where a
#: real default login would sit.)
TEMPLATE_SUFFIXES = ("example", "sample", "template", "tmpl")

_TEMPLATE = "|".join(TEMPLATE_SUFFIXES)
_NAMES = "|".join(re.escape(n) for n in CREDENTIAL_NAMES)
#: A backup or copy suffix that is not a template one.
_BACKUP = rf"(?:\.(?!(?:{_TEMPLATE})$)[^/]*|~)?"

#: The credential-file part of the guard's secret regex (case-insensitive).
CREDENTIAL_PATTERN = (
    rf"(?i:(?:^|/)(?:{_NAMES}){_BACKUP}$)"
    rf"|(?i:\.tfvars(?:\.json)?{_BACKUP}$)"
)
_CREDENTIAL_RE: re.Pattern | None = None  # compiled on first use (hot path)

#: Bare names (no slash, no dot) the shell guard must still look at.
BARE_NAMES = ("_netrc",)


def is_credential_file(rel_path: str) -> bool:
    """Is this (relative or absolute) path a credential file by name?"""
    global _CREDENTIAL_RE
    if _CREDENTIAL_RE is None:
        _CREDENTIAL_RE = re.compile(CREDENTIAL_PATTERN)
    return bool(_CREDENTIAL_RE.search(str(rel_path).replace("\\", "/")))
