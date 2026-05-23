"""CRUD operations: set/get/delete with flat and nested keys, idempotency,
cross-clone visibility."""

import pytest

from Git_KV import GitKVError, GitKVStore


@pytest.fixture
def table(table_factory):
    return table_factory("pm")


class TestBasicCrud:
    def test_get_missing_key_returns_none(self, table):
        assert table.get("nonexistent") is None

    def test_set_then_get_returns_value(self, table):
        table.set("foo", "hello")
        assert table.get("foo") == "hello"

    def test_set_overwrites_existing(self, table):
        table.set("foo", "v1")
        table.set("foo", "v2")
        assert table.get("foo") == "v2"

    def test_delete_removes_key(self, table):
        table.set("foo", "hello")
        table.delete("foo")
        assert table.get("foo") is None

    def test_delete_missing_key_is_noop(self, table):
        # Should not raise even though the key was never written.
        table.delete("never-existed")

    def test_set_none_raises(self, table):
        with pytest.raises(GitKVError):
            table.set("foo", None)

    def test_set_empty_string_is_allowed(self, table):
        table.set("foo", "")
        assert table.get("foo") == ""


class TestNestedKeys:
    def test_set_get_nested(self, table):
        table.set("a/b/c", "deep")
        assert table.get("a/b/c") == "deep"

    def test_siblings_dont_collide(self, table):
        table.set("a/b/c", "C")
        table.set("a/b/d", "D")
        assert table.get("a/b/c") == "C"
        assert table.get("a/b/d") == "D"

    def test_delete_preserves_siblings(self, table):
        table.set("a/b/c", "C")
        table.set("a/b/d", "D")
        table.delete("a/b/c")
        assert table.get("a/b/c") is None
        assert table.get("a/b/d") == "D"

    def test_delete_collapses_empty_subtree(self, table):
        """After removing the only child of `a/b`, `a/b/anything` should
        cleanly resolve to None — and a subsequent write under a different
        path should work (no leftover empty-tree corruption)."""
        table.set("a/b/c", "C")
        table.delete("a/b/c")
        assert table.get("a/b/c") is None
        # Confirm we can still write into the same namespace.
        table.set("a/x", "X")
        assert table.get("a/x") == "X"

    def test_deeply_nested(self, table):
        table.set("a/b/c/d/e/f/g", "deep")
        assert table.get("a/b/c/d/e/f/g") == "deep"


class TestIdempotency:
    def test_repeated_set_same_value_does_not_create_new_commit(self, table):
        """Setting the same value twice must short-circuit at the tree
        level — otherwise commits pile up and rotation triggers wastefully."""
        table.set("foo", "v1")
        active, tip_before = table._find_active()
        table.set("foo", "v1")  # identical
        active2, tip_after = table._find_active()
        assert active == active2
        assert tip_before == tip_after, "second identical set should be a no-op"

    def test_delete_of_missing_key_does_not_create_new_commit(self, table):
        table.set("foo", "v1")
        _, tip_before = table._find_active()
        table.delete("nonexistent")
        _, tip_after = table._find_active()
        assert tip_before == tip_after


class TestCrossCloneVisibility:
    def test_writes_visible_to_other_clone(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        b = GitKVStore(str(make_clone("client_b")))
        a.create_table("pm")
        a.table("pm").set("foo", "from-a")
        a.table("pm").set("nest/key", "deep-from-a")

        assert b.table("pm").get("foo") == "from-a"
        assert b.table("pm").get("nest/key") == "deep-from-a"

    def test_deletes_visible_to_other_clone(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        b = GitKVStore(str(make_clone("client_b")))
        a.create_table("pm")
        a.table("pm").set("foo", "hello")
        # B sees it
        assert b.table("pm").get("foo") == "hello"
        # A deletes
        a.table("pm").delete("foo")
        # B sees the deletion
        assert b.table("pm").get("foo") is None
