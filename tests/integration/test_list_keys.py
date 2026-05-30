"""list_keys / list_items / __iter__ on GitKVTable.

These cover:
- Empty tables and no-prefix listing
- Path-prefix semantics (segment-strict, trailing slash ignored,
  prefix that's itself a key returns [that key])
- Pagination via `after` + `limit` (alone and combined)
- list_items round-trips values, paginates the same way
- Iteration via __iter__
- Validation of prefix / after / limit
"""

import pytest

from gitkv import GitKVError, TableNotFoundError
from gitkv._store import _apply_pagination, _normalize_path_prefix


@pytest.fixture
def table(table_factory):
    return table_factory("pm")


@pytest.fixture
def populated_table(table):
    """A table with 9 keys spanning two top-level branches."""
    fixtures = [
        ("config/timeout", "30"),
        ("config/region", "us-east"),
        ("user/alice/email", "alice@example.com"),
        ("user/alice/role", "admin"),
        ("user/bob/email", "bob@example.com"),
        ("user/bob/role", "viewer"),
        ("user/charlie/email", "c@example.com"),
        ("user/charlie/role", "viewer"),
        ("notes", "top-level blob"),
    ]
    for k, v in fixtures:
        table.set(k, v)
    return table


# ---------------------------------------------------------------------------
# list_keys — basic shape
# ---------------------------------------------------------------------------

class TestListKeysEmpty:
    def test_empty_table_returns_empty_list(self, table):
        assert table.list_keys() == []

    def test_empty_table_with_prefix_returns_empty(self, table):
        assert table.list_keys(prefix="user/") == []


class TestListKeysAllKeys:
    def test_returns_every_key_sorted(self, populated_table):
        keys = populated_table.list_keys()
        assert keys == sorted(keys)
        expected = {
            "config/region", "config/timeout",
            "user/alice/email", "user/alice/role",
            "user/bob/email", "user/bob/role",
            "user/charlie/email", "user/charlie/role",
            "notes",
        }
        assert set(keys) == expected

    def test_empty_prefix_same_as_none(self, populated_table):
        assert populated_table.list_keys() == populated_table.list_keys(prefix="")


# ---------------------------------------------------------------------------
# list_keys — path-prefix semantics
# ---------------------------------------------------------------------------

class TestPathPrefix:
    def test_prefix_matches_segment(self, populated_table):
        keys = populated_table.list_keys(prefix="user")
        assert all(k.startswith("user/") for k in keys)
        assert "user/alice/email" in keys
        assert "config/timeout" not in keys

    def test_trailing_slash_ignored(self, populated_table):
        assert (
            populated_table.list_keys(prefix="user/")
            == populated_table.list_keys(prefix="user")
        )

    def test_nested_prefix(self, populated_table):
        keys = populated_table.list_keys(prefix="user/alice")
        assert set(keys) == {"user/alice/email", "user/alice/role"}

    def test_prefix_that_is_a_blob_returns_that_one_key(self, populated_table):
        """`notes` is a top-level blob, not a tree. The path resolves to a
        single key — we return that one key."""
        assert populated_table.list_keys(prefix="notes") == ["notes"]

    def test_nonexistent_prefix_returns_empty(self, populated_table):
        assert populated_table.list_keys(prefix="ghost") == []

    def test_partial_segment_does_not_match(self, populated_table):
        """`use` is not a path segment of `user/...` — strict path semantics."""
        assert populated_table.list_keys(prefix="use") == []

    def test_deep_nonexistent_prefix(self, populated_table):
        assert populated_table.list_keys(prefix="user/alice/email/foo") == []


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestPaginationLimit:
    def test_limit_caps_results(self, populated_table):
        all_keys = populated_table.list_keys()
        page = populated_table.list_keys(limit=3)
        assert page == all_keys[:3]

    def test_limit_zero_returns_empty(self, populated_table):
        assert populated_table.list_keys(limit=0) == []

    def test_limit_larger_than_total_returns_all(self, populated_table):
        all_keys = populated_table.list_keys()
        assert populated_table.list_keys(limit=1000) == all_keys


class TestPaginationAfter:
    def test_after_skips_lex_equal_or_less(self, populated_table):
        all_keys = populated_table.list_keys()
        cutoff = all_keys[2]
        result = populated_table.list_keys(after=cutoff)
        assert all(k > cutoff for k in result)
        assert result == all_keys[3:]

    def test_after_nonexistent_key_uses_lex_position(self, populated_table):
        """`after` need not be an actual key — anything lex-comparable works."""
        result = populated_table.list_keys(after="user/aa")
        # All keys strictly greater than "user/aa"
        assert "user/alice/email" in result
        assert "config/timeout" not in result

    def test_after_past_end_returns_empty(self, populated_table):
        assert populated_table.list_keys(after="zzzzzz") == []


class TestPaginationCombined:
    def test_walk_in_pages(self, populated_table):
        """Standard paginated walk: keep calling until len(page) < limit."""
        all_keys = populated_table.list_keys()
        page_size = 3
        collected = []
        after = None
        while True:
            page = populated_table.list_keys(limit=page_size, after=after)
            collected.extend(page)
            if len(page) < page_size:
                break
            after = page[-1]
        assert collected == all_keys

    def test_prefix_plus_pagination(self, populated_table):
        scoped = populated_table.list_keys(prefix="user/")
        page1 = populated_table.list_keys(prefix="user/", limit=2)
        page2 = populated_table.list_keys(prefix="user/", limit=2, after=page1[-1])
        assert page1 + page2 == scoped[:4]


