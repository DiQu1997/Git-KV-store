# gitkv

A Git-backed key-value store. Every write is a commit; concurrency is
handled by Git's fast-forward push semantics (compare-and-swap). Multi-table
layout, tombstone-based log rotation, partial-clone friendly.

See `DESIGN.md` for the full architecture.

## Install

```bash
pip install git+https://github.com/DiQu1997/Git-KV-store
```

(Not yet published to PyPI.)

## Quick start

You need a Git remote you can push to, and a local clone of it.

```bash
git clone git@github.com:me/my-kv-store.git ~/kv

# One-time setup: tell gitkv where the repo is and which table to default to
gitkv config set repo ~/kv
gitkv config set table pm

# Daily use — no flags needed
gitkv create-table pm
gitkv set user/alice "alice@example.com"
gitkv get user/alice
# alice@example.com
gitkv list-tables
# pm
```

You can also override the defaults per command:

```bash
gitkv --repo /elsewhere set foo bar      # different repo, one-off
gitkv set -t analytics event/42 click    # different table, one-off
GITKV_REPO=/elsewhere gitkv list-tables  # via env var
```

See `gitkv config --help` (`config set`/`get`/`list`/`unset`/`path`) and
`gitkv config list` to see which value comes from which source.

As a Python library:

```python
import gitkv

db = gitkv.open()                         # uses GITKV_REPO / config file
# db = gitkv.open("/path/to/clone")       # or explicit

db.create_table("pm")

# Dict-style API (recommended)
db["pm"]["user/alice/email"] = "alice@example.com"
db["pm"]["user/alice/email"]              # → "alice@example.com"
"user/alice/email" in db["pm"]            # → True
del db["pm"]["user/alice/email"]

"pm" in db                                # → True
list(db)                                  # → ["pm"]
len(db)                                   # → 1
```

Listing keys and items, with path-prefix filter and pagination:

```python
table = db["pm"]

table.list_keys()                         # every key, sorted
table.list_keys(prefix="user/")           # only keys under user/
table.list_keys(prefix="user/alice")      # only user/alice/*
table.list_keys(limit=100)                # first 100
table.list_keys(limit=100, after=last)    # next page

table.list_items(prefix="user/alice")     # [(key, value), ...]
list(table)                               # same as table.list_keys()
```

Prefix matching is segment-strict — `prefix="user"` does **not** match `users/...`. Trailing slash is optional.

The lower-level explicit API is still there and is the one to use when you
want `.get()` to return `None` on a miss instead of raising `KeyError`:

```python
from gitkv import GitKVStore

store = GitKVStore("/path/to/clone")
store.table("pm").set("user/alice", "alice@example.com")
store.table("pm").get("missing")          # → None (does not raise)
```

## How the data is laid out

Each key becomes a Git blob at a path matching the key. `user/alice/email`
is a file at `user/alice/email` on the active log branch
(`<prefix>_log_<hex>`). Browse it on GitHub by switching to that branch.

## Development

```bash
pip install -e ".[dev]"
pytest
```
