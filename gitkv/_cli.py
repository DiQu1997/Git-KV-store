"""Command-line interface for gitkv.

Daily commands resolve their repo path and (where applicable) table name from
this cascade — see `gitkv config --help` for details:

    1. CLI flag (`--repo`, `--table`)
    2. Env var (`GITKV_REPO`, `GITKV_TABLE`)
    3. Config file (`~/.config/gitkv/config.ini`)

So after `gitkv config set repo ~/kv` + `gitkv config set table pm`, daily
use is just `gitkv set foo bar` / `gitkv get foo`.
"""

import argparse
import sys

from gitkv import (
    GitKVError,
    GitKVStore,
    SyncDivergedError,
    TableAlreadyExistsError,
    TableNotFoundError,
)
from gitkv._config import (
    ALLOWED_KEYS,
    VALID_MODES,
    Config,
    config_path,
    describe_sources,
    resolve_mode,
    resolve_repo,
    resolve_rotation_threshold,
    resolve_table,
    write_config,
)


def _add_repo_flag(p):
    p.add_argument(
        "--repo",
        help="Repo path (overrides GITKV_REPO env var and config file)",
    )
    p.add_argument(
        "--rotation-threshold",
        type=int,
        default=None,
        help="Commit-count threshold for auto-rotation",
    )
    p.add_argument(
        "--mode",
        choices=list(VALID_MODES),
        default=None,
        help=(
            "online: every op syncs with the remote (default). "
            "offline: ops are local-only; use pull/push/sync to exchange "
            "with the remote"
        ),
    )


