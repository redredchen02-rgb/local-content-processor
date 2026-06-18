# Atomic file write = temp-in-same-dir + fsync + os.replace

**Problem.** Writing directly to the destination file (`open(path, "w")` then
write) leaves a window where a crash/SIGKILL mid-write yields a half-written,
corrupt file. For the manifest and review packet that corruption is
indistinguishable from valid output and silently poisons later stages.

**Fix.** Write to a temp file in the SAME directory, `flush()` + `os.fsync()` it,
then `os.replace(tmp, path)`. `os.replace` is atomic on POSIX, so a reader ever
sees either the old file or the fully-written new one — never a torn file. Same
directory matters: `os.replace` across filesystems is not atomic. Clean up the
temp file in a `finally`.

**Where.** `src/lcp/adapters/storage/manifest.py::_atomic_write` is the
reference; the same shape recurs in `draft_store.py`, `config_io.py`,
`review_packet.py`, `media_checker.py`, `job_store.py`.

**Related, but distinct.** The audit log additionally fsyncs the PARENT
DIRECTORY fd (`_fsync_dir`) — fsyncing the file persists its data, but the
directory entry that makes the new tail line part of the file is only durable
once the dir is fsynced. A truncated audit log reads as *tampered*, so that
extra step is load-bearing there.

**Tell-tale.** A "corrupt JSON" / "truncated manifest" report after an unclean
shutdown, or a half-written config — a non-atomic write got interrupted.
