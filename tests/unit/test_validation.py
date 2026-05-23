"""Input validation: keys and table prefixes are checked at the public API
boundary so we never have to guard against bad data deeper in the plumbing."""

import pytest

from Git_KV import GitKVError, _validate_key, _validate_prefix


class TestValidatePrefix:
    @pytest.mark.parametrize("prefix", [
        "pm",
        "analytics",
        "x",
        "a1",
        "a_b_c",
        "a" + "b" * 62,  # 63 chars, the boundary
    ])
    def test_accepts_valid_prefixes(self, prefix):
        _validate_prefix(prefix)  # no exception

    @pytest.mark.parametrize("prefix", [
        "",                # empty
        "1abc",            # leading digit
        "ABC",             # uppercase
        "abc-def",         # hyphen
        "abc.def",         # dot
        "abc def",         # space
        "abc/def",         # slash
        "a" * 64,          # one over the limit
        "_abc",            # leading underscore
    ])
    def test_rejects_invalid_prefixes(self, prefix):
        with pytest.raises(GitKVError):
            _validate_prefix(prefix)

    @pytest.mark.parametrize("prefix", ["main", "tables"])
    def test_rejects_reserved_prefixes(self, prefix):
        with pytest.raises(GitKVError, match="reserved"):
            _validate_prefix(prefix)

    def test_rejects_non_string(self):
        with pytest.raises(GitKVError):
            _validate_prefix(None)
        with pytest.raises(GitKVError):
            _validate_prefix(42)


class TestValidateKey:
    @pytest.mark.parametrize("key,expected_parts", [
        ("foo", ["foo"]),
        ("a/b/c", ["a", "b", "c"]),
        ("x", ["x"]),
        ("with-dash_and.dot", ["with-dash_and.dot"]),
        ("nested/key/with/many/parts", ["nested", "key", "with", "many", "parts"]),
    ])
    def test_accepts_valid_keys(self, key, expected_parts):
        assert _validate_key(key) == expected_parts

    def test_normalizes_backslashes(self):
        assert _validate_key("a\\b\\c") == ["a", "b", "c"]

    @pytest.mark.parametrize("key", [
        "",                  # empty
        "/abs/path",         # absolute path
        "..",                # parent dir
        ".",                 # current dir
        "a/..",              # parent in middle
        "a/./b",             # dot segment
        "a//b",              # empty segment
        ".git",              # refers to .git
        ".git/config",       # tries to escape into .git
    ])
    def test_rejects_invalid_keys(self, key):
        with pytest.raises(GitKVError):
            _validate_key(key)

    @pytest.mark.parametrize("key", [None, 42, b"bytes-not-string", ["list"]])
    def test_rejects_non_string(self, key):
        with pytest.raises(GitKVError):
            _validate_key(key)
