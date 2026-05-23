"""Git-backed key-value store.

Public API:

    from gitkv import GitKVStore

    store = GitKVStore("/path/to/local/clone")
    store.create_table("pm")
    store.table("pm").set("user/alice", "alice@example.com")
    store.table("pm").get("user/alice")
"""

from gitkv._store import (
    DEFAULT_MAX_CAS_ATTEMPTS,
    DEFAULT_ROTATION_THRESHOLD,
    ChainBrokenError,
    CycleDetectedError,
    GitKVError,
    GitKVStore,
    TableAlreadyExistsError,
    TableNotFoundError,
)

__all__ = [
    "DEFAULT_MAX_CAS_ATTEMPTS",
    "DEFAULT_ROTATION_THRESHOLD",
    "ChainBrokenError",
    "CycleDetectedError",
    "GitKVError",
    "GitKVStore",
    "TableAlreadyExistsError",
    "TableNotFoundError",
]

__version__ = "0.1.0"
