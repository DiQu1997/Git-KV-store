import fcntl
import os
import time
import uuid
from contextlib import contextmanager

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo


class GitKVError(Exception):
    """Raised when a KV operation fails permanently."""


class GitKVFileBasedSync:
    """A single-branch, multi-writer key-value store backed by a Git repository.

    Concurrency model: every write goes through a transaction branch created
    locally from the freshly-fetched remote tip; the write is then committed
    on the txn branch and pushed into the canonical branch's ref with
    fast-forward-only semantics. A non-fast-forward rejection means another
    writer pushed first, in which case the local state is re-synced and the
    write is retried.

    This class corresponds to Stage 1 of DESIGN.md (single namespace, no
    tombstones or rotation).
    """

    def __init__(
        self,
        repo_path,
        remote_name="origin",
        branch_name="main",
        max_retries=5,
    ):
        self.repo_path = repo_path
        self.remote_name = remote_name
        self.branch_name = branch_name
        self.max_retries = max_retries

        try:
            self.repo = Repo(repo_path)
        except (InvalidGitRepositoryError, NoSuchPathError) as e:
            raise GitKVError(
                f"Not a valid Git repository or path doesn't exist: {repo_path}"
            ) from e

    # ------------------------------------------------------------------
    # path / locking helpers
    # ------------------------------------------------------------------

    def _key_to_path(self, key):
        if not isinstance(key, str) or not key:
            raise GitKVError(f"Invalid key (must be a non-empty string): {key!r}")
        if key.startswith("/") or key.startswith(os.sep):
            raise GitKVError(f"Invalid key (must be a relative path): {key!r}")
        parts = key.replace("\\", "/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise GitKVError(
                f"Invalid key (empty or '.'/'..' segment not allowed): {key!r}"
            )
        if parts[0] == ".git":
            raise GitKVError(f"Invalid key (refers to .git): {key!r}")
        return os.path.join(self.repo_path, key)

    @contextmanager
    def _lock(self):
        """Process-level exclusive lock protecting the shared working tree,
        index, and HEAD against concurrent in-process operations.
        """
        lock_path = os.path.join(self.repo.git_dir, "kv.lock")
        f = open(lock_path, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()

    # ------------------------------------------------------------------
    # git helpers
    # ------------------------------------------------------------------

    def _fetch(self):
        try:
            self.repo.remote(name=self.remote_name).fetch()
        except GitCommandError as e:
            raise GitKVError(f"Failed to fetch from remote: {e}") from e

    def _sync_local_to_remote(self):
        """Align the local branch and working tree with the remote tip.

        This uses `git reset --hard origin/<branch>`, which is the standard
        "make local match canonical remote" idiom. It is *not* the
        "discard my own commit after a failed push" anti-pattern that the
        previous implementation used: the flock guarantees no in-process
        work-in-progress can be lost here, and no local commit is being
        thrown away (txn-branch commits live on a separate ref).
        """
        self._fetch()
        remote_ref = f"{self.remote_name}/{self.branch_name}"

        head_is_detached = self.repo.head.is_detached
        on_branch = (
            not head_is_detached and self.repo.active_branch.name == self.branch_name
        )
        if not on_branch:
            try:
                self.repo.git.checkout(self.branch_name)
            except GitCommandError:
                self.repo.git.checkout("-B", self.branch_name, remote_ref)
        self.repo.git.reset("--hard", remote_ref)

    @staticmethod
    def _push_succeeded(push_info_list):
        if not push_info_list:
            return False
        for pi in push_info_list:
            if pi.flags & (pi.ERROR | pi.REJECTED | pi.REMOTE_REJECTED):
                return False
        return True

    @staticmethod
    def _is_non_ff(err):
        msg = str(err).lower()
        return (
            "non-fast-forward" in msg
            or "fetch first" in msg
            or "updates were rejected" in msg
        )

    # ------------------------------------------------------------------
    # core write attempt
    # ------------------------------------------------------------------

    def _try_write_once(self, key, value, delete):
        """One CAS attempt.

        Returns True if the push succeeded (or there was nothing to do).
        Returns False if the push was rejected as non-fast-forward; the
        caller should re-sync and retry.
        Raises GitKVError on unexpected failures.
        """
        self._sync_local_to_remote()
        base_sha = self.repo.git.rev_parse("HEAD")

        txn = f"_kv_txn_{uuid.uuid4().hex[:8]}"
        self.repo.git.checkout("-b", txn, base_sha)

        try:
            file_path = self._key_to_path(key)

            if delete:
                if not os.path.exists(file_path):
                    return True
                os.remove(file_path)
                self.repo.git.add("-A")
                msg = f"Delete key: {key}"
            else:
                parent_dir = os.path.dirname(file_path) or self.repo_path
                os.makedirs(parent_dir, exist_ok=True)
                with open(file_path, "w") as f:
                    f.write(value)
                self.repo.git.add(file_path)
                msg = f"Set key: {key}"

            if not self.repo.index.diff("HEAD"):
                return True

            self.repo.index.commit(msg)
            txn_sha = self.repo.head.commit.hexsha

            origin = self.repo.remote(name=self.remote_name)
            try:
                push_info = list(
                    origin.push(f"{txn_sha}:refs/heads/{self.branch_name}")
                )
            except GitCommandError as e:
                if self._is_non_ff(e):
                    return False
                raise GitKVError(f"Push failed: {e}") from e

            if not self._push_succeeded(push_info):
                return False
            return True
        finally:
            try:
                self.repo.git.reset("--hard")
            except GitCommandError:
                pass
            try:
                self.repo.git.checkout(self.branch_name)
            except GitCommandError:
                pass
            try:
                self.repo.git.branch("-D", txn)
            except GitCommandError:
                pass

    def _retry_write(self, key, value, delete):
        last_err = None
        for attempt in range(self.max_retries):
            try:
                if self._try_write_once(key, value, delete=delete):
                    return
            except GitKVError as e:
                last_err = e
            time.sleep(min(0.1 * (2 ** attempt), 2.0))
        if last_err is not None:
            raise last_err
        action = "delete" if delete else "set"
        raise GitKVError(
            f"Failed to {action} key {key!r} after {self.max_retries} attempts "
            f"due to repeated non-fast-forward rejections"
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def get(self, key):
        with self._lock():
            self._sync_local_to_remote()
            file_path = self._key_to_path(key)
            if not os.path.exists(file_path):
                return None
            with open(file_path, "r") as f:
                return f.read()

    def set(self, key, value):
        if value is None:
            raise GitKVError("set() value must not be None; use delete() instead")
        with self._lock():
            self._retry_write(key, value, delete=False)

    def delete(self, key):
        with self._lock():
            self._retry_write(key, None, delete=True)
