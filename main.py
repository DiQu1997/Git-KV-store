import argparse
import sys

from Git_KV import GitKVError, GitKVFileBasedSync


def main():
    parser = argparse.ArgumentParser(
        description="Read or update a key in the Git-based key-value store.",
    )
    parser.add_argument("repo_path", help="Path to the Git repository")
    parser.add_argument("key", help="The key to read or update")
    parser.add_argument(
        "-v", "--value", help="The value to set for the key (if updating)"
    )
    parser.add_argument(
        "-d", "--delete", action="store_true", help="Delete the key"
    )
    parser.add_argument(
        "-r",
        "--retry-attempts",
        type=int,
        default=5,
        help="Max retry attempts on non-fast-forward push (default: 5)",
    )
    args = parser.parse_args()

    if args.delete and args.value is not None:
        parser.error("Cannot use --value and --delete together.")

    try:
        store = GitKVFileBasedSync(
            args.repo_path, max_retries=args.retry_attempts
        )
    except GitKVError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.delete:
            store.delete(args.key)
        elif args.value is not None:
            store.set(args.key, args.value)
        else:
            value = store.get(args.key)
            if value is None:
                print(f"Key '{args.key}' not found.")
            else:
                print(value)
    except GitKVError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
