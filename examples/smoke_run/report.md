# kreview report

Generated 2026-10-09T15:41:58+08:00 from `examples/smoke_run` (3 function result(s), 0 failed/skipped).

| severity | count |
|---|---|
| critical | 2 |

**Usage:** 3 agent call(s) (grok-4.7-high×3), 0 kreview cache hit(s), 4.2 min wall time. CLI-reported tokens: 59812 input + 12928 cache-read, 14072 output (incl. reasoning).

_Billed usage is authoritative only in the Cursor dashboard._

## Findings (sorted by severity, deduplicated)

| # | sev | conf | location | function | category | summary |
|---|---|---|---|---|---|---|
| 1 | critical | 0.98 | `fs/pipe.c:800` | put_pipe_info | locking | When --pipe->files hits zero, the function returns while inode->i_lock (taken at line 796) is still held; spin_unlock at line 802 is skipped. Every final put... |
| 2 | critical | 0.93 | `fs/pipe.c:799` | put_pipe_info | locking | free_pipe_info() now runs under inode->i_lock. It calls watch_queue_clear, pipe_buf_release, free_uid, __free_page, and kfree, which can sleep or take sleepi... |

## Details

### 1. [critical] fs/pipe.c:800 `put_pipe_info()` — locking (confidence 0.98)

When --pipe->files hits zero, the function returns while inode->i_lock (taken at line 796) is still held; spin_unlock at line 802 is skipped. Every final put (pipe_release, fifo_open error, create_pipe_files failure) leaks the spinlock and later i_lock users deadlock.

_sources: put_pipe_info()@grok-4.7-high_

### 2. [critical] fs/pipe.c:799 `put_pipe_info()` — locking (confidence 0.93)

free_pipe_info() now runs under inode->i_lock. It calls watch_queue_clear, pipe_buf_release, free_uid, __free_page, and kfree, which can sleep or take sleeping locks. The pre-patch code only set a kill flag under the lock and freed after spin_unlock.

_sources: put_pipe_info()@grok-4.7-high_

## Reviewed with no findings

`fs/pipe.c:free_pipe_info`, `fs/pipe.c:pipe_lock`
