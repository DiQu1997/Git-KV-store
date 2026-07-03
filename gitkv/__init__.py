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
    SyncDivergedError,
    TableAlreadyExistsError,
    TableNotFoundError,
)


def open(repo_path=None, *, rotation_threshold=None, offline=None):
    """Open a `GitKVStore`, resolving the repo path and mode via the same
    cascade the CLI uses (explicit arg → env var → config file).

    offline=True gives a store whose every op is local-only; exchange with
    the remote happens through store.pull() / store.push() / store.sync().
    offline=None (default) resolves GITKV_MODE / the config `mode` key,
    falling back to online.

    Raises `GitKVError` if no repo path can be resolved.
    """
    # Local imports keep `import gitkv` cheap when callers don't need config.
    from gitkv._config import (
        Config,
        resolve_mode,
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
    if offline is None:
        mode = resolve_mode(None, cfg)
    else:
        mode = "offline" if offline else "online"
    return GitKVStore(
        resolved,
        rotation_threshold=threshold,
        offline=(mode == "offline"),
    )


__all__ = [
    "DEFAULT_MAX_CAS_ATTEMPTS",
    "DEFAULT_ROTATION_THRESHOLD",
    "ChainBrokenError",
    "CycleDetectedError",
    "GitKVError",
    "GitKVStore",
    "GitKVTable",
    "SyncDivergedError",
    "TableAlreadyExistsError",
    "TableNotFoundError",
    "open",
]

__version__ = "0.5.0"
