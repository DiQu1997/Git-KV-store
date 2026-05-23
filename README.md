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
from gitkv import GitKVStore

store = GitKVStore("/home/me/kv")
store.create_table("pm")
store.table("pm").set("user/alice", "alice@example.com")
store.table("pm").get("user/alice")
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
