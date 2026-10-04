## 2024-05-18 - Caching IO-heavy operations in polling loops
**Learning:** Polling file systems (especially parsing JSONL logs and opening SQLite DBs per session) is a bottleneck. In `SessionDetector`, we repeatedly parse 64KB log chunks and open secondary SQLite files every poll interval. Caching the results based on the file's modification time (mtime) drastically reduces overhead (measured 65% reduction in execution time in a 30-session environment).
**Action:** When adding periodic file inspections, always cache the parsed result using `os.path.getmtime` or `.stat().st_mtime` to skip I/O if the file hasn't changed.
## 2024-05-18 - Avoid SQLite DB mtime caching and bounding caches
**Learning:** Caching SQLite queries using the main `.db` file's `mtime` is dangerous if WAL mode is used, as changes appear in the `-wal` file without modifying the main file's timestamp. Also, dictionaries used for caching sessions can grow unbounded.
**Action:** Do not use `mtime` to cache SQLite queries without checking the `-wal` file or better yet, just cache the transcript reading where I/O costs are huge (due to JSON parsing from a 64KB log). Use bounded dictionaries or clean up inactive IDs.
