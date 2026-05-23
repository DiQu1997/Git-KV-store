"""End-to-end CLI tests: invoke `python -m gitkv` as a subprocess and check
stdout / exit codes for each subcommand."""

import subprocess
import sys


def run_cli(*args, expect_exit=0):
    """Run `python -m gitkv <args>` and return (stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "gitkv", *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == expect_exit, (
        f"exit {proc.returncode} (expected {expect_exit})\n"
        f"stdout: {proc.stdout!r}\n"
        f"stderr: {proc.stderr!r}"
    )
    return proc.stdout, proc.stderr


def test_list_tables_empty(clone):
    out, _ = run_cli(str(clone), "list-tables")
    assert out == ""


def test_create_and_list(clone):
    out, _ = run_cli(str(clone), "create-table", "pm")
    assert "Created table: pm" in out
    out, _ = run_cli(str(clone), "list-tables")
    assert out.strip() == "pm"


def test_set_then_get(clone):
    run_cli(str(clone), "create-table", "pm")
    run_cli(str(clone), "set", "-t", "pm", "foo", "hello")
    out, _ = run_cli(str(clone), "get", "-t", "pm", "foo")
    assert out.strip() == "hello"


def test_get_missing_key_prints_not_found(clone):
    run_cli(str(clone), "create-table", "pm")
    out, _ = run_cli(str(clone), "get", "-t", "pm", "nope")
    assert "not found" in out


def test_delete_then_get(clone):
    run_cli(str(clone), "create-table", "pm")
    run_cli(str(clone), "set", "-t", "pm", "foo", "hello")
    run_cli(str(clone), "delete", "-t", "pm", "foo")
    out, _ = run_cli(str(clone), "get", "-t", "pm", "foo")
    assert "not found" in out


def test_nested_key(clone):
    run_cli(str(clone), "create-table", "pm")
    run_cli(str(clone), "set", "-t", "pm", "a/b/c", "deep")
    out, _ = run_cli(str(clone), "get", "-t", "pm", "a/b/c")
    assert out.strip() == "deep"


def test_rotate(clone):
    run_cli(str(clone), "create-table", "pm")
    run_cli(str(clone), "set", "-t", "pm", "foo", "hello")
    out, _ = run_cli(str(clone), "rotate", "-t", "pm")
    assert "Rotated. New active log" in out
    # Data still readable after rotation.
    out, _ = run_cli(str(clone), "get", "-t", "pm", "foo")
    assert out.strip() == "hello"


def test_unknown_table_exits_2(clone):
    _, err = run_cli(str(clone), "get", "-t", "ghost", "foo", expect_exit=2)
    assert "does not exist" in err


def test_duplicate_create_exits_3(clone):
    run_cli(str(clone), "create-table", "pm")
    _, err = run_cli(str(clone), "create-table", "pm", expect_exit=3)
    assert "already exists" in err


def test_invalid_repo_exits_1(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    _, err = run_cli(str(not_a_repo), "list-tables", expect_exit=1)
    assert "Not a git repository" in err
