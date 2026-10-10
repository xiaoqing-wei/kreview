# kbundle: `pipe_lock()` in fs/pipe.c

<!-- kbundle-meta: {"file": "fs/pipe.c", "function": "pipe_lock", "lines": 5, "start": 88, "end": 92, "risk": 6, "level": "low", "model": "grok-4.7-high", "tokens": 1062} -->

| field | value |
|---|---|
| estimated tokens | ~1062 (budget 120000, ~4 chars/token) |
| function lines | 5 |
| risk score | 6 (low) |
| recommended model | grok-4.7-high |
| resolvers | ctags, git grep |
| trimming | none |

Everything needed for review is in this bundle. Line numbers refer to fs/pipe.c.

## Target function: `pipe_lock` (fs/pipe.c:88-92, 5 lines)

```c
    88  void pipe_lock(struct pipe_inode_info *pipe)
    89  {
    90  	if (pipe->files)
    91  		mutex_lock(&pipe->mutex);
    92  }
```

## Risk signals (auto-detected, heuristic)

**Score 6 → low.** locking +3; lock/unlock count mismatch (1 vs 0; conditional/early-exit paths?) +3

- **locking**: `mutex_lock` (L91)

## Referenced types and macros (definitions via ctags)

### struct pipe_inode_info  — include/linux/pipe_fs_i.h:102
```c
   102  struct pipe_inode_info {
   103  	struct mutex mutex;
   104  	wait_queue_head_t rd_wait, wr_wait;
   105  
   106  	union pipe_index;
   107  
   108  	unsigned int max_usage;
   109  	unsigned int ring_size;
   110  	unsigned int nr_accounted;
   111  	unsigned int readers;
   112  	unsigned int writers;
   113  	unsigned int files;
   114  	unsigned int r_counter;
   115  	unsigned int w_counter;
   116  	bool pseudo_edgetrigger;
   117  #ifdef CONFIG_WATCH_QUEUE
   118  	bool note_loss;
   119  #endif
   120  	struct anon_pipe_prealloc prealloc;
   121  	struct fasync_struct *fasync_readers;
   122  	struct fasync_struct *fasync_writers;
   123  	struct pipe_buffer *bufs;
   124  	struct user_struct *user;
   125  #ifdef CONFIG_WATCH_QUEUE
   126  	struct watch_queue *watch_queue;
   127  #endif
   128  };
```

### union pipe_index (embedded anonymously above) — include/linux/pipe_fs_i.h:56
```c
    56  union pipe_index {
    57  	unsigned long head_tail;
    58  	struct {
    59  		pipe_index_t head;
    60  		pipe_index_t tail;
    61  	};
    62  };
```

## Callees (signature + truncated body)

Well-known primitives (locking, alloc, RCU, printk, ...) are omitted; see risk signals.

## Callers / references (call-site context, capped)

_13 sites found; showing 8._

### fs/pipe.c:110 in `pipe_double_lock()` — call
```c
   107  	if (pipe1 > pipe2)
   108  		swap(pipe1, pipe2);
   109  
   110> 	pipe_lock(pipe1);
   111  	pipe_lock(pipe2);
   112  }
   113  
```

### fs/pipe.c:111 in `pipe_double_lock()` — call
```c
   108  		swap(pipe1, pipe2);
   109  
   110  	pipe_lock(pipe1);
   111> 	pipe_lock(pipe2);
   112  }
   113  
   114  static struct page *anon_pipe_prealloc_pop(struct anon_pipe_prealloc *prealloc)
```

### fs/pipe.c:1158 in `pipe_wait_readable()` — call
```c
  1155  {
  1156  	pipe_unlock(pipe);
  1157  	wait_event_interruptible(pipe->rd_wait, pipe_readable(pipe));
  1158> 	pipe_lock(pipe);
  1159  }
  1160  
  1161  void pipe_wait_writable(struct pipe_inode_info *pipe)
```

### fs/pipe.c:1165 in `pipe_wait_writable()` — call
```c
  1162  {
  1163  	pipe_unlock(pipe);
  1164  	wait_event_interruptible(pipe->wr_wait, pipe_writable(pipe));
  1165> 	pipe_lock(pipe);
  1166  }
  1167  
  1168  /*
```

### fs/pipe.c:1190 in `wait_for_partner()` — call
```c
  1187  		pipe_unlock(pipe);
  1188  		schedule();
  1189  		finish_wait(&pipe->rd_wait, &rdwait);
  1190> 		pipe_lock(pipe);
  1191  		if (signal_pending(current))
  1192  			break;
  1193  	}
```

### fs/splice.c:640 in `splice_from_pipe()` — call
```c
   637  		.u.file = out,
   638  	};
   639  
   640> 	pipe_lock(pipe);
   641  	ret = __splice_from_pipe(pipe, &sd, actor);
   642  	pipe_unlock(pipe);
   643  
```

### fs/splice.c:682 in `iter_file_splice_write()` — call
```c
   679  	if (unlikely(!array))
   680  		return -ENOMEM;
   681  
   682> 	pipe_lock(pipe);
   683  
   684  	splice_from_pipe_begin(&sd);
   685  	while (sd.total_len) {
```

### fs/splice.c:805 in `splice_to_socket()` — call
```c
   802  	size_t spliced = 0;
   803  	bool need_wakeup = false;
   804  
   805> 	pipe_lock(pipe);
   806  
   807  	while (len > 0) {
   808  		unsigned int head, tail, bc = 0;
```
