"""Git-backed key-value store with multi-table layout, tombstone-based
log rotation, and CAS concurrency over Git's fast-forward push semantics.

See DESIGN.md for the architecture. This file implements Stage 2:

- GitKVStore: top-level handle for a Git repository hosting multiple
  independent KV tables. Manages the table registry on the `main` branch.

- GitKVTable: one KV namespace ("table"). All ops go through Git plumbing
  (hash-object, mktree, commit-tree) — the working tree, index, and HEAD
  are never touched.

- Tombstone-based log rotation: when the active log branch exceeds the
  rotation threshold, a new log segment is opened and the closing branch
  receives a final tombstone commit whose message points to the new
  branch. Clients walk the tombstone chain to find the current active
  branch; chain navigation never depends on branch naming.
"""

import fcntl
import os
import random
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


DEFAULT_ROTATION_THRESHOLD = 10000
DEFAULT_MAX_CAS_ATTEMPTS = 20
ZERO_SHA = "0" * 40


def _backoff_sleep(attempt):
    """Exponential backoff with full jitter, capped at 2s. The jitter avoids
    multiple racing writers from waking simultaneously and re-colliding."""
    upper = min(0.05 * (2 ** attempt), 2.0)
    time.sleep(random.uniform(0, upper))
TOMBSTONE_VERSION = "1"
GENESIS_VERSION = "1"

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_RESERVED_PREFIXES = frozenset({"main", "tables"})


class GitKVError(Exception):
    """Raised when a KV operation fails permanently."""


class TableNotFoundError(GitKVError):
    pass


class TableAlreadyExistsError(GitKVError):
    pass


class ChainBrokenError(GitKVError):
    pass


class CycleDetectedError(GitKVError):
    pass


class SyncDivergedError(GitKVError):
    """Local and remote histories have both moved on the same branch.

    gitkv never auto-merges: reconcile manually with git (e.g. rebase the
    local branch onto the remote one), then retry pull()/push()."""


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

