"""Dict-style ergonomics on GitKVStore and GitKVTable, plus the
`gitkv.open()` factory that resolves a repo via the same cascade the CLI uses.

The semantics intentionally diverge from the explicit API on missing keys:

  - `table.get(k)` returns None when k is absent (explicit, no exception)
  - `table[k]` raises KeyError when k is absent (dict semantics)
  - `del table[k]` raises KeyError when k is absent; `table.delete(k)` is silent

That split lets callers pick whichever surface is right for their context.
"""

import os

import pytest

import gitkv
from gitkv import GitKVError
from gitkv._config import ENV_CONFIG, ENV_REPO


@pytest.fixture
def db(store):
    """Alias the existing `store` fixture to make tests read more naturally."""
    return store


@pytest.fixture
def db_with_table(db):
    db.create_table("pm")
    return db


# ---------------------------------------------------------------------------
# GitKVTable dict semantics
# ---------------------------------------------------------------------------

class TestTableGetItem:
    def test_returns_value(self, db_with_table):
        db_with_table["pm"]["foo"] = "hello"
        assert db_with_table["pm"]["foo"] == "hello"

    def test_missing_key_raises_keyerror(self, db_with_table):
        with pytest.raises(KeyError) as exc:
            db_with_table["pm"]["nope"]
        assert "nope" in str(exc.value)

    def test_nested_key(self, db_with_table):
        db_with_table["pm"]["a/b/c"] = "deep"
        assert db_with_table["pm"]["a/b/c"] == "deep"


class TestTableSetItem:
    def test_creates_value(self, db_with_table):
        db_with_table["pm"]["foo"] = "hello"
        assert db_with_table["pm"].get("foo") == "hello"

    def test_overwrites(self, db_with_table):
        db_with_table["pm"]["foo"] = "v1"
        db_with_table["pm"]["foo"] = "v2"
        assert db_with_table["pm"]["foo"] == "v2"


class TestTableDelItem:
    def test_removes_existing(self, db_with_table):
        db_with_table["pm"]["foo"] = "hello"
        del db_with_table["pm"]["foo"]
        assert db_with_table["pm"].get("foo") is None

    def test_missing_key_raises_keyerror(self, db_with_table):
        with pytest.raises(KeyError) as exc:
            del db_with_table["pm"]["never-set"]
        assert "never-set" in str(exc.value)

    def test_explicit_delete_is_still_silent(self, db_with_table):
        """The explicit API stays silent on missing — only dict-style raises."""
        db_with_table["pm"].delete("never-set")  # no exception


class TestTableContains:
    def test_existing_key_true(self, db_with_table):
        db_with_table["pm"]["foo"] = "hello"
        assert "foo" in db_with_table["pm"]

    def test_missing_key_false(self, db_with_table):
        assert "foo" not in db_with_table["pm"]

    def test_after_delete_false(self, db_with_table):
        db_with_table["pm"]["foo"] = "hello"
        del db_with_table["pm"]["foo"]
        assert "foo" not in db_with_table["pm"]


# ---------------------------------------------------------------------------
# GitKVStore dict semantics
# ---------------------------------------------------------------------------

class TestStoreGetItem:
    def test_returns_table_object(self, db_with_table):
        from gitkv import GitKVTable
        assert isinstance(db_with_table["pm"], GitKVTable)

    def test_validates_prefix(self, db):
        with pytest.raises(GitKVError):
            db["INVALID-PREFIX"]


class TestStoreContains:
    def test_existing_table(self, db_with_table):
        assert "pm" in db_with_table

    def test_missing_table(self, db):
        assert "ghost" not in db


class TestStoreIter:
    def test_iter_yields_table_names(self, db):
        db.create_table("pm")
        db.create_table("analytics")
        assert set(iter(db)) == {"pm", "analytics"}

    def test_len_counts_tables(self, db):
        assert len(db) == 0
        db.create_table("pm")
        assert len(db) == 1
        db.create_table("analytics")
        assert len(db) == 2


# ---------------------------------------------------------------------------
# gitkv.open() factory
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """Empty GITKV_* env vars + redirect config file to a tmp path."""
    monkeypatch.setenv(ENV_CONFIG, str(tmp_path / "config.ini"))
    monkeypatch.delenv(ENV_REPO, raising=False)


class TestOpenFactory:
    def test_with_explicit_path(self, clone):
        db = gitkv.open(str(clone))
        assert db.repo_path == str(clone)

    def test_reads_from_env(self, clone, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_REPO, str(clone))
        db = gitkv.open()
        # resolve_repo() does abspath; compare via os.path.realpath to handle
        # any /private symlinking on macOS-style tmp paths.
        assert os.path.realpath(db.repo_path) == os.path.realpath(str(clone))

    def test_reads_from_config_file(self, clone, clean_env):
        from gitkv._config import write_config
        write_config({"repo": str(clone)})
        db = gitkv.open()
        assert os.path.realpath(db.repo_path) == os.path.realpath(str(clone))

    def test_no_repo_raises_clear_error(self, clean_env):
        with pytest.raises(GitKVError, match="No repo configured"):
            gitkv.open()

    def test_works_end_to_end(self, clone):
        db = gitkv.open(str(clone))
        db.create_table("pm")
        db["pm"]["user/alice"] = "alice@example.com"
        assert db["pm"]["user/alice"] == "alice@example.com"
        assert "pm" in db
        assert list(db) == ["pm"]
