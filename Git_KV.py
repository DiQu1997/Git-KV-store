import os
from git import Repo, InvalidGitRepositoryError, NoSuchPathError, InvalidGitRepositoryError, GitCommandError
import shutil

class GitKVFileBasedSync:
    def __init__(self, repo_path, remote_name="origin", branch_name="main"):
        self.repo_path = repo_path
        self.remote_name = remote_name
        self.branch_name = branch_name

        try:
            self.repo = Repo(repo_path)
        except (InvalidGitRepositoryError, NoSuchPathError):
            print(f"Error: Not a valid Git repository or path doesn't exist: {repo_path}")
            exit(1)

    def _get_file_path(self, key):
        # Sanitize the key to create a valid file path
        # (You might need more robust sanitization depending on your keys)
        return os.path.join(self.repo_path, key)

    def _pull(self):
        try:
            origin = self.repo.remote(name=self.remote_name)
            origin.pull()
        except GitCommandError as e:
            print(f"Error during git pull: {e}")
            raise

    def _commit_and_push(self, message):
        try:
            self.repo.git.add(A=True) # Stage all changes (including deletions)
            self.repo.index.commit(message)
            origin = self.repo.remote(name=self.remote_name)
            origin.push()
        except GitCommandError as e:
            print(f"Error during git commit or push: {e}")
            # Check for specific error messages
            if "failed to push" in str(e).lower() and "updates were rejected" in str(e).lower():
                print("Retrying after reset and pull")
                self.repo.git.reset('--hard', 'HEAD^')
                self._pull()
                return False  # Indicate retry needed
            elif "another git process seems to be running" in str(e).lower():
                print("Error: Another Git process is running. Please complete or terminate it and try again.")
                # Handle the error (e.g., retry after a delay, prompt the user, etc.)
            elif "unable to access" in str(e).lower() and "Could not resolve host" in str(e).lower():
                print("Error: Unable to access the remote repository. Check your network connection.")
                # Handle the network error
            else:
                print("An unexpected Git error occurred.")
                # Log the error for further investigation
                # Potentially raise the exception or handle it gracefully
            raise
        

    def get(self, key):
        self._pull()  # Always pull before reading
        file_path = self._get_file_path(key)
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return f.read()
        return None

    def set(self, key, value, retry=True):
        self._pull()
        file_path = self._get_file_path(key)
        with open(file_path, 'w') as f:
            f.write(value)
        success = self._commit_and_push(f"Set key: {key}")
        if not success and retry:
            print("Retrying set operation...")
            self.set(key, value, retry=False)

    def delete(self, key, retry=True):
        self._pull()
        file_path = self._get_file_path(key)
        if os.path.exists(file_path):
            os.remove(file_path)
            success = self._commit_and_push(f"Delete key: {key}")
            if not success and retry:
                print("Retrying delete operation...")
                self.delete(key, retry=False)
        else:
            print(f"Key '{key}' not found")
