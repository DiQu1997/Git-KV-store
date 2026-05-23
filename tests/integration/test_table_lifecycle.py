"""Table lifecycle: creating, listing, registry consistency across clones."""

import pytest

from Git_KV import (
    GitKVError,
    GitKVStore,
    TableAlreadyExistsError,
    TableNotFoundError,
    _parse_trailer,
)


class TestCreateTable:
    def test_creates_table_on_fresh_repo(self, store):
        store.create_table("pm")
        assert "pm" in store.list_tables()

    def test_creates_multiple_tables(self, store):
        store.create_table("pm")
        store.create_table("analytics")
        store.create_table("events")
        assert set(store.list_tables()) == {"pm", "analytics", "events"}

    def test_duplicate_raises(self, store):
        store.create_table("pm")
        with pytest.raises(TableAlreadyExistsError):
            store.create_table("pm")

    def test_invalid_prefix_raises(self, store):
        with pytest.raises(GitKVError):
            store.create_table("BAD-NAME")

    def test_reserved_prefix_raises(self, store):
        with pytest.raises(GitKVError, match="reserved"):
            store.create_table("main")

    def test_genesis_commit_carries_genesis_trailer(self, store):
        """The <prefix>_main commit must hold a Genesis-First-Log trailer
        that points to the initial log branch — that's the bootstrap entry
        for chain discovery."""
        store.create_table("pm")
        genesis_sha = store._git(
            "rev-parse", store._remote_ref("pm_main")
        ).strip()
        msg = store._commit_message(genesis_sha)
        first_log = _parse_trailer(msg, "Genesis-First-Log")
        assert first_log is not None
        assert first_log.startswith("refs/heads/pm_log_")


class TestListTables:
    def test_empty_repo_returns_empty_list(self, store):
        assert store.list_tables() == []

    def test_visible_across_clones(self, make_clone):
        a = GitKVStore(str(make_clone("client_a")))
        b = GitKVStore(str(make_clone("client_b")))
        a.create_table("pm")
        a.create_table("analytics")
        # Client B has no prior knowledge of the tables.
        assert set(b.list_tables()) == {"pm", "analytics"}


class TestTableNotFound:
    def test_get_on_unknown_table_raises(self, store):
        with pytest.raises(TableNotFoundError):
            store.table("ghost").get("any-key")

    def test_set_on_unknown_table_raises(self, store):
        with pytest.raises(TableNotFoundError):
            store.table("ghost").set("k", "v")
