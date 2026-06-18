# BEGIN IMMEDIATE for read-modify-write under WAL

**Problem.** SQLite (even in WAL mode) opens a write transaction in DEFERRED
mode by default: it takes a read lock first and only upgrades to a write lock at
the first write. Two connections that both `SELECT` the current state, decide a
transition, then `UPDATE` can both pass their reads and then collide — one gets
`SQLITE_BUSY` and rolls back, OR worse, a lost-update if the read-decision was
stale. Our state-machine writes (`set_state`, `persist_from_processing`,
`delete_job`) are all read-decide-write.

**Fix.** Open those transactions with `BEGIN IMMEDIATE` so the write lock is
acquired up front, before the read. A competing writer then blocks (and waits
out `busy_timeout`) instead of racing to a lost update. The reads inside the
transaction now see a state no other writer can change under us.

**Where.** `src/lcp/adapters/storage/job_store.py` (`set_state`,
`persist_from_processing`, the delete-row write). Marker file I/O stays OUTSIDE
the lock (PR #8 constraint) so a slow filesystem op never holds the WAL writer.

**Tell-tale.** Intermittent `database is locked` under concurrency, or a
transition that "shouldn't have been legal" landing — both point at a DEFERRED
read-decide-write that should be IMMEDIATE.
