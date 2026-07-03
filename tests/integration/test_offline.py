"""Offline mode: local-only ops, auto-bootstrap from remote-tracking refs,
explicit pull/push/sync, and refuse-on-diverge semantics.

The "truly offline" tests break the clone's remote URL after cloning — any
op that touches the network then fails loudly, so a passing test proves the
op never left the machine.
"""

import subprocess

import pytest

from gitkv import GitKVError, GitKVStore, SyncDivergedError, TableNotFoundError


def _break_remote(clone):
    """Point origin at a nonexistent path so any network op fails."""
    subprocess.run(
        ["git", "-C", str(clone), "remote", "set-url", "origin",
         "/nonexistent/gitkv-blackhole.git"],
        check=True, capture_output=True,
    )


@pytest.fixture
def dark_store(make_clone):
    """Offline store whose remote is unreachable — proves ops are local."""
    clone = make_clone("dark_client")
    _break_remote(clone)
    return GitKVStore(str(clone), offline=True)


# ---------------------------------------------------------------------------
# Local-only operation (remote unreachable)
# ---------------------------------------------------------------------------

class TestOfflineOps:
    def test_create_table_and_crud(self, dark_store):
        dark_store.create_table("pm")
        t = dark_store.table("pm")
        t.set("foo", "hello")
        assert t.get("foo") == "hello"
        t.set("nest/deep/key", "v")
        assert t.get("nest/deep/key") == "v"
        t.delete("foo")
        assert t.get("foo") is None

    def test_list_tables(self, dark_store):
        assert dark_store.list_tables() == []
        dark_store.create_table("pm")
        dark_store.create_table("analytics")
        assert set(dark_store.list_tables()) == {"pm", "analytics"}

    def test_dict_api(self, dark_store):
        dark_store.create_table("pm")
        dark_store["pm"]["k"] = "v"
        assert dark_store["pm"]["k"] == "v"
        assert "k" in dark_store["pm"]
        del dark_store["pm"]["k"]
        assert "k" not in dark_store["pm"]

    def test_list_keys_and_items(self, dark_store):
        dark_store.create_table("pm")
        t = dark_store.table("pm")
        t.set("a/x", "1")
        t.set("a/y", "2")
        t.set("b", "3")
        assert t.list_keys() == ["a/x", "a/y", "b"]
        assert t.list_keys(prefix="a") == ["a/x", "a/y"]
        assert dict(t.list_items()) == {"a/x": "1", "a/y": "2", "b": "3"}

    def test_rotation_works_offline(self, make_clone):
        clone = make_clone("dark_rot")
        _break_remote(clone)
        s = GitKVStore(str(clone), offline=True, rotation_threshold=3)
        s.create_table("pm")
        t = s.table("pm")
        for i in range(10):
            t.set(f"k{i}", f"v{i}")
        for i in range(10):
            assert t.get(f"k{i}") == f"v{i}"

    def test_manual_rotate_offline(self, dark_store):
        dark_store.create_table("pm")
        t = dark_store.table("pm")
        t.set("k", "v")
        old_active, _ = t._find_active()
        new_branch = t.rotate()
        assert new_branch != old_active
        assert t.get("k") == "v"

    def test_unknown_table_raises(self, dark_store):
        with pytest.raises(TableNotFoundError, match="pull"):
            dark_store.table("ghost").get("k")

    def test_online_store_on_broken_remote_fails(self, make_clone):
        """Sanity contrast: the same broken remote kills online mode."""
        clone = make_clone("dark_sanity")
        _break_remote(clone)
        online = GitKVStore(str(clone), offline=False)
        with pytest.raises(GitKVError):
            online.create_table("pm")


# ---------------------------------------------------------------------------
# Bootstrap from remote-tracking refs
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_offline_reads_data_cloned_from_remote(self, bare_remote, make_clone, tmp_path):
        # A writes online.
        a = GitKVStore(str(make_clone("writer")))
        a.create_table("pm")
        a.table("pm").set("user/alice", "hello")

        # Fresh clone made AFTER the write → remote-tracking refs exist.
        import subprocess as sp
        late = tmp_path / "late_clone"
        sp.run(["git", "clone", "--quiet", str(bare_remote), str(late)],
               check=True, capture_output=True)
        _break_remote(late)

        # Offline store bootstraps local heads from remote-tracking refs.
        b = GitKVStore(str(late), offline=True)
        assert b.table("pm").get("user/alice") == "hello"
        assert b.list_tables() == ["pm"]

    def test_offline_create_of_remotely_existing_table_raises(
        self, bare_remote, make_clone, tmp_path
    ):
        a = GitKVStore(str(make_clone("writer")))
        a.create_table("pm")

        import subprocess as sp
        late = tmp_path / "late_clone2"
        sp.run(["git", "clone", "--quiet", str(bare_remote), str(late)],
               check=True, capture_output=True)
        _break_remote(late)

        from gitkv import TableAlreadyExistsError
        b = GitKVStore(str(late), offline=True)
        with pytest.raises(TableAlreadyExistsError):
            b.create_table("pm")


# ---------------------------------------------------------------------------
# pull / push / sync
# ---------------------------------------------------------------------------

