"""User config + env-var defaults for gitkv.

Resolution priority (highest wins):

    1. Explicit CLI flag (`--repo`, `--table`, `--rotation-threshold`)
    2. Environment variable (`GITKV_REPO`, `GITKV_TABLE`, `GITKV_ROTATION_THRESHOLD`)
    3. Config file (default `~/.config/gitkv/config.ini`, override via `GITKV_CONFIG`)
    4. Hard-coded defaults — only `rotation_threshold` has one

If a value is still unset after the cascade and a command needs it, the CLI
raises a clear error pointing at the fix (set the config key, set the env var,
or pass the flag).
"""

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gitkv._store import DEFAULT_ROTATION_THRESHOLD


CONFIG_SECTION = "gitkv"
ENV_REPO = "GITKV_REPO"
ENV_TABLE = "GITKV_TABLE"
ENV_ROTATION_THRESHOLD = "GITKV_ROTATION_THRESHOLD"
ENV_CONFIG = "GITKV_CONFIG"

# The keys allowed in the config file / `gitkv config` subcommand.
ALLOWED_KEYS = frozenset({"repo", "table", "rotation_threshold"})


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw) if raw else Path.home() / ".config"


def config_path() -> Path:
    """Resolved config file path. Override via `GITKV_CONFIG`."""
    raw = os.environ.get(ENV_CONFIG)
    if raw:
        return Path(raw).expanduser()
    return _xdg_config_home() / "gitkv" / "config.ini"


@dataclass
class Config:
    repo: Optional[str] = None
    table: Optional[str] = None
    rotation_threshold: Optional[int] = None

    @classmethod
    def from_file(cls, path: Optional[Path] = None) -> "Config":
        path = path or config_path()
        cfg = cls()
        if not path.exists():
            return cfg
        parser = configparser.ConfigParser()
        parser.read(path)
        if CONFIG_SECTION not in parser:
            return cfg
        section = parser[CONFIG_SECTION]
        cfg.repo = section.get("repo") or None
        cfg.table = section.get("table") or None
        rt = section.get("rotation_threshold")
        cfg.rotation_threshold = int(rt) if rt else None
        return cfg


def write_config(updates: dict, path: Optional[Path] = None) -> Path:
    """Update keys in the config file. Pass value=None to delete a key.

    Returns the path that was written (or would have been written).
    """
    path = path or config_path()
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    if CONFIG_SECTION not in parser:
        parser[CONFIG_SECTION] = {}
    section = parser[CONFIG_SECTION]
    for key, value in updates.items():
        if key not in ALLOWED_KEYS:
            raise ValueError(
                f"Unknown config key {key!r}. Allowed: {sorted(ALLOWED_KEYS)}"
            )
        if value is None:
            section.pop(key, None)
        else:
            section[key] = str(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        parser.write(f)
    return path


def resolve_repo(cli_value: Optional[str], cfg: Config) -> Optional[str]:
    """Resolve the repo path from CLI > env > config. Returns None if unset.

    Expands `~` and resolves to an absolute path so subprocess calls don't
    depend on the caller's cwd.
    """
    raw = cli_value or os.environ.get(ENV_REPO) or cfg.repo
    if not raw:
        return None
    return str(Path(raw).expanduser().resolve())


def resolve_table(cli_value: Optional[str], cfg: Config) -> Optional[str]:
    return cli_value or os.environ.get(ENV_TABLE) or cfg.table


def resolve_rotation_threshold(cli_value: Optional[int], cfg: Config) -> int:
    if cli_value is not None:
        return cli_value
    env = os.environ.get(ENV_ROTATION_THRESHOLD)
    if env:
        return int(env)
    if cfg.rotation_threshold is not None:
        return cfg.rotation_threshold
    return DEFAULT_ROTATION_THRESHOLD


def describe_sources(cfg: Config) -> list:
    """Return [(key, value, source)] for every effective setting, even the
    unset ones. Used by `gitkv config list`."""
    rows = []
    for key, env_var, resolver in [
        ("repo", ENV_REPO, lambda: resolve_repo(None, cfg)),
        ("table", ENV_TABLE, lambda: resolve_table(None, cfg)),
        ("rotation_threshold", ENV_ROTATION_THRESHOLD,
         lambda: resolve_rotation_threshold(None, cfg)),
    ]:
        value = resolver()
        if os.environ.get(env_var):
            source = f"env:{env_var}"
        elif getattr(cfg, key) is not None:
            source = "config"
        elif value is not None:
            source = "default"
        else:
            source = "unset"
        rows.append((key, value, source))
    return rows
