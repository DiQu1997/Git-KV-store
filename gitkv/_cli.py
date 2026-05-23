import argparse
import sys

from gitkv import (
    DEFAULT_ROTATION_THRESHOLD,
    GitKVError,
    GitKVStore,
    TableAlreadyExistsError,
    TableNotFoundError,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Git-backed key-value store. Subcommands operate on tables; "
            "get/set/delete take a --table (-t) flag selecting the namespace."
        ),
    )
    parser.add_argument("repo_path", help="Path to the Git repository")
    parser.add_argument(
        "--rotation-threshold",
        type=int,
        default=DEFAULT_ROTATION_THRESHOLD,
        help=f"Commit-count threshold for auto-rotation (default: {DEFAULT_ROTATION_THRESHOLD})",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_get = sub.add_parser("get", help="Read a key")
    p_get.add_argument("-t", "--table", required=True)
    p_get.add_argument("key")

    p_set = sub.add_parser("set", help="Write a key")
    p_set.add_argument("-t", "--table", required=True)
    p_set.add_argument("key")
    p_set.add_argument("value")

    p_del = sub.add_parser("delete", help="Delete a key")
    p_del.add_argument("-t", "--table", required=True)
    p_del.add_argument("key")

    p_create = sub.add_parser("create-table", help="Create a new table")
    p_create.add_argument("table")

    sub.add_parser("list-tables", help="List known tables")

    p_rot = sub.add_parser("rotate", help="Force rotation of a table's log")
    p_rot.add_argument("-t", "--table", required=True)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        store = GitKVStore(
            args.repo_path,
            rotation_threshold=args.rotation_threshold,
        )
    except GitKVError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.cmd == "list-tables":
            for prefix in store.list_tables():
                print(prefix)
            return

        if args.cmd == "create-table":
            store.create_table(args.table)
            print(f"Created table: {args.table}")
            return

        if args.cmd == "rotate":
            new_branch = store.table(args.table).rotate()
            print(f"Rotated. New active log: {new_branch}")
            return

        table = store.table(args.table)
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
        else:
            parser.error(f"Unknown command: {args.cmd}")
    except TableNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    except TableAlreadyExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(3)
    except GitKVError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
