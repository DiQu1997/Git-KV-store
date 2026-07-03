"""End-to-end CLI tests: invoke `python -m gitkv` as a subprocess and check
stdout / exit codes for each subcommand.

The CLI takes its repo from `--repo`, `GITKV_REPO`, or the config file. Tests
here exercise all three paths so we catch regressions in the resolution
cascade.
"""

import os
import subprocess
import sys

import pytest

from gitkv._config import ENV_CONFIG, ENV_MODE, ENV_REPO, ENV_TABLE


def run_cli(*args, env_overrides=None, expect_exit=0):
    """Run `python -m gitkv <args>` with a clean GITKV_* env, applying any
    overrides, and return (stdout, stderr)."""
    env = os.environ.copy()
    for var in (ENV_REPO, ENV_TABLE, ENV_CONFIG, ENV_MODE):
        env.pop(var, None)
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-m", "gitkv", *args],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == expect_exit, (
        f"exit {proc.returncode} (expected {expect_exit})\n"
        f"stdout: {proc.stdout!r}\n"
        f"stderr: {proc.stderr!r}"
    )
    return proc.stdout, proc.stderr


@pytest.fixture
def repo_env(clone, tmp_path):
    """Pre-built env with GITKV_REPO pointing at the test's bare-clone and
    GITKV_CONFIG pointing at a sandboxed empty config file."""
    return {
        ENV_REPO: str(clone),
        ENV_CONFIG: str(tmp_path / "config.ini"),
    }


# ---------------------------------------------------------------------------
# --repo flag
# ---------------------------------------------------------------------------

class TestRepoViaFlag:
    def test_list_tables_empty(self, clone):
        out, _ = run_cli("list-tables", "--repo", str(clone))
        assert out == ""

    def test_create_and_list(self, clone):
        out, _ = run_cli("create-table", "pm", "--repo", str(clone))
        assert "Created table: pm" in out
        out, _ = run_cli("list-tables", "--repo", str(clone))
        assert out.strip() == "pm"


# ---------------------------------------------------------------------------
# GITKV_REPO env var
# ---------------------------------------------------------------------------

class TestRepoViaEnv:
    def test_set_then_get(self, repo_env):
        run_cli("create-table", "pm", env_overrides=repo_env)
        run_cli("set", "-t", "pm", "foo", "hello", env_overrides=repo_env)
        out, _ = run_cli("get", "-t", "pm", "foo", env_overrides=repo_env)
        assert out.strip() == "hello"

    def test_get_missing_key_prints_not_found(self, repo_env):
        run_cli("create-table", "pm", env_overrides=repo_env)
        out, _ = run_cli("get", "-t", "pm", "nope", env_overrides=repo_env)
        assert "not found" in out

    def test_delete_then_get(self, repo_env):
        run_cli("create-table", "pm", env_overrides=repo_env)
        run_cli("set", "-t", "pm", "foo", "hello", env_overrides=repo_env)
        run_cli("delete", "-t", "pm", "foo", env_overrides=repo_env)
        out, _ = run_cli("get", "-t", "pm", "foo", env_overrides=repo_env)
        assert "not found" in out

    def test_nested_key(self, repo_env):
        run_cli("create-table", "pm", env_overrides=repo_env)
        run_cli("set", "-t", "pm", "a/b/c", "deep", env_overrides=repo_env)
        out, _ = run_cli("get", "-t", "pm", "a/b/c", env_overrides=repo_env)
        assert out.strip() == "deep"

    def test_rotate(self, repo_env):
        run_cli("create-table", "pm", env_overrides=repo_env)
        run_cli("set", "-t", "pm", "foo", "hello", env_overrides=repo_env)
        out, _ = run_cli("rotate", "-t", "pm", env_overrides=repo_env)
        assert "Rotated. New active log" in out
        out, _ = run_cli("get", "-t", "pm", "foo", env_overrides=repo_env)
        assert out.strip() == "hello"

    def test_table_via_env_var(self, repo_env):
        """GITKV_TABLE lets us drop the -t flag from data commands."""
        run_cli("create-table", "pm", env_overrides=repo_env)
        env = {**repo_env, ENV_TABLE: "pm"}
        run_cli("set", "foo", "hello", env_overrides=env)
        out, _ = run_cli("get", "foo", env_overrides=env)
        assert out.strip() == "hello"


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------

