# Git-KV-store: Design

This document describes the target architecture for the Git-backed key-value store. It is the result of a design discussion and supersedes the behaviour of the current `Git_KV.py` implementation. Implementation will land in stages — see [Implementation Plan](#implementation-plan) at the end.

## Goals

- A key-value store whose source of truth is a Git repository on a remote host.
- Multiple concurrent client writers, coordinated via Git's existing primitives (no external lock service).
- Bounded history growth — the repository must remain usable after millions of writes.
- Multi-tenancy: multiple logically independent KV namespaces ("tables") in one repo.
- No history rewriting — every `ref` update is either fast-forward or an atomic create. Force-push is never used by the protocol.

## Non-goals

- Cross-table atomic operations (joins, multi-key transactions across tables).
- High write throughput. Git's per-operation cost makes this unsuitable for high-frequency workloads. Target: human-scale config / state storage.
- Strict linearizability of reads. Reads observe the snapshot at the moment of fetch.

## Concurrency model (single table)

Every write builds on Git's atomic ref update as a compare-and-swap (CAS) primitive: a non-fast-forward push is rejected by the remote, which is exactly the failure semantics we need for optimistic concurrency.

### Write path

```
1. Acquire a process-level file lock (e.g. flock on .git/kv.lock)
   — protects the shared working tree, index, and HEAD against
   concurrent in-process operations.

2. Cheap probe: fetch the current active branch's tip commit only.
       git fetch --filter=tree:0 --depth=1 origin <active>
   — ~1 KB regardless of how far behind the client is.

3. Inspect the tip commit's message:
     - If it carries a Tombstone-Next-Branch trailer:
         follow the chain (see "Discovery" below) to find the new
         active branch, then restart step 2 with that branch.
     - Otherwise: this is the live branch. Proceed to step 4.

4. Full fetch of the now-known active branch.

5. Create a transaction branch from the active branch tip,
   apply the write on it, commit it.

6. Push the txn branch's tip into the active branch's ref,
   fast-forward only:
       git push origin <txn_sha>:refs/heads/<active>
   - Success → write committed.
   - Rejected (non-FF)  → goto step 2 (someone else pushed first
     or rotation happened). Retry with the latest state.

7. Release the lock.
```

The transaction-branch indirection isolates the write's commit from the active branch until the moment of the CAS push. Failure leaves no trace — the txn branch is local-only and can be discarded.

The `reset --hard HEAD^` pattern in the current implementation is removed entirely. It is incorrect under concurrent writes (can silently drop other un-pushed commits and wipes the working tree) and unnecessary once the txn-branch + ff-only CAS pattern is in place.

### Read path

Same cheap-probe-first pattern: confirm the cached active branch is still active (not a tombstone), then fetch enough of it to read the requested key. With `--filter=blob:none`, individual key reads can lazy-fetch only the blob they need.

## Multi-tenancy: tables via ref prefixes

Refs are grouped by prefix. Each prefix is an independent KV namespace ("table" / "DB instance"). Three classes of refs exist:

### Top-level registry: `main`

A single branch listing the known tables. The tree of `main` contains one entry per table:

```
tables/pm        (empty file initially; may hold metadata later)
tables/analytics (empty file)
tables/users     (empty file)
```

`main` receives one FF commit when a table is added or removed. The body of these files is reserved for future client-side metadata (for example, the expected commit sha of the corresponding `<prefix>_main`, used to defend against ref squatting).

### Per-table genesis anchor: `<prefix>_main`

For every table, a dedicated branch that holds **exactly one commit, forever**. This commit acts as a stable, immutable "DNS root" for that table.

- The commit is an orphan (no parents). Its tree is empty (or a tiny placeholder).
- Its commit message records the name of the first log branch:

  ```
  Genesis of table <prefix>

  Genesis-First-Log: refs/heads/<prefix>_log_<uuid>
  ```

- `<prefix>_main` is never updated. Adding archive support later would relax this invariant; until then, treat it as write-once.

The commit sha of `<prefix>_main` doubles as the **common ancestor of every log branch in that table**: every log branch's first commit lists `<prefix>_main`'s commit as its parent. This makes namespace membership verifiable with `git merge-base --is-ancestor`.

### Data refs: `<prefix>_log_<uuid>`

The actual KV data lives on a chain of log branches, one per "segment". A segment grows until rotation closes it.

- Each log branch's first commit has `parent = <prefix>_main`'s commit, `tree = snapshot of the data at branch start`.
- Subsequent commits on the branch are individual writes (one commit per `set` / `delete`).
- When closed, the branch's final commit is a **tombstone** (see below) that points to the new segment.
- Branch names are random UUIDs (16 hex chars suffix). Naming carries **no information** — clients never parse branch names. Generated UUIDs avoid the crash-recovery complications of a monotonic counter (no need to persist "next counter value"; every retry uses a fresh name).

## Tombstone protocol

Rotation is signalled entirely through commit message metadata on the closing branch's final commit. **Discovery never depends on branch name conventions.**

The tombstone commit's message uses git trailers:

```
KV log rotated: <closing branch> closed

Tombstone-Version: 1
Tombstone-Next-Branch: refs/heads/<new branch name>
Tombstone-Next-Sha: <new branch tip sha at rotation time>
Tombstone-Snapshot-Tree: <tree sha; equal to the tip tree of both branches>
Tombstone-Created-At: <ISO 8601 timestamp>
```

Field semantics:

| Field | Required | Purpose |
|---|---|---|
| `Tombstone-Version` | yes | Protocol version. Unknown versions: fail closed, never guess. |
| `Tombstone-Next-Branch` | yes | Full ref path of the next log branch. The **only** thing the chain walker uses for navigation. |
| `Tombstone-Next-Sha` | yes | Expected tip of the next branch at rotation time. Used by clients to verify the chain link is intact. |
| `Tombstone-Snapshot-Tree` | yes | Tree sha at close time. Must equal the tree of the next branch's first commit (data continuity invariant). |
| `Tombstone-Created-At` | optional | Diagnostics only. |

A `Tombstone-Generation` field (monotonic counter) was discussed but is **not** part of the protocol. Cycle and replay protection rely instead on a visited-set during chain traversal — sufficient for the current trust model.

### Why message-driven, not name-driven

Tombstone metadata is part of the commit object itself. With `git fetch --filter=tree:0 --depth=1`, a client can download a single commit's metadata without any tree or blob data — roughly ~1 KB regardless of branch size. This makes chain discovery O(chain length) in tiny round trips, instead of O(branch data) in megabytes.

Putting the marker in a file would force the client to download trees and blobs just to discover that the branch is closed.

## Discovery and chain walk

### From cold start (no client cache)

```
1. Fetch tip of `main`, read tables/<prefix> to confirm the table exists.
2. Fetch <prefix>_main (single commit; ~1 KB).
3. Parse its message for Genesis-First-Log → first log branch name.
4. Loop, starting with that branch name:
     a. git fetch --filter=tree:0 --depth=1 origin <branch>
     b. Parse the tip commit's message.
     c. If it has Tombstone-Next-Branch:
          - Validate: not already in visited set (cycle detection).
          - branch ← Tombstone-Next-Branch; goto a.
        Else: <branch> is the active branch. Stop.
5. Cache (table, active branch) locally for next time.
```

### From a warm cache

```
1. Probe the cached active branch's tip with the same cheap fetch.
2. If tip is not a tombstone → it's still active. Proceed.
3. If tip is a tombstone → walk the (usually short) chain forward
   from here; update cache.
```

Steady-state cost is 0–1 hop per operation. Multi-rotation catch-up after a long pause costs O(rotations missed) hops, but each hop is ~1 KB and the absolute count is small in practice.

### Discovery cost — known limitation

A cold-start client must walk the entire tombstone chain from `<prefix>_main` to the current active branch. Each hop is a single tip-commit fetch (~1 KB). For a long-lived table with many rotations, this is O(N) hops where N is the total number of rotations ever performed for that table.

This is not addressed in the initial implementation. Possible future mitigations:

- Client-side persistent cache of last-known active branch reduces typical-case to 0–1 hop.
- Periodic checkpoint commits (would require relaxing the immutability of `<prefix>_main` or introducing a separate checkpoint ref) could short-circuit the walk.

These are deferred until the chain length is shown to be a practical problem.

## Rotation

A rotation closes the current log branch and opens a new one. It uses git plumbing throughout — `commit-tree` and `push` — and does **not** touch the working tree, the index, or `HEAD`. There is no `git checkout`, no `cp`, no temporary directory.

The reason this works: a Git `tree` object is content-addressed and immutable. A new commit referencing the same tree sha is created in O(1) and shares all underlying blob storage with the original. "Copying the data into the new branch" is literally just `git commit-tree <existing tree sha> -p <genesis> -m ...`.

### Trigger

Default: **commit count on the active log branch > 10000**, evaluated after each successful write. Any client that observes the threshold being exceeded may initiate rotation. Concurrent attempts are resolved by the protocol below (one wins, the others detect a tombstone already exists and abort cleanly).

The threshold is a tunable default; future work may make it per-table configurable via the metadata file in `main`.

### Procedure

```
1. Acquire the per-table lock.

2. Fetch current state:
   git fetch --filter=tree:0 --depth=1 origin <current_log>
   current_tip  = rev-parse FETCH_HEAD
   current_tree = rev-parse FETCH_HEAD^{tree}    # tree sha from commit header

   git fetch --depth=1 origin <prefix>_main
   genesis = rev-parse FETCH_HEAD

3. Generate a fresh branch name:
   new_branch = "<prefix>_log_" + uuid4().hex[:16]

4. Create the new segment's first commit (no checkout, no IO):
   new_first = git commit-tree <current_tree> -p <genesis> -m "Open log segment"

5. Atomically create the new branch on the remote:
   git push origin <new_first>:refs/heads/<new_branch>
   (fails if the ref already exists — UUID collision is negligible.)

6. Build the tombstone commit (no checkout):
   tombstone = git commit-tree <current_tree> -p <current_tip> \
                 -F <(message with Tombstone-* trailers)>

7. FF-push the tombstone into the current log branch:
   git push origin <tombstone>:refs/heads/<current_log>
     - Success → rotation complete. Update local cache.
     - Rejected → another client pushed to <current_log> after step 2.
                  The branch created in step 5 is now orphaned; delete it:
                  git push origin :refs/heads/<new_branch>
                  Then retry the whole rotation with a new UUID.

8. Release the lock.
```

### Crash safety

UUID-named branches make every retry independent. Possible crash states:

| Crash point | Remote state | Effect | Recovery |
|---|---|---|---|
| Before step 5 | Unchanged | None | None needed |
| After 5, before 7 | Orphan `<prefix>_log_<uuid>` exists, no tombstone points to it | No client navigates to it | Next rotation uses a new UUID; orphan is cleaned by a periodic GC pass (any log branch not reachable from `<prefix>_main`'s tombstone chain after a grace period) |
| After 7, before local cache update | Rotation succeeded | Local cache is stale | Next operation's cheap probe detects the tombstone and walks the chain |

No state requires manual recovery. The protocol is crash-safe by construction.

## Storage layout summary

```
refs/heads/main
  Tree:
    tables/pm
    tables/analytics
    tables/users
    ...

refs/heads/pm_main
  Single commit (orphan), commit message:
    Genesis of table pm
    Genesis-First-Log: refs/heads/pm_log_<uuid_0>

refs/heads/pm_log_<uuid_0>
  First commit:    parent=pm_main, tree=initial snapshot
  Write commits:   each one ff-pushed by a set/delete
  ...
  Final commit:    tombstone with Tombstone-Next-Branch=pm_log_<uuid_1>

refs/heads/pm_log_<uuid_1>
  First commit:    parent=pm_main, tree=copy of pm_log_<uuid_0>'s tip tree
  ...

analytics_main, analytics_log_<...>, ... : same structure, independent chain
```

## Implementation plan

The design is delivered in two stages so each lands as an independently working artifact.

### Stage 1: hardened single-branch baseline

Refactor the existing `Git_KV.py` to remove the unsafe `reset --hard HEAD^` recovery path and replace it with the CAS pattern from the [Write path](#write-path) section. Scope:

- Process-level `flock` around every public operation.
- Transaction-branch + ff-only push CAS for both `set` and `delete`.
- Rebase + retry on non-FF rejection (no `reset --hard` anywhere).
- Single branch only — no tables, no tombstones, no rotation.

After Stage 1 the store has correct concurrency semantics on a single namespace.

### Stage 2: layered architecture + rotation

Add the multi-table structure, tombstone protocol, and auto-rotation described above. Scope:

- `main` / `<prefix>_main` / `<prefix>_log_<uuid>` three-tier ref layout.
- Cheap-probe + chain-walk discovery on the client.
- Auto rotation triggered at the default 10000-commit threshold.
- `commit-tree`-based rotation with no working-tree involvement.
- UUID branch naming.

After Stage 2 the store supports multi-table, automatically-bounded history.
