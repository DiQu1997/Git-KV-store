from Git_KV import GitKVFileBasedSync  # Assuming you saved the class as Git_KV.py
import argparse
import time

def main():
    parser = argparse.ArgumentParser(description="Read or update a key in the Git-based key-value store.")
    parser.add_argument("repo_path", help="Path to the Git repository")
    parser.add_argument("key", help="The key to read or update")
    parser.add_argument("-v", "--value", help="The value to set for the key (if updating)")
    parser.add_argument("-d", "--delete", action="store_true", help="Delete the key")
    parser.add_argument("-r", "--retry_attempts", type=int, default=3, help="Max retry attempts on conflict")

    args = parser.parse_args()

    store = GitKVFileBasedSync(args.repo_path)

    if args.delete:
        if args.value:
            print("Error: Cannot use --value and --delete together.")
            exit(1)
        
        retry_count = 0
        success = False
        while retry_count < args.retry_attempts and not success:
            try:
                store.delete(args.key)
                success = True
            except Exception as e:
                print(f"Error deleting key (Attempt {retry_count+1}/{args.retry_attempts}): {e}")
                retry_count += 1
                if retry_count < args.retry_attempts:
                    print("Retrying in 1 second...")
                    time.sleep(1)
        if not success:
            print(f"Failed to delete key '{args.key}' after multiple retries.")

    elif args.value:
        retry_count = 0
        success = False
        while retry_count < args.retry_attempts and not success:
            try:
                store.set(args.key, args.value)
                success = True
            except Exception as e:
                print(f"Error setting key (Attempt {retry_count+1}/{args.retry_attempts}): {e}")
                retry_count += 1
                if retry_count < args.retry_attempts:
                    print("Retrying in 1 second...")
                    time.sleep(1)
        if not success:
            print(f"Failed to set key '{args.key}' after multiple retries.")

    else:
        value = store.get(args.key)
        if value is None:
            print(f"Key '{args.key}' not found.")
        else:
            print(value)

if __name__ == "__main__":
    main()