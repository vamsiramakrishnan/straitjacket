"""Acceptance: a command that never exits must not be steered into a block.

The guard rewrites noisy commands into `ctx run -- ...`. For a stream-following
command that capture never returns, so the model was being told to run
something that hangs until the 600s timeout — worse than not steering at all.
Those go to `ctx run --bg`, which returns a job handle immediately.

The precision half matters as much: `-f` means *file* in `grep -f`/`make -f`/
`docker build -f` and *force* in `rm -f`. Backgrounding a build the model needed
to read inline is its own bug, so the short flag only counts for programs where
it really means follow.
"""

from __future__ import annotations

import pytest

from ctx.hook import _follows_forever


@pytest.mark.parametrize("cmd", [
    "journalctl -f",
    "journalctl --follow",
    "docker logs -f web",
    "docker logs --follow web",
    "podman logs -f c1",
    "kubectl logs -f pod",
    "tail -f app.log",
    "tail -F app.log --follow",
])
def test_stream_followers_are_detected(cmd):
    assert _follows_forever(cmd.split()) is True


@pytest.mark.parametrize("cmd", [
    "grep -f patterns.txt big.log",   # -f is a patterns FILE
    "make -f Makefile.ci",            # -f is a makefile
    "docker build -f Dockerfile .",   # -f is a dockerfile
    "rm -f stale",                    # -f is force
    "cp -f a b",
    "tar -xf archive.tar",
    "pytest -q",
    "git log --stat",
    "docker logs web",                # same program, no follow flag
])
def test_overloaded_f_is_not_mistaken_for_follow(cmd):
    assert _follows_forever(cmd.split()) is False


def test_rewrite_backgrounds_a_follower_and_says_why(workspace_dir):
    from ctx.hook import _deny_cmd

    policy = {"steering": "rewrite"}
    d = _deny_cmd("docker logs -f web".split(), policy)
    rw = d.get("_rewrite") or {}
    assert rw.get("command") == "ctx run --bg -- docker logs -f web"
    assert "never exits" in rw.get("reason", "")
    assert "ctx job" in rw.get("reason", "")  # tells the model how to collect it


def test_rewrite_leaves_ordinary_commands_in_the_foreground(workspace_dir):
    from ctx.hook import _deny_cmd

    d = _deny_cmd("pytest -q".split(), {"steering": "rewrite"})
    assert (d.get("_rewrite") or {}).get("command") == "ctx run -- pytest -q"
