"""Git-backed key-value store.

Quick start:

    import gitkv

    db = gitkv.open()                       # uses GITKV_REPO / config file
    db.create_table("pm")

    db["pm"]["user/alice"] = "alice@example.com"
    db["pm"]["user/alice"]                  # → "alice@example.com"
    "user/alice" in db["pm"]                # → True

    "pm" in db                              # → True
    list(db)                                # → ["pm"]

The lower-level explicit API is still available via `GitKVStore` and the
`store.table(prefix).set / get / delete` methods.
"""

from gitkv._store import (
    DEFAULT_MAX_CAS_ATTEMPTS,
    DEFAULT_ROTATION_THRESHOLD,
    ChainBrokenError,
    CycleDetectedError,
    GitKVError,
    GitKVStore,
    GitKVTable,
    TableAlreadyExistsError,
    TableNotFoundError,
)


def open(repo_path=None, *, rotation_threshold=None):
    """Open a `GitKVStore`, resolving the repo path via the same cascade the
    CLI uses (explicit arg → GITKV_REPO env var → config file).

    Raises `GitKVError` if no repo path can be resolved.
    """
    # Local imports keep `import gitkv` cheap when callers don't need config.
    from gitkv._config import (
        Config,
        resolve_repo,
        resolve_rotation_threshold,
    )
    cfg = Config.from_file()
    resolved = resolve_repo(repo_path, cfg)
    if not resolved:
        raise GitKVError(
            "No repo configured. Pass repo_path=..., export GITKV_REPO=..., "
            "or run `gitkv config set repo PATH`."
        )
    threshold = resolve_rotation_threshold(rotation_threshold, cfg)
    return GitKVStore(resolved, rotation_threshold=threshold)


__all__ = [
    "DEFAULT_MAX_CAS_ATTEMPTS",
    "DEFAULT_ROTATION_THRESHOLD",
    "ChainBrokenError",
    "CycleDetectedError",
    "GitKVError",
    "GitKVStore",
    "GitKVTable",
    "TableAlreadyExistsError",
    "TableNotFoundError",
    "open",
]

__version__ = "0.4.0"
