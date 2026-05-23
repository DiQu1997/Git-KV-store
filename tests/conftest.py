"""Shared fixtures for the Git-KV test suite.

Every test gets its own bare remote and one or more clones, all under
pytest's tmp_path. We use real Git plumbing throughout — no mocks — so
behaviour under test matches what production code actually sees.
"""

import os
import subprocess

import pytest

from Git_KV import GitKVStore


def _run(cmd, cwd=None):
    """Run a shell command, fail loudly on non-zero exit."""
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def bare_remote(tmp_path):
    """Initialise a bare repo with partial-clone support enabled and return
    its path. The default branch is `main` to match the registry layout."""
    remote = tmp_path / "remote.git"
    _run(["git", "init", "--bare", "-b", "main", str(remote)])
    _run(["git", "-C", str(remote), "config", "uploadpack.allowFilter", "true"])
    return remote


def _make_clone(bare_remote, name, tmp_path):
    """Clone the bare remote into tmp_path/<name> and set safe defaults."""
    clone = tmp_path / name
    _run(["git", "clone", "--quiet", str(bare_remote), str(clone)])
    for key, value in [
        ("user.email", "test@example.com"),
        ("user.name", "test"),
        ("commit.gpgsign", "false"),
    ]:
        _run(["git", "-C", str(clone), "config", key, value])
    return clone


@pytest.fixture
def make_clone(bare_remote, tmp_path):
    """Factory that returns clone paths sharing the same bare remote.

    Use this when a test needs more than one client (concurrency, chain-walk
    cross-clone discovery, etc.). The factory caches by name so calling it
    twice with the same name returns the same clone.
    """
    cache = {}

    def _factory(name="client"):
        if name not in cache:
            cache[name] = _make_clone(bare_remote, name, tmp_path)
        return cache[name]

    return _factory


@pytest.fixture
def clone(make_clone):
    """Single default clone — convenience wrapper around make_clone."""
    return make_clone("client")


@pytest.fixture
def store(clone):
    """A GitKVStore on a fresh clone of an empty bare remote."""
    return GitKVStore(str(clone))


@pytest.fixture
def two_stores(make_clone):
    """Two independent GitKVStore clients sharing the same bare remote.

    Useful for testing chain-walk discovery, CAS conflicts, and cross-clone
    visibility.
    """
    a = GitKVStore(str(make_clone("client_a")))
    b = GitKVStore(str(make_clone("client_b")))
    return a, b


@pytest.fixture
def table_factory(store):
    """Create-and-return a named table on the default store."""
    def _make(prefix="pm"):
        store.create_table(prefix)
        return store.table(prefix)
    return _make