# ---------------------------------------------------------------------------
# list_items
# ---------------------------------------------------------------------------

class TestListItems:
    def test_empty_table(self, table):
        assert table.list_items() == []

    def test_round_trips_keys_and_values(self, populated_table):
        items = populated_table.list_items()
        as_dict = dict(items)
        assert as_dict["user/alice/email"] == "alice@example.com"
        assert as_dict["config/timeout"] == "30"
        assert as_dict["notes"] == "top-level blob"

    def test_results_sorted_by_key(self, populated_table):
        items = populated_table.list_items()
        keys = [k for k, _ in items]
        assert keys == sorted(keys)

    def test_prefix_filters(self, populated_table):
        items = populated_table.list_items(prefix="user/alice")
        assert dict(items) == {
            "user/alice/email": "alice@example.com",
            "user/alice/role": "admin",
        }

    def test_pagination(self, populated_table):
        page1 = populated_table.list_items(limit=2)
        page2 = populated_table.list_items(limit=2, after=page1[-1][0])
        assert len(page1) == 2 and len(page2) == 2
        # Pages don't overlap and stay sorted.
        assert page1[-1][0] < page2[0][0]

    def test_cross_clone_visibility(self, two_stores):
        a, b = two_stores
        a.create_table("pm")
        a.table("pm").set("foo", "from-a")
        a.table("pm").set("nest/bar", "deeper")
        # B reads through list_items.
        assert dict(b.table("pm").list_items()) == {
            "foo": "from-a",
            "nest/bar": "deeper",
        }


# ---------------------------------------------------------------------------
# __iter__
# ---------------------------------------------------------------------------

class TestIter:
    def test_empty_table_iterates_nothing(self, table):
        assert list(table) == []

    def test_yields_all_keys(self, populated_table):
        from_iter = list(populated_table)
        from_list = populated_table.list_keys()
        assert from_iter == from_list

    def test_for_loop_with_getitem(self, populated_table):
        """The classic dict pattern — iterate keys, look up values."""
        seen = {k: populated_table[k] for k in populated_table}
        assert seen["user/alice/email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Errors / validation
# ---------------------------------------------------------------------------

class TestErrors:
    def test_table_not_found(self, store):
        with pytest.raises(TableNotFoundError):
            store.table("ghost").list_keys()

    @pytest.mark.parametrize("bad", [
        "/abs/path",          # absolute path
        "a/..",               # parent dir
        "a/./b",              # dot segment
        "a//b",               # empty segment
        ".git",               # refers to .git
        ".git/config",        # inside .git
    ])
    def test_invalid_prefix_raises(self, populated_table, bad):
        with pytest.raises(GitKVError):
            populated_table.list_keys(prefix=bad)

    @pytest.mark.parametrize("bad", [42, b"bytes", ["list"]])
    def test_non_string_prefix_raises(self, populated_table, bad):
        with pytest.raises(GitKVError):
            populated_table.list_keys(prefix=bad)

    def test_negative_limit_rejected(self, populated_table):
        with pytest.raises(GitKVError, match="limit"):
            populated_table.list_keys(limit=-1)

    def test_non_int_limit_rejected(self, populated_table):
        with pytest.raises(GitKVError, match="limit"):
            populated_table.list_keys(limit="10")

    def test_non_string_after_rejected(self, populated_table):
        with pytest.raises(GitKVError, match="after"):
            populated_table.list_keys(after=42)


# ---------------------------------------------------------------------------
# Unit-ish coverage of the small helpers (no git involved)
# ---------------------------------------------------------------------------

class TestNormalizePathPrefix:
    @pytest.mark.parametrize("raw,expected", [
        ("", ""),
        (None, ""),
        ("/", ""),                          # lone slash = root
        ("user", "user"),
        ("user/", "user"),                  # trailing slash stripped
        ("user/alice", "user/alice"),
        ("user/alice/", "user/alice"),
        ("user\\alice", "user/alice"),      # backslash folded to /
    ])
    def test_normalization(self, raw, expected):
        assert _normalize_path_prefix(raw) == expected

    @pytest.mark.parametrize("raw", ["/user", "/user/", "/abs/path"])
    def test_absolute_path_rejected(self, raw):
        with pytest.raises(GitKVError, match="relative"):
            _normalize_path_prefix(raw)


class TestApplyPagination:
    def test_no_after_no_limit_returns_all(self):
        assert _apply_pagination(["a", "b", "c"], None, None) == ["a", "b", "c"]

    def test_after_skips_to_strictly_greater(self):
        assert _apply_pagination(["a", "b", "c", "d"], "b", None) == ["c", "d"]

    def test_limit_caps(self):
        assert _apply_pagination(["a", "b", "c"], None, 2) == ["a", "b"]

    def test_combined(self):
        assert _apply_pagination(["a", "b", "c", "d", "e"], "b", 2) == ["c", "d"]

    def test_limit_zero(self):
        assert _apply_pagination(["a", "b"], None, 0) == []
