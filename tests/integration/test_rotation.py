"""Log rotation: manual + automatic, tombstone metadata, chain walk, data
preservation across rotations, commit-number bookkeeping."""

import pytest

from Git_KV import GitKVStore, TOMBSTONE_VERSION, _parse_trailer


@pytest.fixture
def table(table_factory):
    return table_factory("pm")


class TestManualRotation:
    def test_rotate_returns_new_branch_name(self, table):
        active_before, _ = table._find_active()
        new_branch = table.rotate()
        assert new_branch != active_before
        assert new_branch.startswith("pm_log_")

    def test_active_branch_changes(self, table):
        active_before, _ = table._find_active()
        table.rotate()
        active_after, _ = table._find_active()
        assert active_after != active_before

    def test_tombstone_has_required_trailers(self, table):
        active_before, tip_before = table._find_active()
        table.rotate()
        # The closing branch's tip is now the tombstone.
        table.store._fetch_tip_only(active_before)
        tombstone_sha = table.store._rev_parse(
            table.store._remote_ref(active_before)
        )
        msg = table.store._commit_message(tombstone_sha)
        assert _parse_trailer(msg, "Tombstone-Version") == TOMBSTONE_VERSION
        next_ref = _parse_trailer(msg, "Tombstone-Next-Branch")
        assert next_ref is not None and next_ref.startswith("refs/heads/pm_log_")
        assert _parse_trailer(msg, "Tombstone-Next-Sha") is not None
        assert _parse_trailer(msg, "Tombstone-Snapshot-Tree") is not None
        assert _parse_trailer(msg, "Tombstone-Created-At") is not None


class TestDataPreservationAcrossRotation:
    def test_pre_rotation_data_survives(self, table):
        table.set("foo", "before")
        table.set("nest/a", "deep")
        table.rotate()
        assert table.get("foo") == "before"
        assert table.get("nest/a") == "deep"

    def test_writes_continue_on_new_branch(self, table):
        table.set("foo", "before")
        table.rotate()
        table.set("bar", "after")
        assert table.get("foo") == "before"
        assert table.get("bar") == "after"

    def test_survives_multiple_rotations(self, table):
        for i in range(5):
            table.set(f"k{i}", f"v{i}")
            table.rotate()
        # All keys must still be readable after 5 rotations.
        for i in range(5):
            assert table.get(f"k{i}") == f"v{i}"


class TestChainWalkCrossClone:
    def test_cold_client_discovers_new_active_via_tombstone(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        b = GitKVStore(str(make_clone("client_b")))
        a.create_table("pm")
        a.table("pm").set("k", "v")
        new_branch = a.table("pm").rotate()

        # B has never seen this table — empty active cache.
        assert "pm" not in b._active_cache
        active, _ = b.table("pm")._find_active()
        assert active == new_branch

    def test_cold_client_walks_multiple_tombstones(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        b = GitKVStore(str(make_clone("client_b")))
        a.create_table("pm")
        for i in range(4):
            a.table("pm").set(f"k{i}", f"v{i}")
            a.table("pm").rotate()
        expected_active, _ = a.table("pm")._find_active()

        # B walks the chain (4 tombstones deep) from cold.
        assert "pm" not in b._active_cache
        active, _ = b.table("pm")._find_active()
        assert active == expected_active
        # Data from before any rotation is still readable.
        for i in range(4):
            assert b.table("pm").get(f"k{i}") == f"v{i}"


class TestAutoRotation:
    def test_threshold_triggers_rotation(self, make_clone):
        s = GitKVStore(str(make_clone("client")), rotation_threshold=5)
        s.create_table("autorot")
        t = s.table("autorot")
        initial_active, _ = t._find_active()

        # Hit the threshold.
        for i in range(5):
            t.set(f"k{i}", f"v{i}")

        post_active, _ = t._find_active()
        assert post_active != initial_active

    def test_threshold_one_below_does_not_trigger(self, make_clone):
        """The opening commit of each segment has Commit-Number=1, so N
        writes leave the tip at N+1. With threshold=5, the largest N that
        keeps tip < threshold is N=3 (tip=4)."""
        s = GitKVStore(str(make_clone("client")), rotation_threshold=5)
        s.create_table("autorot")
        t = s.table("autorot")
        initial_active, _ = t._find_active()
        for i in range(3):
            t.set(f"k{i}", f"v{i}")
        assert (t._find_active())[0] == initial_active

    def test_threshold_boundary_triggers(self, make_clone):
        """The write that brings the tip's Commit-Number up to the
        threshold must trigger rotation."""
        s = GitKVStore(str(make_clone("client")), rotation_threshold=5)
        s.create_table("autorot")
        t = s.table("autorot")
        initial_active, _ = t._find_active()
        for i in range(4):  # 4th write → tip=5 → rotate
            t.set(f"k{i}", f"v{i}")
        assert (t._find_active())[0] != initial_active

    def test_data_survives_many_auto_rotations(self, make_clone):
        s = GitKVStore(str(make_clone("client")), rotation_threshold=3)
        r = GitKVStore(str(make_clone("reader")), rotation_threshold=3)
        s.create_table("autorot")
        for i in range(15):
            s.table("autorot").set(f"k{i}", f"v{i}")
        for i in range(15):
            assert r.table("autorot").get(f"k{i}") == f"v{i}"


class TestCommitNumber:
    def test_first_log_commit_has_number_1(self, store):
        store.create_table("pm")
        active, tip = store.table("pm")._find_active()
        assert store._commit_number(tip) == 1

    def test_each_write_increments_number(self, table):
        active, tip0 = table._find_active()
        n0 = table.store._commit_number(tip0)

        table.set("a", "1")
        _, tip1 = table._find_active()
        n1 = table.store._commit_number(tip1)

        table.set("b", "2")
        _, tip2 = table._find_active()
        n2 = table.store._commit_number(tip2)

        assert n1 == n0 + 1
        assert n2 == n1 + 1

    def test_resets_to_1_on_new_segment(self, table):
        for i in range(3):
            table.set(f"k{i}", f"v{i}")
        table.rotate()
        _, tip = table._find_active()
        assert table.store._commit_number(tip) == 1
