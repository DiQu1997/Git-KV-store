"""Config resolution: file IO + env vars + CLI > env > config > default
precedence. These are pure-Python and don't touch git."""

import os

import pytest

from gitkv._config import (
    Config,
    ENV_CONFIG,
    ENV_REPO,
    ENV_ROTATION_THRESHOLD,
    ENV_TABLE,
    config_path,
    describe_sources,
    resolve_repo,
    resolve_rotation_threshold,
    resolve_table,
    write_config,
)
from gitkv._store import DEFAULT_ROTATION_THRESHOLD


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the config helpers at a tmp file and clear all GITKV_* env vars
    so the test starts from a known empty state."""
    cfg_file = tmp_path / "config.ini"
    monkeypatch.setenv(ENV_CONFIG, str(cfg_file))
    for var in [ENV_REPO, ENV_TABLE, ENV_ROTATION_THRESHOLD]:
        monkeypatch.delenv(var, raising=False)
    return cfg_file


class TestConfigPath:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom.ini"
        monkeypatch.setenv(ENV_CONFIG, str(custom))
        assert config_path() == custom

    def test_expands_tilde_in_env(self, monkeypatch):
        monkeypatch.setenv(ENV_CONFIG, "~/some/where.ini")
        assert str(config_path()).startswith(os.path.expanduser("~"))

    def test_xdg_config_home_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_CONFIG, raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert config_path() == tmp_path / "gitkv" / "config.ini"


class TestConfigFromFile:
    def test_missing_file_returns_empty_config(self, isolated_config):
        cfg = Config.from_file()
        assert cfg.repo is None
        assert cfg.table is None
        assert cfg.rotation_threshold is None

    def test_round_trip(self, isolated_config):
        write_config({"repo": "/x", "table": "pm", "rotation_threshold": 50})
        cfg = Config.from_file()
        assert cfg.repo == "/x"
        assert cfg.table == "pm"
        assert cfg.rotation_threshold == 50

    def test_partial_keys(self, isolated_config):
        write_config({"repo": "/x"})
        cfg = Config.from_file()
        assert cfg.repo == "/x"
        assert cfg.table is None


class TestWriteConfig:
    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "nested" / "config.ini"
        monkeypatch.setenv(ENV_CONFIG, str(nested))
        write_config({"repo": "/x"})
        assert nested.exists()

    def test_unset_removes_key(self, isolated_config):
        write_config({"repo": "/x", "table": "pm"})
        write_config({"table": None})
        cfg = Config.from_file()
        assert cfg.repo == "/x"
        assert cfg.table is None

    def test_rejects_unknown_key(self, isolated_config):
        with pytest.raises(ValueError, match="Unknown config key"):
            write_config({"made_up_key": "x"})


class TestResolveRepo:
    def test_cli_wins_over_env_and_config(self, isolated_config, monkeypatch):
        monkeypatch.setenv(ENV_REPO, "/from-env")
        write_config({"repo": "/from-config"})
        cfg = Config.from_file()
        assert resolve_repo("/from-cli", cfg).endswith("/from-cli")

    def test_env_wins_over_config(self, isolated_config, monkeypatch):
        monkeypatch.setenv(ENV_REPO, "/from-env")
        write_config({"repo": "/from-config"})
        cfg = Config.from_file()
        assert resolve_repo(None, cfg).endswith("/from-env")

    def test_config_used_when_no_env_no_cli(self, isolated_config):
        write_config({"repo": "/from-config"})
        cfg = Config.from_file()
        assert resolve_repo(None, cfg).endswith("/from-config")

    def test_returns_none_when_nothing_set(self, isolated_config):
        cfg = Config.from_file()
        assert resolve_repo(None, cfg) is None

    def test_expands_tilde(self, isolated_config):
        cfg = Config.from_file()
        result = resolve_repo("~/somewhere", cfg)
        assert os.path.isabs(result)
        assert os.path.expanduser("~") in result


class TestResolveTable:
    def test_priority_order(self, isolated_config, monkeypatch):
        monkeypatch.setenv(ENV_TABLE, "env_table")
        write_config({"table": "config_table"})
        cfg = Config.from_file()
        assert resolve_table("cli_table", cfg) == "cli_table"
        assert resolve_table(None, cfg) == "env_table"
        monkeypatch.delenv(ENV_TABLE)
        cfg2 = Config.from_file()
        assert resolve_table(None, cfg2) == "config_table"


class TestResolveRotationThreshold:
    def test_default_when_nothing_set(self, isolated_config):
        cfg = Config.from_file()
        assert resolve_rotation_threshold(None, cfg) == DEFAULT_ROTATION_THRESHOLD

    def test_env_var_parsed_as_int(self, isolated_config, monkeypatch):
        monkeypatch.setenv(ENV_ROTATION_THRESHOLD, "42")
        cfg = Config.from_file()
        assert resolve_rotation_threshold(None, cfg) == 42

    def test_cli_int_wins(self, isolated_config, monkeypatch):
        monkeypatch.setenv(ENV_ROTATION_THRESHOLD, "42")
        write_config({"rotation_threshold": 100})
        cfg = Config.from_file()
        assert resolve_rotation_threshold(7, cfg) == 7


class TestDescribeSources:
    def test_marks_default_source(self, isolated_config):
        rows = describe_sources(Config.from_file())
        sources = {k: src for k, _, src in rows}
        assert sources["repo"] == "unset"
        assert sources["table"] == "unset"
        assert sources["rotation_threshold"] == "default"

    def test_marks_config_and_env(self, isolated_config, monkeypatch):
        write_config({"repo": "/x"})
        monkeypatch.setenv(ENV_TABLE, "pm")
        rows = {k: (v, src) for k, v, src in describe_sources(Config.from_file())}
        assert rows["repo"][1] == "config"
        assert rows["table"][1] == f"env:{ENV_TABLE}"
