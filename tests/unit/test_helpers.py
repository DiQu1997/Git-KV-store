"""Tiny pure-function helpers: trailer parsing, ref-to-branch, non-FF detection.

These all run without any Git interaction. They're the kind of thing that's
trivial to get subtly wrong (off-by-one in trailer matching, missing an error
phrase in non-FF detection) and the bugs hurt under contention, so we keep
exhaustive coverage of them.
"""

import pytest

from Git_KV import _is_non_ff, _parse_trailer, _ref_to_branch


class TestParseTrailer:
    def test_finds_trailer_at_end(self):
        msg = (
            "Some commit\n"
            "\n"
            "Tombstone-Next-Branch: refs/heads/foo_log_abc\n"
        )
        assert _parse_trailer(msg, "Tombstone-Next-Branch") == "refs/heads/foo_log_abc"

    def test_finds_trailer_among_many(self):
        msg = (
            "Header\n"
            "\n"
            "Tombstone-Version: 1\n"
            "Tombstone-Next-Branch: refs/heads/x\n"
            "Commit-Number: 7\n"
        )
        assert _parse_trailer(msg, "Commit-Number") == "7"
        assert _parse_trailer(msg, "Tombstone-Version") == "1"

    def test_missing_returns_none(self):
        msg = "Header\n\nOther-Key: x\n"
        assert _parse_trailer(msg, "Not-There") is None

    def test_strips_surrounding_whitespace(self):
        msg = "Header\n\nKey:    value-with-spaces   \n"
        assert _parse_trailer(msg, "Key") == "value-with-spaces"

    def test_empty_message_returns_none(self):
        assert _parse_trailer("", "Anything") is None

    def test_prefix_collision_not_a_match(self):
        # "Tombstone-Next" should not match a query for "Tombstone".
        msg = "Header\n\nTombstone-Next-Branch: x\n"
        assert _parse_trailer(msg, "Tombstone") is None


class TestRefToBranch:
    def test_strips_refs_heads(self):
        assert _ref_to_branch("refs/heads/foo") == "foo"

    def test_handles_underscores_and_hex(self):
        assert _ref_to_branch("refs/heads/pm_log_abcd1234") == "pm_log_abcd1234"

    def test_pass_through_when_no_prefix(self):
        # Some callers may pass bare branch names; we accept them.
        assert _ref_to_branch("already_a_branch") == "already_a_branch"


class TestIsNonFf:
    @pytest.mark.parametrize("text", [
        "Updates were rejected because the tip of your current branch is behind",
        "! [rejected] foo -> foo (non-fast-forward)",
        "! [rejected] foo -> foo (fetch first)",
        "remote: error: cannot lock ref 'refs/heads/x': is at A but expected B",
        "error: stale info: refs/heads/x",
        "! [rejected] foo -> foo (would clobber existing tag)",
    ])
    def test_matches_known_non_ff_phrases(self, text):
        assert _is_non_ff(text), f"should match: {text!r}"

    @pytest.mark.parametrize("text", [
        "fatal: Authentication failed",
        "fatal: unable to access 'https://...': Connection refused",
        "error: pathspec 'foo' did not match any file(s) known to git",
        "",
    ])
    def test_does_not_match_other_errors(self, text):
        assert not _is_non_ff(text), f"should NOT match: {text!r}"

    def test_case_insensitive(self):
        assert _is_non_ff("NON-FAST-FORWARD")
        assert _is_non_ff("Cannot Lock Ref")