class TestRepoViaConfigFile:
    def test_set_via_config_then_use(self, clone, tmp_path):
        cfg_file = tmp_path / "config.ini"
        env = {ENV_CONFIG: str(cfg_file)}

        run_cli("config", "set", "repo", str(clone), env_overrides=env)
        run_cli("config", "set", "table", "pm", env_overrides=env)

        # No --repo / GITKV_REPO; resolution comes from the config file.
        run_cli("create-table", "pm", env_overrides=env)
        run_cli("set", "foo", "hello", env_overrides=env)
        out, _ = run_cli("get", "foo", env_overrides=env)
        assert out.strip() == "hello"

    def test_config_list_shows_sources(self, clone, tmp_path):
        cfg_file = tmp_path / "config.ini"
        env = {ENV_CONFIG: str(cfg_file)}
        run_cli("config", "set", "repo", str(clone), env_overrides=env)
        env_with_table = {**env, ENV_TABLE: "from_env"}

        out, _ = run_cli("config", "list", env_overrides=env_with_table)
        assert "repo" in out
        assert "[config]" in out
        assert "from_env" in out
        assert f"[env:{ENV_TABLE}]" in out

    def test_config_path_prints_resolved_path(self, tmp_path):
        cfg_file = tmp_path / "config.ini"
        out, _ = run_cli("config", "path", env_overrides={ENV_CONFIG: str(cfg_file)})
        assert out.strip() == str(cfg_file)

    def test_unset_removes_key(self, clone, tmp_path):
        cfg_file = tmp_path / "config.ini"
        env = {ENV_CONFIG: str(cfg_file)}
        run_cli("config", "set", "table", "pm", env_overrides=env)
        run_cli("config", "unset", "table", env_overrides=env)
        # `config get` exits 1 when the value is unset.
        run_cli("config", "get", "table", env_overrides=env, expect_exit=1)


# ---------------------------------------------------------------------------
# Resolution-order checks (CLI > env > config)
# ---------------------------------------------------------------------------

class TestResolutionOrder:
    def test_cli_table_overrides_env_table(self, make_clone, tmp_path):
        clone = make_clone("client")
        env = {
            ENV_REPO: str(clone),
            ENV_CONFIG: str(tmp_path / "config.ini"),
            ENV_TABLE: "wrong_table",
        }
        run_cli("create-table", "right_table", env_overrides=env)
        # -t flag overrides GITKV_TABLE.
        run_cli("set", "-t", "right_table", "foo", "ok", env_overrides=env)
        out, _ = run_cli("get", "-t", "right_table", "foo", env_overrides=env)
        assert out.strip() == "ok"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

class TestErrors:
    def test_no_repo_anywhere_errors_clearly(self, tmp_path):
        _, err = run_cli(
            "list-tables",
            env_overrides={ENV_CONFIG: str(tmp_path / "config.ini")},
            expect_exit=1,
        )
        assert "No repo configured" in err

    def test_no_table_when_required_errors_clearly(self, clone, tmp_path):
        env = {ENV_REPO: str(clone), ENV_CONFIG: str(tmp_path / "config.ini")}
        run_cli("create-table", "pm", env_overrides=env)
        _, err = run_cli("set", "foo", "bar", env_overrides=env, expect_exit=1)
        assert "No table configured" in err

    def test_unknown_table_exits_2(self, repo_env):
        _, err = run_cli("get", "-t", "ghost", "foo",
                         env_overrides=repo_env, expect_exit=2)
        assert "does not exist" in err

    def test_duplicate_create_exits_3(self, repo_env):
        run_cli("create-table", "pm", env_overrides=repo_env)
        _, err = run_cli("create-table", "pm",
                         env_overrides=repo_env, expect_exit=3)
        assert "already exists" in err

    def test_invalid_repo_exits_1(self, tmp_path):
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        env = {
            ENV_REPO: str(not_a_repo),
            ENV_CONFIG: str(tmp_path / "config.ini"),
        }
        _, err = run_cli("list-tables", env_overrides=env, expect_exit=1)
        assert "Not a git repository" in err


