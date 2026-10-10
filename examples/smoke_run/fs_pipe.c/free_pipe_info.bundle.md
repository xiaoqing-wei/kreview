# kbundle: `free_pipe_info()` in fs/pipe.c

<!-- kbundle-meta: {"file": "fs/pipe.c", "function": "free_pipe_info", "lines": 25, "start": 924, "end": 948, "risk": 7, "level": "medium", "model": "grok-4.7-high", "tokens": 1367} -->

| field | value |
|---|---|
| estimated tokens | ~1367 (budget 60000, ~4 chars/token) |
| function lines | 25 |
| risk score | 7 (medium) |
| recommended model | grok-4.7-high |
| resolvers | ctags, git grep |
| trimming | none |

Everything needed for review is in this bundle. Line numbers refer to fs/pipe.c.

## Target function: `free_pipe_info` (fs/pipe.c:924-948, 25 lines)

```c
   924  void free_pipe_info(struct pipe_inode_info *pipe)
   925  {
   926  	unsigned int i;
   927  
   928  #ifdef CONFIG_WATCH_QUEUE
   929  	if (pipe->watch_queue)
   930  		watch_queue_clear(pipe->watch_queue);
   931  #endif
   932  
   933  	(void) account_pipe_buffers(pipe->user, pipe->nr_accounted, 0);
   934  	free_uid(pipe->user);
   935  	for (i = 0; i < pipe->ring_size; i++) {
   936  		struct pipe_buffer *buf = pipe->bufs + i;
   937  		if (buf->ops)
   938  			pipe_buf_release(pipe, buf);
   939  	}
   940  #ifdef CONFIG_WATCH_QUEUE
   941  	if (pipe->watch_queue)
   942  		put_watch_queue(pipe->watch_queue);
   943  #endif
   944  	for (i = 0; i < pipe->prealloc.count; i++)
   945  		__free_page(pipe->prealloc.pages[i]);
   946  	kfree(pipe->bufs);
   947  	kfree(pipe);
   948  }
```

## Risk signals (auto-detected, heuristic)

**Score 7 → medium.** refcount +3; alloc_free +4

- **alloc_free**: `__free_page` (L945), `kfree` (L946,947)
- **refcount**: `put_watch_queue` (L942)

## Referenced types and macros (definitions via ctags)

### struct pipe_buffer  — include/linux/pipe_fs_i.h:29
```c
    29  struct pipe_buffer {
    30  	struct page *page;
    31  	unsigned int offset, len;
    32  	const struct pipe_buf_operations *ops;
    33  	unsigned int flags;
    34  	unsigned long private;
    35  };
```

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

### `__free_page` (called 1x) — include/linux/gfp.h:346
```c
   346  #define __free_page(page) __free_pages((page), 0)
```

### `account_pipe_buffers` (called 1x) — fs/pipe.c:850
```c
   850  unsigned long account_pipe_buffers(struct user_struct *user,
   851  				   unsigned long old, unsigned long new)
   852  {
   853  	return atomic_long_add_return(new - old, &user->pipe_bufs);
   854  }
```

- `free_uid` (called 1x) — prototype only: `include/linux/sched/user.h:55  extern void free_uid(struct user_struct *);`

### `pipe_buf_release` (called 1x) — include/linux/pipe_fs_i.h:285
```c
   285  static inline void pipe_buf_release(struct pipe_inode_info *pipe,
   286  				    struct pipe_buffer *buf)
   287  {
   288  	const struct pipe_buf_operations *ops = buf->ops;
   289  
   290  	buf->ops = NULL;
   291  	ops->release(pipe, buf);
   292  }
```

- `put_watch_queue` (called 1x) — prototype only: `include/linux/watch_queue.h:84  extern void put_watch_queue(struct watch_queue *);`

- `watch_queue_clear` (called 1x) — prototype only: `include/linux/watch_queue.h:92  extern void watch_queue_clear(struct watch_queue *);`

## Callers / references (call-site context, capped)

### fs/pipe.c:804 in `put_pipe_info()` — call
```c
   801  	spin_unlock(&inode->i_lock);
   802  
   803  	if (kill)
   804> 		free_pipe_info(pipe);
   805  }
   806  
   807  static int
```

### fs/pipe.c:1019 in `create_pipe_files()` — call
```c
  1016  	if (flags & O_NOTIFICATION_PIPE) {
  1017  		error = watch_queue_init(inode->i_pipe);
  1018  		if (error) {
  1019> 			free_pipe_info(inode->i_pipe);
  1020  			iput(inode);
  1021  			return error;
  1022  		}
```

### fs/pipe.c:1029 in `create_pipe_files()` — call
```c
  1026  				O_WRONLY | (flags & (O_NONBLOCK | O_DIRECT)),
  1027  				&pipeanon_fops);
  1028  	if (IS_ERR(f)) {
  1029> 		free_pipe_info(inode->i_pipe);
  1030  		iput(inode);
  1031  		return PTR_ERR(f);
  1032  	}
```

### fs/pipe.c:1225 in `fifo_open()` — call
```c
  1222  		if (unlikely(inode->i_pipe)) {
  1223  			inode->i_pipe->files++;
  1224  			spin_unlock(&inode->i_lock);
  1225> 			free_pipe_info(pipe);
  1226  			pipe = inode->i_pipe;
  1227  		} else {
  1228  			inode->i_pipe = pipe;
```