def _add_table_flag(p):
    p.add_argument(
        "-t", "--table",
        help="Table name (overrides GITKV_TABLE env var and config file)",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="gitkv",
        description=(
            "Git-backed key-value store. Configure a default repo and table "
            "via `gitkv config set ...` so daily commands don't need flags."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Data commands -------------------------------------------------------
    for name, help_text, extras in [
        ("get", "Read a key", [("key", {})]),
        ("set", "Write a key", [("key", {}), ("value", {})]),
        ("delete", "Delete a key", [("key", {})]),
        ("rotate", "Force rotation of the table's log", []),
    ]:
        p = sub.add_parser(name, help=help_text)
        _add_repo_flag(p)
        _add_table_flag(p)
        for arg_name, kwargs in extras:
            p.add_argument(arg_name, **kwargs)

    # Table-management commands ------------------------------------------
    p_create = sub.add_parser("create-table", help="Create a new table")
    _add_repo_flag(p_create)
    p_create.add_argument("table", help="Prefix / name of the new table")

    p_list = sub.add_parser("list-tables", help="List known tables")
    _add_repo_flag(p_list)

    # Sync commands (always talk to the remote, whatever the mode) ---------
    for name, help_text in [
        ("pull", "Fetch the remote and fast-forward local branches"),
        ("push", "Publish locally-ahead branches to the remote"),
        ("sync", "pull then push"),
        ("status", "Show per-branch ahead/behind state vs the remote"),
    ]:
        p = sub.add_parser(name, help=help_text)
        _add_repo_flag(p)

    # Config command ------------------------------------------------------
    p_config = sub.add_parser("config", help="Manage user defaults")
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True)

    config_sub.add_parser("list", help="Show effective config and its source")
    config_sub.add_parser("path", help="Print the config file path")

    p_cfg_get = config_sub.add_parser("get", help="Print one config value")
    p_cfg_get.add_argument("key", choices=sorted(ALLOWED_KEYS))

    p_cfg_set = config_sub.add_parser("set", help="Set one config value")
    p_cfg_set.add_argument("key", choices=sorted(ALLOWED_KEYS))
    p_cfg_set.add_argument("value")

    p_cfg_unset = config_sub.add_parser("unset", help="Remove one config value")
    p_cfg_unset.add_argument("key", choices=sorted(ALLOWED_KEYS))

    return parser


# ---------------------------------------------------------------------------
# Resolution helpers — small wrappers that turn unset values into clear errors
# ---------------------------------------------------------------------------

def _require_repo(args, cfg):
    repo = resolve_repo(getattr(args, "repo", None), cfg)
    if not repo:
        raise GitKVError(
            "No repo configured. Set one with `gitkv config set repo PATH`, "
            "export GITKV_REPO=..., or pass --repo."
        )
    return repo


def _require_table(args, cfg):
    table = resolve_table(getattr(args, "table", None), cfg)
    if not table:
        raise GitKVError(
            "No table configured. Set one with `gitkv config set table NAME`, "
            "export GITKV_TABLE=..., or pass --table / -t."
        )
    return table


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_data_command(args, cfg, store):
    table_name = _require_table(args, cfg)
    table = store.table(table_name)
    if args.cmd == "get":
        value = table.get(args.key)
        if value is None:
            print(f"Key '{args.key}' not found.")
        else:
            print(value)
    elif args.cmd == "set":
        table.set(args.key, args.value)
    elif args.cmd == "delete":
        table.delete(args.key)
    elif args.cmd == "rotate":
        new_branch = table.rotate()
        print(f"Rotated. New active log: {new_branch}")


def _print_pull_summary(result):
    if result["created"]:
        print(f"Created {len(result['created'])} local branch(es): "
              f"{', '.join(result['created'])}")
    if result["updated"]:
        print(f"Fast-forwarded {len(result['updated'])} branch(es): "
              f"{', '.join(result['updated'])}")
    if not result["created"] and not result["updated"]:
        print("Already up to date.")
    if result["ahead"] or result["local_only"]:
        pending = list(result["ahead"]) + result["local_only"]
        print(f"Local work not yet pushed on: {', '.join(pending)} "
              f"(run `gitkv push`)")


def _print_push_summary(result):
    if result["pushed"]:
        print(f"Pushed {len(result['pushed'])} branch(es): "
              f"{', '.join(result['pushed'])}")
    else:
        print("Nothing to push.")


def _handle_sync_command(args, store):
    if args.cmd == "pull":
        _print_pull_summary(store.pull())
        return
    if args.cmd == "push":
        _print_push_summary(store.push())
        return
    if args.cmd == "sync":
        result = store.sync()
        _print_pull_summary(result["pull"])
        _print_push_summary(result["push"])
        return
    if args.cmd == "status":
        st = store.status()
        lines = []
        for branch, n in st["ahead"].items():
            lines.append(f"{branch}: ahead {n}")
        for branch, n in st["behind"].items():
            lines.append(f"{branch}: behind {n}")
        for branch in st["diverged"]:
            lines.append(f"{branch}: DIVERGED — reconcile manually")
        for branch in st["local_only"]:
            lines.append(f"{branch}: local only (will push)")
        for branch in st["remote_only"]:
            lines.append(f"{branch}: remote only (will pull)")
        if lines:
            print("\n".join(sorted(lines)))
        else:
            print("In sync with remote.")
        return


def _handle_config_command(args):
    if args.config_cmd == "path":
        print(config_path())
        return
    if args.config_cmd == "list":
        for key, value, source in describe_sources(Config.from_file()):
            shown = value if value is not None else "(unset)"
            print(f"{key} = {shown}    [{source}]")
        return
    if args.config_cmd == "get":
        cfg = Config.from_file()
        value = getattr(cfg, args.key)
        if value is None:
            sys.exit(1)
        print(value)
        return
    if args.config_cmd == "set":
        path = write_config({args.key: args.value})
        print(f"Set {args.key} = {args.value} in {path}")
        return
    if args.config_cmd == "unset":
        path = write_config({args.key: None})
        print(f"Unset {args.key} in {path}")
        return


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "config":
        try:
            _handle_config_command(args)
        except (ValueError, OSError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    cfg = Config.from_file()

    try:
        repo = _require_repo(args, cfg)
        rotation_threshold = resolve_rotation_threshold(
            getattr(args, "rotation_threshold", None), cfg
        )
        mode = resolve_mode(getattr(args, "mode", None), cfg)
        store = GitKVStore(
            repo,
            rotation_threshold=rotation_threshold,
            offline=(mode == "offline"),
        )
    except (GitKVError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.cmd in ("pull", "push", "sync", "status"):
            _handle_sync_command(args, store)
            return

        if args.cmd == "list-tables":
            for prefix in store.list_tables():
                print(prefix)
            return

        if args.cmd == "create-table":
            store.create_table(args.table)
            print(f"Created table: {args.table}")
            return

        _handle_data_command(args, cfg, store)
    except TableNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    except TableAlreadyExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(3)
    except SyncDivergedError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(4)
    except GitKVError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