# ---------------------------------------------------------------------------
# Offline mode + sync commands
# ---------------------------------------------------------------------------

def _break_remote(clone):
    subprocess.run(
        ["git", "-C", str(clone), "remote", "set-url", "origin",
         "/nonexistent/gitkv-blackhole.git"],
        check=True, capture_output=True,
    )


class TestOfflineCli:
    def test_mode_flag_works_without_network(self, clone, tmp_path):
        _break_remote(clone)
        env = {ENV_REPO: str(clone), ENV_CONFIG: str(tmp_path / "config.ini")}
        run_cli("create-table", "pm", "--mode", "offline", env_overrides=env)
        run_cli("set", "-t", "pm", "foo", "bar", "--mode", "offline",
                env_overrides=env)
        out, _ = run_cli("get", "-t", "pm", "foo", "--mode", "offline",
                         env_overrides=env)
        assert out.strip() == "bar"

    def test_mode_env_var(self, clone, tmp_path):
        _break_remote(clone)
        env = {
            ENV_REPO: str(clone),
            ENV_CONFIG: str(tmp_path / "config.ini"),
            ENV_MODE: "offline",
        }
        run_cli("create-table", "pm", env_overrides=env)
        out, _ = run_cli("list-tables", env_overrides=env)
        assert out.strip() == "pm"

    def test_mode_via_config_file(self, clone, tmp_path):
        _break_remote(clone)
        env = {ENV_REPO: str(clone), ENV_CONFIG: str(tmp_path / "config.ini")}
        run_cli("config", "set", "mode", "offline", env_overrides=env)
        run_cli("create-table", "pm", env_overrides=env)
        out, _ = run_cli("list-tables", env_overrides=env)
        assert out.strip() == "pm"

    def test_config_rejects_invalid_mode(self, tmp_path):
        env = {ENV_CONFIG: str(tmp_path / "config.ini")}
        _, err = run_cli("config", "set", "mode", "sometimes",
                         env_overrides=env, expect_exit=1)
        assert "Invalid mode" in err


class TestSyncCli:
    def test_status_in_sync(self, clone, tmp_path):
        env = {ENV_REPO: str(clone), ENV_CONFIG: str(tmp_path / "config.ini")}
        out, _ = run_cli("status", env_overrides=env)
        assert "In sync" in out

    def test_offline_write_then_status_push_status(self, clone, tmp_path):
        env = {
            ENV_REPO: str(clone),
            ENV_CONFIG: str(tmp_path / "config.ini"),
            ENV_MODE: "offline",
        }
        run_cli("create-table", "pm", env_overrides=env)

        out, _ = run_cli("status", env_overrides=env)
        assert "local only" in out

        out, _ = run_cli("push", env_overrides=env)
        assert "Pushed 3 branch(es)" in out

        out, _ = run_cli("status", env_overrides=env)
        assert "In sync" in out

    def test_pull_and_sync_smoke(self, clone, tmp_path):
        env = {ENV_REPO: str(clone), ENV_CONFIG: str(tmp_path / "config.ini")}
        out, _ = run_cli("pull", env_overrides=env)
        assert "Already up to date" in out
        out, _ = run_cli("sync", env_overrides=env)
        assert "Already up to date" in out and "Nothing to push" in out

    def test_diverged_exits_4(self, make_clone, tmp_path):
        # A (online) and B (offline) both advance the same log branch.
        clone_a, clone_b = make_clone("cli_div_a"), make_clone("cli_div_b")
        env_a = {ENV_REPO: str(clone_a), ENV_CONFIG: str(tmp_path / "ca.ini")}
        env_b = {
            ENV_REPO: str(clone_b),
            ENV_CONFIG: str(tmp_path / "cb.ini"),
            ENV_MODE: "offline",
        }
        run_cli("create-table", "pm", env_overrides=env_a)
        run_cli("set", "-t", "pm", "seed", "s", env_overrides=env_a)
        run_cli("pull", env_overrides=env_b)
        run_cli("set", "-t", "pm", "k", "from-a", env_overrides=env_a)
        run_cli("set", "-t", "pm", "k", "from-b", env_overrides=env_b)

        _, err = run_cli("pull", env_overrides=env_b, expect_exit=4)
        assert "diverged" in err