class TestPullPush:
    def test_pull_brings_remote_writes_local(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        a.create_table("pm")
        a.table("pm").set("k", "from-a")

        b = GitKVStore(str(make_clone("client_b")), offline=True)
        result = b.pull()
        assert result["created"]  # branches materialized locally
        assert b.table("pm").get("k") == "from-a"

    def test_push_publishes_offline_writes(self, make_clone):
        b = GitKVStore(str(make_clone("client_b")), offline=True)
        b.create_table("pm")
        b.table("pm").set("k", "from-b-offline")
        result = b.push()
        assert result["pushed"]

        a = GitKVStore(str(make_clone("client_a")))
        assert a.table("pm").get("k") == "from-b-offline"

    def test_push_with_nothing_to_do(self, make_clone):
        b = GitKVStore(str(make_clone("client_b")), offline=True)
        assert b.push() == {"pushed": []}

    def test_pull_up_to_date(self, make_clone):
        b = GitKVStore(str(make_clone("client_b")), offline=True)
        result = b.pull()
        assert result["created"] == [] and result["updated"] == []

    def test_sync_round_trip(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        a.create_table("pm")
        a.table("pm").set("from_a", "1")

        b = GitKVStore(str(make_clone("client_b")), offline=True)
        b.pull()
        b.table("pm").set("from_b", "2")
        b.sync()

        assert a.table("pm").get("from_b") == "2"
        assert b.table("pm").get("from_a") == "1"

    def test_pull_after_remote_advance_fast_forwards(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        a.create_table("pm")
        a.table("pm").set("k", "v1")

        b = GitKVStore(str(make_clone("client_b")), offline=True)
        b.pull()
        assert b.table("pm").get("k") == "v1"

        # Remote advances (same branch), B is now behind.
        a.table("pm").set("k", "v2")
        result = b.pull()
        assert result["updated"]  # fast-forwarded
        assert b.table("pm").get("k") == "v2"

    def test_offline_writes_invisible_until_push(self, make_clone):
        b = GitKVStore(str(make_clone("client_b")), offline=True)
        b.create_table("pm")
        b.table("pm").set("k", "local-only")

        a = GitKVStore(str(make_clone("client_a")))
        assert a.list_tables() == []  # remote knows nothing yet

        b.push()
        assert a.list_tables() == ["pm"]
        assert a.table("pm").get("k") == "local-only"


# ---------------------------------------------------------------------------
# Divergence: refuse, never merge
# ---------------------------------------------------------------------------

@pytest.fixture
def diverged_pair(make_clone):
    """A (online) and B (offline) that have both written to the same log
    branch: B pulled, then A wrote online, then B wrote offline."""
    a = GitKVStore(str(make_clone("client_a")))
    a.create_table("pm")
    a.table("pm").set("seed", "s")

    b = GitKVStore(str(make_clone("client_b")), offline=True)
    b.pull()

    a.table("pm").set("k", "from-a")   # remote advances
    b.table("pm").set("k", "from-b")   # local advances on same branch
    return a, b


class TestDiverged:
    def test_pull_raises_and_preserves_local(self, diverged_pair):
        _, b = diverged_pair
        with pytest.raises(SyncDivergedError, match="diverged"):
            b.pull()
        # Local value untouched by the failed pull.
        assert b.table("pm").get("k") == "from-b"

    def test_push_raises_and_preserves_remote(self, diverged_pair):
        a, b = diverged_pair
        with pytest.raises(SyncDivergedError, match="diverged"):
            b.push()
        # Remote value untouched by the failed push.
        assert a.table("pm").get("k") == "from-a"

    def test_status_reports_divergence(self, diverged_pair):
        _, b = diverged_pair
        st = b.status()
        assert st["diverged"]  # at least the shared log branch


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_fresh_clone_in_sync(self, make_clone):
        b = GitKVStore(str(make_clone("client_b")), offline=True)
        st = b.status()
        assert st == {
            "ahead": {}, "behind": {},
            "diverged": [], "local_only": [], "remote_only": [],
        }

    def test_offline_writes_show_local_only(self, make_clone):
        b = GitKVStore(str(make_clone("client_b")), offline=True)
        b.create_table("pm")
        st = b.status()
        # Genesis + first log + main registry — all local-only.
        assert len(st["local_only"]) == 3
        assert not st["remote_only"] and not st["diverged"]

    def test_remote_writes_show_remote_only_then_behind(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        b = GitKVStore(str(make_clone("client_b")), offline=True)

        a.create_table("pm")
        st = b.status()
        assert len(st["remote_only"]) == 3

        b.pull()
        a.table("pm").set("k", "v")     # remote log branch advances
        st = b.status()
        assert len(st["behind"]) == 1
        assert list(st["behind"].values()) == [1]

    def test_ahead_counts(self, make_clone):
        b = GitKVStore(str(make_clone("client_b")), offline=True)
        b.create_table("pm")
        b.push()
        b.table("pm").set("k1", "v")
        b.table("pm").set("k2", "v")
        st = b.status()
        assert list(st["ahead"].values()) == [2]


# ---------------------------------------------------------------------------
# Mixed-mode interplay on one clone
# ---------------------------------------------------------------------------

class TestMixedModes:
    def test_offline_then_online_store_on_same_clone(self, make_clone):
        """Offline work, push, then an online store on the same clone sees
        everything and can keep writing."""
        clone = make_clone("mixed")
        off = GitKVStore(str(clone), offline=True)
        off.create_table("pm")
        off.table("pm").set("k", "offline-v")
        off.push()

        on = GitKVStore(str(clone), offline=False)
        assert on.table("pm").get("k") == "offline-v"
        on.table("pm").set("k", "online-v")
        assert on.table("pm").get("k") == "online-v"
