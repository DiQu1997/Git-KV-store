"""Concurrency: CAS conflict handling, stale-cache recovery, cross-process
writes, rotation racing against writes.

Workers are top-level functions so multiprocessing can pickle them.
"""

import multiprocessing
import os
import sys

import pytest

from Git_KV import GitKVStore


# Worker entrypoints (must be importable / picklable) -----------------------

def _worker_write_keys(repo_path, prefix, items, rotation_threshold):
    """Worker process: writes a list of (key, value) pairs on `prefix`."""
    # Ensure the worker can import Git_KV when launched fresh.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from Git_KV import GitKVStore  # re-import inside the worker
    s = GitKVStore(repo_path, rotation_threshold=rotation_threshold)
    t = s.table(prefix)
    for key, value in items:
        t.set(key, value)


def _spawn(fn, args, *, timeout=60):
    """Spawn a process running fn(*args) and return its exit code."""
    p = multiprocessing.Process(target=fn, args=args)
    p.start()
    p.join(timeout=timeout)
    if p.is_alive():
        p.terminate()
        p.join()
        raise AssertionError(f"worker timed out after {timeout}s")
    return p.exitcode


# Tests ---------------------------------------------------------------------

class TestCasConflictRetry:
    """Two stores on the same remote racing on the same key. We never lose
    writes — the loser of each round simply retries on the new tip."""

    def test_sequential_two_clones_no_conflict(self, two_stores):
        """Baseline: when writes don't interleave, both succeed cleanly."""
        a, b = two_stores
        a.create_table("pm")
        a.table("pm").set("foo", "from-a")
        b.table("pm").set("bar", "from-b")
        # Read from a fresh clone perspective.
        assert a.table("pm").get("foo") == "from-a"
        assert a.table("pm").get("bar") == "from-b"
        assert b.table("pm").get("foo") == "from-a"
        assert b.table("pm").get("bar") == "from-b"

    def test_in_process_alternating_writes(self, two_stores):
        """Alternate between two clients on the same key. Each write must
        succeed (even though the cached active branch's tip is stale)."""
        a, b = two_stores
        a.create_table("pm")
        for i in range(10):
            a.table("pm").set("k", f"A-{i}")
            b.table("pm").set("k", f"B-{i}")
        # Last write wins.
        assert b.table("pm").get("k") == "B-9"

    @pytest.mark.slow
    def test_concurrent_processes_no_data_loss(self, make_clone):
        """Two processes racing on overlapping keys: every key must end up
        with one writer's value (no missing keys, no torn writes)."""
        clone_a = str(make_clone("client_a"))
        clone_b = str(make_clone("client_b"))

        # Bootstrap the table from outside the workers so there's no race
        # in create_table itself.
        bootstrap = GitKVStore(clone_a)
        bootstrap.create_table("pm")

        n = 30
        items_a = [(f"k{i}", f"A-{i}") for i in range(n)]
        items_b = [(f"k{i}", f"B-{i}") for i in range(n)]

        p1 = multiprocessing.Process(
            target=_worker_write_keys, args=(clone_a, "pm", items_a, 1000)
        )
        p2 = multiprocessing.Process(
            target=_worker_write_keys, args=(clone_b, "pm", items_b, 1000)
        )
        p1.start(); p2.start()
        p1.join(timeout=120); p2.join(timeout=120)
        assert p1.exitcode == 0, "worker A failed"
        assert p2.exitcode == 0, "worker B failed"

        reader = GitKVStore(str(make_clone("reader")))
        missing = []
        for i in range(n):
            val = reader.table("pm").get(f"k{i}")
            if val is None or not (val.startswith("A-") or val.startswith("B-")):
                missing.append((f"k{i}", val))
        assert missing == [], f"missing/corrupt keys: {missing}"


class TestStaleCacheRecovery:
    """After rotation by one client, another client's cached active branch
    becomes a tombstone. The next operation must detect this and re-walk."""

    def test_writer_with_stale_cache_follows_tombstone_to_new_active(self, two_stores):
        a, b = two_stores
        a.create_table("pm")
        # Warm up A's cache.
        a.table("pm").set("k", "from-a")
        cached_active_a, _ = a.table("pm")._find_active()
        assert cached_active_a in a._active_cache["pm"]

        # B rotates → A's cached active is now a tombstone.
        new_active = b.table("pm").rotate()
        assert new_active != cached_active_a

        # A writes again — must transparently follow the tombstone to the
        # new active branch.
        a.table("pm").set("k", "from-a-after-rotation")
        active_after, _ = a.table("pm")._find_active()
        assert active_after == new_active
        assert b.table("pm").get("k") == "from-a-after-rotation"

    def test_reader_with_stale_cache_finds_new_active(self, two_stores):
        a, b = two_stores
        a.create_table("pm")
        a.table("pm").set("k", "v1")
        # Both have visited the original active.
        assert b.table("pm").get("k") == "v1"

        # A rotates; B reads — B should pick up the new active via the
        # tombstone walk starting from its (now-stale) cached active.
        a.table("pm").rotate()
        a.table("pm").set("k", "v2")
        assert b.table("pm").get("k") == "v2"


class TestRotationConcurrency:
    def test_write_succeeds_after_concurrent_rotation(self, two_stores):
        """A writer with a stale active branch (the branch was rotated out
        from under it) must retry on the new active branch."""
        a, b = two_stores
        a.create_table("pm")
        a.table("pm").set("seed", "value")
        # Force A to populate its cache.
        a.table("pm")._find_active()
        # B rotates.
        b.table("pm").rotate()
        # A writes — its cached active is now a tombstone; the CAS push
        # will reject and A re-walks the chain.
        a.table("pm").set("after", "rotated")
        assert b.table("pm").get("after") == "rotated"
        assert b.table("pm").get("seed") == "value"
