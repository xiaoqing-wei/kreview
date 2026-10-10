# kbundle: `put_pipe_info()` in fs/pipe.c

<!-- kbundle-meta: {"file": "fs/pipe.c", "function": "put_pipe_info", "lines": 12, "start": 792, "end": 803, "risk": 9, "level": "medium", "model": "grok-4.7-high", "tokens": 1260} -->

| field | value |
|---|---|
| estimated tokens | ~1260 (budget 60000, ~4 chars/token) |
| function lines | 12 |
| risk score | 9 (medium) |
| recommended model | grok-4.7-high |
| resolvers | ctags, git grep |
| trimming | none |

Everything needed for review is in this bundle. Line numbers refer to fs/pipe.c.

## Target function: `put_pipe_info` (fs/pipe.c:792-803, 12 lines)

```c
   792  static void put_pipe_info(struct inode *inode, struct pipe_inode_info *pipe)
   793  {
   794  	int kill = 0;
   795  
   796  	spin_lock(&inode->i_lock);
   797  	if (!--pipe->files) {
   798  		inode->i_pipe = NULL;
   799  		free_pipe_info(pipe);
   800  		return;
   801  	}
   802  	spin_unlock(&inode->i_lock);
   803  }
```

## Risk signals (auto-detected, heuristic)

**Score 9 → medium.** refcount +3; locking +6

- **locking**: `spin_lock` (L796), `spin_unlock` (L802)
- **refcount**: `put_pipe_info` (L792)

## Patch under review

```diff
diff --git a/fs/pipe.c b/fs/pipe.c
index 292425a..2db2d62 100644
--- a/fs/pipe.c
+++ b/fs/pipe.c
@@ -794,16 +794,14 @@ static void put_pipe_info(struct inode *inode, struct pipe_inode_info *pipe)
 	int kill = 0;
 
 	spin_lock(&inode->i_lock);
 	if (!--pipe->files) {
 		inode->i_pipe = NULL;
-		kill = 1;
+		free_pipe_info(pipe);
+		return;
 	}
 	spin_unlock(&inode->i_lock);
-
-	if (kill)
-		free_pipe_info(pipe);
 }
 
 static int
 pipe_release(struct inode *inode, struct file *file)
 {

```

## Referenced types and macros (definitions via ctags)

### struct inode  — include/linux/fs.h:762 (large: showing lines with fields used by target)
```c
   762  struct inode {
   763  	umode_t			i_mode;
   764  	unsigned short		i_opflags;
        ...
   803  	spinlock_t		i_lock;	/* i_blocks, i_bytes, maybe i_size */
        ...
   844  	atomic_t		i_readcount; /* struct files open RO */
        ...
   857  		struct pipe_inode_info	*i_pipe;
        ...
   871  } __randomize_layout;
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

### `free_pipe_info` (called 1x) — fs/pipe.c:922
```c
   922  void free_pipe_info(struct pipe_inode_info *pipe)
   923  {
   924  	unsigned int i;
   925  
   926  #ifdef CONFIG_WATCH_QUEUE
   927  	if (pipe->watch_queue)
   928  		watch_queue_clear(pipe->watch_queue);
   929  #endif
   930  
   931  	(void) account_pipe_buffers(pipe->user, pipe->nr_accounted, 0);
   932  	free_uid(pipe->user);
   933  	for (i = 0; i < pipe->ring_size; i++) {
   934  		struct pipe_buffer *buf = pipe->bufs + i;
   935  		if (buf->ops)
   936  			pipe_buf_release(pipe, buf);
   937  	}
   938  #ifdef CONFIG_WATCH_QUEUE
   939  	if (pipe->watch_queue)
   940  		put_watch_queue(pipe->watch_queue);
   941  #endif
   942  	for (i = 0; i < pipe->prealloc.count; i++)
   943  		__free_page(pipe->prealloc.pages[i]);
   944  	kfree(pipe->bufs);
   945  	kfree(pipe);
   946  }
```

## Callers / references (call-site context, capped)

### fs/pipe.c:825 in `pipe_release()` — call
```c
   822  	}
   823  	mutex_unlock(&pipe->mutex);
   824  
   825> 	put_pipe_info(inode, pipe);
   826  	return 0;
   827  }
   828  
```

### fs/pipe.c:1038 in `create_pipe_files()` — call
```c
  1035  	res[0] = alloc_file_clone(f, O_RDONLY | (flags & O_NONBLOCK),
  1036  				  &pipeanon_fops);
  1037  	if (IS_ERR(res[0])) {
  1038> 		put_pipe_info(inode, inode->i_pipe);
  1039  		fput(f);
  1040  		return PTR_ERR(res[0]);
  1041  	}
```

### fs/pipe.c:1321 in `fifo_open()` — call
```c
  1318  err:
  1319  	mutex_unlock(&pipe->mutex);
  1320  
  1321> 	put_pipe_info(inode, pipe);
  1322  	return ret;
  1323  }
  1324  
```