def _run_git(repo_path, args, input_bytes=None, check=True):
    """Run a git subprocess, returning (returncode, stdout_bytes, stderr_bytes)."""
    proc = subprocess.run(
        ["git", "-C", repo_path] + list(args),
        input=input_bytes,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise GitKVError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.returncode, proc.stdout, proc.stderr


def _is_non_ff(stderr_text):
    s = stderr_text.lower()
    return (
        "non-fast-forward" in s
        or "fetch first" in s
        or "updates were rejected" in s
        or "cannot lock ref" in s  # receive-pack's atomic ref-update failure
        or "stale info" in s
        or ("rejected" in s and "would clobber" in s)
    )


def _parse_trailer(message, key):
    """Read a single `Key: value` trailer line out of a commit message."""
    target = key + ":"
    for line in message.splitlines():
        line = line.rstrip()
        if line.startswith(target):
            return line[len(target):].strip()
    return None


def _ref_to_branch(ref):
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return ref


def _validate_prefix(prefix):
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        raise GitKVError(
            f"Invalid table prefix {prefix!r}: must match {_PREFIX_RE.pattern}"
        )
    if prefix in _RESERVED_PREFIXES:
        raise GitKVError(f"Table prefix {prefix!r} is reserved")


def _validate_key(key):
    if not isinstance(key, str) or not key:
        raise GitKVError(f"Invalid key (must be non-empty string): {key!r}")
    if key.startswith("/") or key.startswith(os.sep):
        raise GitKVError(f"Invalid key (must be relative): {key!r}")
    parts = key.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise GitKVError(f"Invalid key (empty or '.'/'..' segment): {key!r}")
    if parts[0] == ".git":
        raise GitKVError(f"Invalid key (refers to .git): {key!r}")
    return parts


def _normalize_path_prefix(prefix):
    """Normalize a list_keys/list_items prefix to canonical form.

    Empty / None / "/" all mean "no restriction". Backslashes are folded to
    forward slashes for parity with `_validate_key`. Returns the trimmed
    prefix or "" for the root.
    """
    if prefix is None or prefix == "":
        return ""
    if not isinstance(prefix, str):
        raise GitKVError(f"Invalid prefix (must be string): {prefix!r}")
    normalized = prefix.replace("\\", "/")
    # Allow a lone "/" (= root, equivalent to "") but reject any other
    # leading-slash form, matching `_validate_key`'s rejection of absolute keys.
    if normalized == "/":
        return ""
    if normalized.startswith("/"):
        raise GitKVError(f"Invalid prefix (must be relative): {prefix!r}")
    trimmed = normalized.rstrip("/")
    if not trimmed:
        return ""
    parts = trimmed.split("/")
    if any(seg in ("", ".", "..") for seg in parts):
        raise GitKVError(f"Invalid prefix (empty or '.'/'..' segment): {prefix!r}")
    if parts[0] == ".git":
        raise GitKVError(f"Invalid prefix (refers to .git): {prefix!r}")
    return trimmed


def _apply_pagination(keys, after, limit):
    """Drop everything <= `after`, then take the first `limit`."""
    if after is not None:
        if not isinstance(after, str):
            raise GitKVError(f"`after` must be a string, got {type(after).__name__}")
        from bisect import bisect_right
        keys = keys[bisect_right(keys, after):]
    if limit is not None:
        if not isinstance(limit, int) or limit < 0:
            raise GitKVError(f"`limit` must be a non-negative int, got {limit!r}")
        keys = keys[:limit]
    return keys


# ---------------------------------------------------------------------------
# GitKVStore — top-level handle
# ---------------------------------------------------------------------------

class GitKVStore:
    def __init__(
        self,
        repo_path,
        remote_name="origin",
        rotation_threshold=DEFAULT_ROTATION_THRESHOLD,
        max_cas_attempts=DEFAULT_MAX_CAS_ATTEMPTS,
        offline=False,
    ):
        self.repo_path = repo_path
        self.remote_name = remote_name
        self.rotation_threshold = rotation_threshold
        self.max_cas_attempts = max_cas_attempts
        # Offline mode: every op reads and writes local branch heads
        # (refs/heads/*) and never touches the network. Local heads are
        # bootstrapped from remote-tracking refs on first use. Publish /
        # refresh happens only through explicit pull()/push()/sync().
        # Offline is effectively single-writer: concurrent remote writes
        # surface as SyncDivergedError at sync time, never auto-merged.
        self.offline = offline

        rc, out, err = _run_git(repo_path, ["rev-parse", "--git-dir"], check=False)
        if rc != 0:
            raise GitKVError(
                f"Not a git repository: {repo_path}: {err.decode(errors='replace').strip()}"
            )
        git_dir = out.decode().strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(repo_path, git_dir)
        self.git_dir = git_dir

        # Active-branch cache shared across GitKVTable instances on this store.
        # prefix -> (branch_name, tip_sha)
        self._active_cache = {}

    # ----- subprocess wrappers -----

    def _git(self, *args):
        _, out, _ = _run_git(self.repo_path, list(args))
        return out.decode()

    def _git_raw(self, *args, input_bytes=None):
        _, out, _ = _run_git(self.repo_path, list(args), input_bytes=input_bytes)
        return out

    def _git_safe(self, *args, input_bytes=None):
        """Return (returncode, stdout_bytes, stderr_text)."""
        rc, out, err = _run_git(
            self.repo_path, list(args), input_bytes=input_bytes, check=False
        )
        return rc, out, err.decode(errors="replace")

    # ----- locking -----

    @contextmanager
    def _lock(self, name):
        lock_dir = os.path.join(self.git_dir, "kv-locks")
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, f"{name}.lock")
        f = open(lock_path, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()

    # ----- ref / fetch helpers -----
    #
    # Every data op reads a branch tip and (for writes) CAS-updates it.
    # Online, "read" means fetch from the remote into a remote-tracking ref
    # and "update" means git push (the server's fast-forward check is the
    # CAS). Offline, "read" means the local head (bootstrapped once from the
    # remote-tracking ref) and "update" means `git update-ref` with an
    # expected old value (the local CAS). All call sites go through
    # `_branch_ref` / `_refresh_*` / `_cas_update_branch` so they never need
    # to know which mode they're in.

    def _rev_parse(self, rev):
        return self._git("rev-parse", rev).strip()

    def _local_ref_exists(self, ref):
        rc, _, _ = self._git_safe("rev-parse", "--verify", "--quiet", ref)
        return rc == 0

    def _ensure_local_head(self, branch):
        """Bootstrap refs/heads/<branch> from the remote-tracking ref if it
        doesn't exist yet. Returns True if the head exists afterwards."""
        head = f"refs/heads/{branch}"
        if self._local_ref_exists(head):
            return True
        remote = f"refs/remotes/{self.remote_name}/{branch}"
        if self._local_ref_exists(remote):
            self._git("update-ref", head, self._rev_parse(remote))
            return True
        return False

    def _branch_ref(self, branch):
        """The ref data ops read a branch tip from, per mode."""
        if self.offline:
            return f"refs/heads/{branch}"
        return self._remote_ref(branch)

    def _ref_exists_remote(self, ref):
        rc, out, _ = self._git_safe("ls-remote", "--exit-code", self.remote_name, ref)
        return rc == 0 and bool(out.strip())

    def _table_ref_exists(self, branch):
        """Mode-aware existence check for a table's branch."""
        if self.offline:
            return self._ensure_local_head(branch)
        return self._ref_exists_remote(f"refs/heads/{branch}")

    def _fetch_with_filter(self, branch, filter_spec):
        """Refresh our view of `branch`'s tip. Online: fetch with the given
        partial-clone filter (fall back to plain --depth=1 if the remote
        refuses filters). Offline: no network — just make sure the local
        head exists."""
        if self.offline:
            if not self._ensure_local_head(branch):
                raise GitKVError(
                    f"Branch {branch!r} not available locally; run pull() first"
                )
            return
        rc, _, err = self._git_safe(
            "fetch", f"--filter={filter_spec}", "--depth=1",
            self.remote_name, branch,
        )
        if rc == 0:
            return
        if "filter" in err.lower() or "uploadpack" in err.lower():
            self._git("fetch", "--depth=1", self.remote_name, branch)
            return
        raise GitKVError(f"fetch failed for {branch}: {err.strip()}")

    def _fetch_tip_only(self, branch):
        """Cheap probe: fetch only the tip commit object (no tree, no blobs).
        Used for tombstone detection during chain walk."""
        self._fetch_with_filter(branch, "tree:0")

    def _fetch_tree_only(self, branch):
        """Fetch commit + trees but no blobs. Used by write paths that need to
        introspect the existing tree structure but don't need to read any
        existing key's value."""
        self._fetch_with_filter(branch, "blob:none")

    def _fetch_full(self, branch):
        if self.offline:
            if not self._ensure_local_head(branch):
                raise GitKVError(
                    f"Branch {branch!r} not available locally; run pull() first"
                )
            return
        self._git("fetch", self.remote_name, branch)

    def _remote_ref(self, branch):
        return f"refs/remotes/{self.remote_name}/{branch}"

    # ----- ref publication (mode-aware CAS) -----

    def _cas_update_branch(self, branch, new_sha, old_sha=None):
        """Move `branch` to `new_sha`, failing if someone else moved it first.

        old_sha None means "create: the branch must not exist yet".
        Online, the CAS is the server's fast-forward check (new_sha's parent
        chain must contain the remote tip). Offline, it's update-ref's
        expected-old-value check against `old_sha`.

        Returns (rc, stderr_text); a CAS loss satisfies `_is_non_ff`.
        """
        if self.offline:
            rc, _, err = self._git_safe(
                "update-ref", f"refs/heads/{branch}", new_sha, old_sha or ZERO_SHA
            )
            return rc, err
        rc, _, err = self._git_safe(
            "push", self.remote_name, f"{new_sha}:refs/heads/{branch}"
        )
        return rc, err

    def _publish_new_refs(self, pairs):
        """Atomically create several new branches. `pairs` is [(sha, branch)].
        Returns (rc, stderr_text)."""
        if self.offline:
            script = "".join(f"create refs/heads/{b} {sha}\n" for sha, b in pairs)
            rc, _, err = self._git_safe(
                "update-ref", "--stdin", input_bytes=script.encode()
            )
            return rc, err
        refspecs = [f"{sha}:refs/heads/{b}" for sha, b in pairs]
        rc, _, err = self._git_safe("push", "--atomic", self.remote_name, *refspecs)
        return rc, err

    def _delete_branch(self, branch):
        """Best-effort deletion of a branch we just created (rotate cleanup)."""
        if self.offline:
            self._git_safe("update-ref", "-d", f"refs/heads/{branch}")
        else:
            self._git_safe("push", self.remote_name, f":refs/heads/{branch}")

    def _commit_message(self, sha):
        rc, out, err = self._git_safe("cat-file", "-p", sha)
        if rc != 0:
            raise GitKVError(f"cat-file {sha} failed: {err.strip()}")
        text = out.decode(errors="replace")
        # commit object format: headers, blank line, then message
        if "\n\n" in text:
            return text.split("\n\n", 1)[1]
        return text

    def _commit_number(self, sha):
        """Read the Commit-Number trailer from a commit, or 0 if absent.

        Used to bound history without needing the full chain locally — each
        commit records its own ordinal, so the tip alone tells us how many
        commits the current log segment holds.
        """
        msg = self._commit_message(sha)
        val = _parse_trailer(msg, "Commit-Number")
        if val is None:
            return 0
        try:
            return int(val)
        except ValueError:
            return 0

    # ----- object construction (pure plumbing) -----

    def _empty_tree_sha(self):
        return self._git_raw("mktree", input_bytes=b"").decode().strip()

    def _hash_object(self, content):
        if isinstance(content, str):
            content = content.encode()
        out = self._git_raw("hash-object", "-w", "--stdin", input_bytes=content)
        return out.decode().strip()

    def _ls_tree(self, tree_sha):
        """Return list of (mode, type, sha, name) for entries in `tree_sha`."""
        if not tree_sha:
            return []
        rc, out, err = self._git_safe("ls-tree", tree_sha)
        if rc != 0:
            raise GitKVError(f"ls-tree {tree_sha}: {err.strip()}")
        result = []
        for line in out.decode().split("\n"):
            if not line:
                continue
            meta, name = line.split("\t", 1)
            mode, typ, sha = meta.split()
            result.append((mode, typ, sha, name))
        return result

    def _mktree(self, entries):
        if not entries:
            return self._empty_tree_sha()

        def sort_key(e):
            _, typ, _, name = e
            return name + "/" if typ == "tree" else name

        sorted_entries = sorted(entries, key=sort_key)
        body = "".join(
            f"{mode} {typ} {sha}\t{name}\n" for mode, typ, sha, name in sorted_entries
        )
        # --missing lets us reference blob shas we don't have locally — the
        # remote will already have them, and we never need to materialize the
        # bytes ourselves during a write.
        return self._git_raw(
            "mktree", "--missing", input_bytes=body.encode()
        ).decode().strip()

    def _commit_tree(self, tree_sha, parents, message):
        args = ["commit-tree", tree_sha]
        for p in parents:
            args += ["-p", p]
        args += ["-F", "-"]
        return self._git_raw(*args, input_bytes=message.encode()).decode().strip()

    # ----- tree manipulation (recursive for nested keys) -----

    def _update_tree(self, tree_sha, path_parts, value_sha, value_type="blob"):
        if not path_parts:
            raise ValueError("path_parts must be non-empty")
        head, *tail = path_parts
        entries = self._ls_tree(tree_sha)
        new_entries = []
        replaced = False
        for mode, typ, sha, name in entries:
            if name == head:
                replaced = True
                if not tail:
                    if value_type == "blob":
                        new_entries.append(("100644", "blob", value_sha, name))
                    else:
                        new_entries.append(("040000", "tree", value_sha, name))
                else:
                    if typ != "tree":
                        raise GitKVError(
                            f"path collision: {head!r} exists as {typ}, not a directory"
                        )
                    new_sub = self._update_tree(sha, tail, value_sha, value_type)
                    if new_sub == sha:
                        return tree_sha
                    new_entries.append(("040000", "tree", new_sub, name))
            else:
                new_entries.append((mode, typ, sha, name))

        if not replaced:
            if not tail:
                if value_type == "blob":
                    new_entries.append(("100644", "blob", value_sha, head))
                else:
                    new_entries.append(("040000", "tree", value_sha, head))
            else:
                sub_sha = self._build_path(tail, value_sha, value_type)
                new_entries.append(("040000", "tree", sub_sha, head))

        return self._mktree(new_entries)

    def _remove_from_tree(self, tree_sha, path_parts):
        if not path_parts:
            raise ValueError("path_parts must be non-empty")
        head, *tail = path_parts
        entries = self._ls_tree(tree_sha)
        new_entries = []
        removed = False
        for mode, typ, sha, name in entries:
            if name == head:
                if not tail:
                    removed = True
                    continue
                if typ != "tree":
                    new_entries.append((mode, typ, sha, name))
                    continue
                new_sub = self._remove_from_tree(sha, tail)
                if new_sub == sha:
                    new_entries.append((mode, typ, sha, name))
                else:
                    removed = True
                    if not self._is_empty_tree(new_sub):
                        new_entries.append(("040000", "tree", new_sub, name))
            else:
                new_entries.append((mode, typ, sha, name))

        if not removed:
            return tree_sha
        if not new_entries:
            return self._empty_tree_sha()
        return self._mktree(new_entries)

    def _build_path(self, path_parts, leaf_sha, leaf_type):
        if not path_parts:
            raise ValueError
        if len(path_parts) == 1:
            if leaf_type == "blob":
                entries = [("100644", "blob", leaf_sha, path_parts[0])]
            else:
                entries = [("040000", "tree", leaf_sha, path_parts[0])]
        else:
            sub = self._build_path(path_parts[1:], leaf_sha, leaf_type)
            entries = [("040000", "tree", sub, path_parts[0])]
        return self._mktree(entries)

    def _is_empty_tree(self, tree_sha):
        return not self._ls_tree(tree_sha)

    # ----- registry (top-level `main` branch) -----

    def list_tables(self):
        if self.offline:
            if not self._ensure_local_head("main"):
                return []
        else:
            rc, _, _ = self._git_safe("fetch", self.remote_name, "main")
            if rc != 0:
                return []
        try:
            tip = self._rev_parse(self._branch_ref("main"))
            tree = self._rev_parse(f"{tip}^{{tree}}")
        except GitKVError:
            return []
        rc, out, _ = self._git_safe("ls-tree", tree, "tables/")
        if rc != 0:
            return []
        prefixes = []
        for line in out.decode().split("\n"):
            if not line:
                continue
            _, name = line.split("\t", 1)
            if name.startswith("tables/") and "/" in name:
                prefixes.append(name.split("/", 1)[1])
        return prefixes

    def create_table(self, prefix):
        _validate_prefix(prefix)
        if self._table_ref_exists(f"{prefix}_main"):
            raise TableAlreadyExistsError(f"Table {prefix!r} already exists")

        first_log = f"{prefix}_log_{uuid.uuid4().hex[:16]}"
        empty_tree = self._empty_tree_sha()

        genesis_msg = (
            f"Genesis of table {prefix}\n"
            f"\n"
            f"Genesis-Version: {GENESIS_VERSION}\n"
            f"Genesis-First-Log: refs/heads/{first_log}\n"
        )
        genesis_sha = self._commit_tree(empty_tree, [], genesis_msg)

        first_log_sha = self._commit_tree(
            empty_tree,
            [genesis_sha],
            f"Open log segment for {prefix}\n\nCommit-Number: 1\n",
        )

        rc, err = self._publish_new_refs([
            (genesis_sha, f"{prefix}_main"),
            (first_log_sha, first_log),
        ])
        if rc != 0:
            raise GitKVError(
                f"Failed to publish genesis refs for table {prefix!r}: {err.strip()}"
            )

        self._register_table_in_main(prefix)
        self._active_cache[prefix] = (first_log, first_log_sha)

    def _register_table_in_main(self, prefix):
        empty_blob = self._hash_object(b"")
        for attempt in range(5):
            base_sha = None
            if self.offline:
                if self._ensure_local_head("main"):
                    base_sha = self._rev_parse("refs/heads/main")
            else:
                rc, _, _ = self._git_safe("fetch", self.remote_name, "main")
                if rc == 0:
                    base_sha = self._rev_parse(self._remote_ref("main"))

            if base_sha:
                base_tree = self._rev_parse(f"{base_sha}^{{tree}}")
                parents = [base_sha]
            else:
                base_tree = self._empty_tree_sha()
                parents = []

            new_tree = self._update_tree(
                base_tree, ["tables", prefix], empty_blob, "blob"
            )
            if new_tree == base_tree:
                return

            commit = self._commit_tree(new_tree, parents, f"Register table: {prefix}")
            rc, err = self._cas_update_branch("main", commit, old_sha=base_sha)
            if rc == 0:
                return
            if _is_non_ff(err):
                _backoff_sleep(attempt)
                continue
            raise GitKVError(f"Failed to register table: {err.strip()}")
        raise GitKVError(f"Failed to register table {prefix!r} after retries")

    def table(self, prefix):
        _validate_prefix(prefix)
        return GitKVTable(self, prefix)

    # ----- explicit sync (pull / push / status) -----
    #
    # These always talk to the network, regardless of mode. They are the only
    # way offline work reaches the remote (and vice versa). Divergence — both
    # sides moved on the same branch — is never auto-merged: pull() and
    # push() raise SyncDivergedError and leave refs untouched, and the user
    # reconciles with plain git.

    def _list_branch_names(self, ref_prefix):
        rc, out, _ = self._git_safe(
            "for-each-ref", "--format=%(refname)", ref_prefix
        )
        if rc != 0:
            return set()
        names = set()
        for line in out.decode().splitlines():
            if not line:
                continue
            name = line[len(ref_prefix):].lstrip("/")
            if name and name != "HEAD":
                names.add(name)
        return names

    def _fetch_all(self):
        """Update every remote-tracking ref. Unshallows first if some online
        op left the repo shallow — sync needs real ancestry to reason about."""
        shallow = self._git("rev-parse", "--is-shallow-repository").strip()
        args = ["fetch"]
        if shallow == "true":
            args.append("--unshallow")
        args += [self.remote_name,
                 f"+refs/heads/*:refs/remotes/{self.remote_name}/*"]
        self._git(*args)

    def status(self, fetch=True):
        """Compare local heads against the remote, per branch.

        Returns a dict:
          ahead       {branch: n}  local has n commits the remote lacks
          behind      {branch: n}  remote has n commits local lacks
          diverged    [branch]     both sides moved — needs manual reconcile
          local_only  [branch]     branch exists only locally (will push)
          remote_only [branch]     branch exists only on the remote (will pull)

        All five empty means fully in sync.
        """
        if fetch:
            self._fetch_all()
        result = {
            "ahead": {}, "behind": {},
            "diverged": [], "local_only": [], "remote_only": [],
        }
        local = self._list_branch_names("refs/heads")
        remote = self._list_branch_names(f"refs/remotes/{self.remote_name}")
        for branch in sorted(local | remote):
            if branch not in remote:
                result["local_only"].append(branch)
                continue
            if branch not in local:
                result["remote_only"].append(branch)
                continue
            local_sha = self._rev_parse(f"refs/heads/{branch}")
            remote_sha = self._rev_parse(self._remote_ref(branch))
            if local_sha == remote_sha:
                continue
            rc, out, _ = self._git_safe(
                "rev-list", "--left-right", "--count",
                f"refs/heads/{branch}...{self._remote_ref(branch)}",
            )
            if rc != 0:
                # Can't establish ancestry (e.g. shallow gaps) — treat as
                # diverged so we refuse rather than guess.
                result["diverged"].append(branch)
                continue
            ahead, behind = (int(x) for x in out.decode().split())
            if ahead and behind:
                result["diverged"].append(branch)
            elif ahead:
                result["ahead"][branch] = ahead
            elif behind:
                result["behind"][branch] = behind
        return result

    def pull(self):
        """Fetch the remote and fast-forward local heads.

        Creates local heads for remote-only branches, fast-forwards heads the
        remote is ahead of, and leaves locally-ahead branches alone (push()
        publishes those). Raises SyncDivergedError — without touching any
        local head — if any branch has moved on both sides.
        """
        st = self.status(fetch=True)
        if st["diverged"]:
            raise SyncDivergedError(
                f"Cannot pull: local and remote histories diverged on: "
                f"{', '.join(st['diverged'])}. Reconcile manually with git "
                f"(e.g. rebase refs/heads/<branch> onto the remote), then retry."
            )
        updated, created = [], []
        for branch in st["behind"]:
            self._git(
                "update-ref", f"refs/heads/{branch}",
                self._rev_parse(self._remote_ref(branch)),
            )
            updated.append(branch)
        for branch in st["remote_only"]:
            self._git(
                "update-ref", f"refs/heads/{branch}",
                self._rev_parse(self._remote_ref(branch)),
            )
            created.append(branch)
        # Tips moved under the cached values.
        self._active_cache.clear()
        return {
            "created": created,
            "updated": updated,
            "ahead": dict(st["ahead"]),
            "local_only": list(st["local_only"]),
        }

    def push(self):
        """Publish locally-ahead branches to the remote.

        Pushes every branch where local is strictly ahead (or exists only
        locally). Never forces: if the remote moved since the last fetch the
        push is rejected and SyncDivergedError is raised — run pull() and
        retry. Branches where only the remote moved are left for pull().
        """
        st = self.status(fetch=True)
        if st["diverged"]:
            raise SyncDivergedError(
                f"Cannot push: local and remote histories diverged on: "
                f"{', '.join(st['diverged'])}. Reconcile manually with git, "
                f"then retry."
            )
        branches = list(st["ahead"]) + st["local_only"]
        if not branches:
            return {"pushed": []}
        refspecs = [f"refs/heads/{b}:refs/heads/{b}" for b in branches]
        rc, _, err = self._git_safe(
            "push", "--atomic", self.remote_name, *refspecs
        )
        if rc != 0:
            if _is_non_ff(err):
                raise SyncDivergedError(
                    f"Push rejected — the remote moved since the last fetch: "
                    f"{err.strip()}. Run pull() and retry."
                )
            raise GitKVError(f"Push failed: {err.strip()}")
        return {"pushed": branches}

    def sync(self):
        """pull() then push(). The everyday 'flush my offline work up and
        grab anything new' operation."""
        pulled = self.pull()
        pushed = self.push()
        return {"pull": pulled, "push": pushed}

    # ----- dict-style ergonomics -----
    #
    # These wrap the explicit methods so callers can write idiomatic Python:
    #
    #     store["pm"]                  # → GitKVTable
    #     "pm" in store                # → bool
    #     list(store)                  # → ["pm", "analytics", ...]
    #
    # No existence check on __getitem__ — it would cost an extra fetch on
    # every access and operations on the returned table already surface
    # TableNotFoundError. Use `prefix in store` to check explicitly.

    def __getitem__(self, prefix):
        return self.table(prefix)

    def __contains__(self, prefix):
        return prefix in self.list_tables()

    def __iter__(self):
        return iter(self.list_tables())

    def __len__(self):
        return len(self.list_tables())


# ---------------------------------------------------------------------------
# GitKVTable — one namespace
# ---------------------------------------------------------------------------

class GitKVTable:
    def __init__(self, store, prefix):
        _validate_prefix(prefix)
        self.store = store
        self.prefix = prefix

    # ----- chain discovery -----

    def _genesis_branch(self):
        return f"{self.prefix}_main"

    def _genesis_sha(self):
        branch = self._genesis_branch()
        if self.store.offline:
            if not self.store._ensure_local_head(branch):
                raise TableNotFoundError(
                    f"Table {self.prefix!r} does not exist locally; "
                    f"run pull() if it lives on the remote"
                )
        else:
            rc, _, err = self.store._git_safe(
                "fetch", "--depth=1", self.store.remote_name, branch
            )
            if rc != 0:
                raise TableNotFoundError(
                    f"Table {self.prefix!r} does not exist: {err.strip()}"
                )
        return self.store._rev_parse(self.store._branch_ref(branch))

    def _genesis_first_log(self):
        sha = self._genesis_sha()
        msg = self.store._commit_message(sha)
        first_log = _parse_trailer(msg, "Genesis-First-Log")
        if not first_log:
            raise ChainBrokenError(
                f"{self._genesis_branch()} commit missing Genesis-First-Log"
            )
        return _ref_to_branch(first_log)

    def _find_active(self):
        """Return (active_branch_name, active_tip_sha), using cache when possible."""
        cache = self.store._active_cache.get(self.prefix)
        if cache:
            try:
                return self._walk_from(cache[0])
            except (ChainBrokenError, GitKVError):
                self.store._active_cache.pop(self.prefix, None)
        start = self._genesis_first_log()
        return self._walk_from(start)

    def _walk_from(self, start_branch):
        visited = set()
        current = start_branch
        while True:
            if current in visited:
                raise CycleDetectedError(
                    f"Cycle in tombstone chain at {current}: visited={sorted(visited)}"
                )
            visited.add(current)

            self.store._fetch_tip_only(current)
            tip = self.store._rev_parse(self.store._branch_ref(current))
            msg = self.store._commit_message(tip)

            next_ref = _parse_trailer(msg, "Tombstone-Next-Branch")
            if not next_ref:
                self.store._active_cache[self.prefix] = (current, tip)
                return current, tip

            current = _ref_to_branch(next_ref)

    # ----- public ops -----

    def get(self, key):
        parts = _validate_key(key)
        with self.store._lock(self.prefix):
            active, _ = self._find_active()
            self.store._fetch_full(active)
            tip = self.store._rev_parse(self.store._branch_ref(active))
            tree = self.store._rev_parse(f"{tip}^{{tree}}")
            rc, out, _ = self.store._git_safe(
                "cat-file", "-p", f"{tree}:{'/'.join(parts)}"
            )
            if rc != 0:
                return None
            return out.decode()

    def set(self, key, value):
        if value is None:
            raise GitKVError("set() value must not be None; use delete()")
        parts = _validate_key(key)
        with self.store._lock(self.prefix):
            self._cas_write(parts, value, delete=False, msg=f"Set key: {key}")
            self._maybe_rotate()

    def delete(self, key):
        parts = _validate_key(key)
        with self.store._lock(self.prefix):
            self._cas_write(parts, None, delete=True, msg=f"Delete key: {key}")
            self._maybe_rotate()

    # ----- internal write path -----

    def _cas_write(self, parts, value, delete, msg, max_attempts=None):
        if max_attempts is None:
            max_attempts = self.store.max_cas_attempts
        last_err = None
        for attempt in range(max_attempts):
            active, tip = self._find_active()
            # The chain-walk fetch was tree:0 (commit object only). Before
            # introspecting the tree we need its objects locally — but not
            # blobs, since we never read existing values during a write.
            self.store._fetch_tree_only(active)
            tree = self.store._rev_parse(f"{tip}^{{tree}}")
            parent_number = self.store._commit_number(tip)

            if delete:
                new_tree = self.store._remove_from_tree(tree, parts)
            else:
                blob_sha = self.store._hash_object(value)
                new_tree = self.store._update_tree(tree, parts, blob_sha, "blob")

            if new_tree == tree:
                return

            full_msg = f"{msg}\n\nCommit-Number: {parent_number + 1}\n"
            new_commit = self.store._commit_tree(new_tree, [tip], full_msg)
            rc, err = self.store._cas_update_branch(active, new_commit, old_sha=tip)
            if rc == 0:
                self.store._active_cache[self.prefix] = (active, new_commit)
                return

            if _is_non_ff(err):
                last_err = err.strip()
                self.store._active_cache.pop(self.prefix, None)
                _backoff_sleep(attempt)
                continue
            raise GitKVError(f"Push failed: {err.strip()}")
        raise GitKVError(
            f"CAS write failed after {max_attempts} attempts: {last_err}"
        )

    # ----- rotation -----

    def _maybe_rotate(self):
        """Trigger rotation if the active branch's tip Commit-Number meets the
        threshold. Reads the count from the tip commit's message alone — no
        extra fetch needed, since we already have the tip after the push."""
        cache = self.store._active_cache.get(self.prefix)
        if not cache:
            return
        _, tip = cache
        if self.store._commit_number(tip) >= self.store.rotation_threshold:
            self.rotate()

    def rotate(self, max_attempts=None):
        """Close the current active log branch and open a new one. Idempotent
        on success; safe to retry on failure."""
        if max_attempts is None:
            max_attempts = self.store.max_cas_attempts
        last_err = None
        for attempt in range(max_attempts):
            active, tip = self._find_active()
            tree = self.store._rev_parse(f"{tip}^{{tree}}")
            genesis = self._genesis_sha()

            parent_number = self.store._commit_number(tip)
            new_branch = f"{self.prefix}_log_{uuid.uuid4().hex[:16]}"
            new_first_msg = (
                f"Open log segment for {self.prefix}\n"
                f"\n"
                f"Commit-Number: 1\n"
            )
            new_first = self.store._commit_tree(tree, [genesis], new_first_msg)

            rc, err = self.store._cas_update_branch(
                new_branch, new_first, old_sha=None
            )
            if rc != 0:
                # UUID collision is statistically impossible; treat as fatal.
                raise GitKVError(
                    f"Failed to create new log branch {new_branch}: {err.strip()}"
                )

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            tombstone_msg = (
                f"KV log rotated: {active} closed\n"
                f"\n"
                f"Tombstone-Version: {TOMBSTONE_VERSION}\n"
                f"Tombstone-Next-Branch: refs/heads/{new_branch}\n"
                f"Tombstone-Next-Sha: {new_first}\n"
                f"Tombstone-Snapshot-Tree: {tree}\n"
                f"Tombstone-Created-At: {now}\n"
                f"Commit-Number: {parent_number + 1}\n"
            )
            tombstone_sha = self.store._commit_tree(tree, [tip], tombstone_msg)

            rc, err = self.store._cas_update_branch(
                active, tombstone_sha, old_sha=tip
            )
            if rc == 0:
                self.store._active_cache[self.prefix] = (new_branch, new_first)
                return new_branch

            if _is_non_ff(err):
                # Someone else moved <active>. Our new_branch is now orphan.
                # Delete it and retry from scratch with a fresh UUID.
                self.store._delete_branch(new_branch)
                last_err = err.strip()
                self.store._active_cache.pop(self.prefix, None)
                _backoff_sleep(attempt)
                continue
            raise GitKVError(f"Tombstone push failed: {err.strip()}")
        raise GitKVError(f"Rotation failed after {max_attempts} attempts: {last_err}")

    # ----- dict-style ergonomics -----
    #
    # __getitem__ raises KeyError (not None) on a missing key, matching dict
    # semantics. The explicit `.get(key)` still returns None — callers who
    # want the "missing means None" behaviour keep using that.

    def __getitem__(self, key):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self.set(key, value)

    def __delitem__(self, key):
        if self.get(key) is None:
            raise KeyError(key)
        self.delete(key)

    def __contains__(self, key):
        return self.get(key) is not None

    def __iter__(self):
        return iter(self.list_keys())

    # ----- listing -----

    def list_keys(self, prefix="", limit=None, after=None):
        """Lexicographically sorted keys, optionally restricted to a path prefix.

        `prefix` is matched as a path: `"user/"` and `"user"` both restrict to
        keys whose first segment is exactly `user`. A prefix that resolves to a
        single blob (a key itself) returns that one key.

        Pagination is key-cursor style: pass the last returned key as `after`
        on the next call. Caller stops when `len(result) < limit`. Results
        across pages are best-effort if the table is being written concurrently.

        Reads tree objects only — no blob contents are fetched.
        """
        normalized = _normalize_path_prefix(prefix)
        with self.store._lock(self.prefix):
            active, _ = self._find_active()
            self.store._fetch_tree_only(active)
            tip = self.store._rev_parse(self.store._branch_ref(active))
            keys = self._collect_keys_under(tip, normalized)
        return _apply_pagination(keys, after, limit)

    def list_items(self, prefix="", limit=None, after=None):
        """Same as `list_keys`, but each entry is a `(key, value)` tuple.

        Triggers a one-time full fetch of the active branch's tip (blobs
        included), then reads each blob in the result page.
        """
        normalized = _normalize_path_prefix(prefix)
        with self.store._lock(self.prefix):
            active, _ = self._find_active()
            self.store._fetch_full(active)
            tip = self.store._rev_parse(self.store._branch_ref(active))
            keys = self._collect_keys_under(tip, normalized)
            page = _apply_pagination(keys, after, limit)
            return [(k, self._cat_blob(tip, k)) for k in page]

    # ----- listing helpers -----

    def _collect_keys_under(self, tip, normalized_prefix):
        """Return sorted keys at or under the given path. `normalized_prefix`
        has no leading/trailing slashes and is either empty (root) or a
        validated path."""
        if not normalized_prefix:
            rc, out, _ = self.store._git_safe(
                "ls-tree", "-r", "--name-only", tip
            )
            if rc != 0:
                return []
            return sorted(n for n in out.decode().splitlines() if n)

        path_ref = f"{tip}:{normalized_prefix}"
        rc, out, _ = self.store._git_safe("cat-file", "-t", path_ref)
        if rc != 0:
            return []
        obj_type = out.decode().strip()
        if obj_type == "blob":
            return [normalized_prefix]
        if obj_type != "tree":
            return []

        rc, out, _ = self.store._git_safe(
            "ls-tree", "-r", "--name-only", path_ref
        )
        if rc != 0:
            return []
        names = (n for n in out.decode().splitlines() if n)
        return sorted(f"{normalized_prefix}/{n}" for n in names)

    def _cat_blob(self, tip, key):
        rc, out, _ = self.store._git_safe("cat-file", "-p", f"{tip}:{key}")
        if rc != 0:
            raise GitKVError(f"Failed to read blob for key {key!r}")
        return out.decode()
