# kbundle: `anon_pipe_read()` in fs/pipe.c

<!-- kbundle-meta: {"file": "fs/pipe.c", "function": "anon_pipe_read", "lines": 138, "start": 343, "end": 480, "risk": 27, "level": "high", "model": "grok-4.7-xhigh", "tokens": 4538} -->

| field | value |
|---|---|
| estimated tokens | ~4538 (budget 120000, ~4 chars/token) |
| function lines | 138 |
| risk score | 27 (high) |
| recommended model | grok-4.7-xhigh |
| resolvers | ctags, git grep |
| trimming | none |

Everything needed for review is in this bundle. Line numbers refer to fs/pipe.c.

## Target function: `anon_pipe_read` (fs/pipe.c:343-480, 138 lines)

```c
   343  static ssize_t
   344  anon_pipe_read(struct kiocb *iocb, struct iov_iter *to)
   345  {
   346  	size_t total_len = iov_iter_count(to);
   347  	struct file *filp = iocb->ki_filp;
   348  	struct pipe_inode_info *pipe = filp->private_data;
   349  	bool wake_writer = false, wake_next_reader = false;
   350  	ssize_t ret;
   351  
   352  	/* Null read succeeds. */
   353  	if (unlikely(total_len == 0))
   354  		return 0;
   355  
   356  	ret = 0;
   357  	mutex_lock(&pipe->mutex);
   358  
   359  	/*
   360  	 * We only wake up writers if the pipe was full when we started reading
   361  	 * and it is no longer full after reading to avoid unnecessary wakeups.
   362  	 *
   363  	 * But when we do wake up writers, we do so using a sync wakeup
   364  	 * (WF_SYNC), because we want them to get going and generate more
   365  	 * data for us.
   366  	 */
   367  	for (;;) {
   368  		/* Read ->head with a barrier vs post_one_notification() */
   369  		unsigned int head = smp_load_acquire(&pipe->head);
   370  		unsigned int tail = pipe->tail;
   371  
   372  #ifdef CONFIG_WATCH_QUEUE
   373  		if (pipe->note_loss) {
   374  			struct watch_notification n;
   375  
   376  			if (total_len < 8) {
   377  				if (ret == 0)
   378  					ret = -ENOBUFS;
   379  				break;
   380  			}
   381  
   382  			n.type = WATCH_TYPE_META;
   383  			n.subtype = WATCH_META_LOSS_NOTIFICATION;
   384  			n.info = watch_sizeof(n);
   385  			if (copy_to_iter(&n, sizeof(n), to) != sizeof(n)) {
   386  				if (ret == 0)
   387  					ret = -EFAULT;
   388  				break;
   389  			}
   390  			ret += sizeof(n);
   391  			total_len -= sizeof(n);
   392  			pipe->note_loss = false;
   393  		}
   394  #endif
   395  
   396  		if (!pipe_empty(head, tail)) {
   397  			struct pipe_buffer *buf = pipe_buf(pipe, tail);
   398  			size_t chars = buf->len;
   399  			size_t written;
   400  			int error;
   401  
   402  			if (chars > total_len) {
   403  				if (buf->flags & PIPE_BUF_FLAG_WHOLE) {
   404  					if (ret == 0)
   405  						ret = -ENOBUFS;
   406  					break;
   407  				}
   408  				chars = total_len;
   409  			}
   410  
   411  			error = pipe_buf_confirm(pipe, buf);
   412  			if (error) {
   413  				if (!ret)
   414  					ret = error;
   415  				break;
   416  			}
   417  
   418  			written = copy_page_to_iter(buf->page, buf->offset, chars, to);
   419  			if (unlikely(written < chars)) {
   420  				if (!ret)
   421  					ret = -EFAULT;
   422  				break;
   423  			}
   424  			ret += chars;
   425  			buf->offset += chars;
   426  			buf->len -= chars;
   427  
   428  			/* Was it a packet buffer? Clean up and exit */
   429  			if (buf->flags & PIPE_BUF_FLAG_PACKET) {
   430  				total_len = chars;
   431  				buf->len = 0;
   432  			}
   433  
   434  			if (!buf->len) {
   435  				wake_writer |= pipe_full(head, tail, pipe->max_usage);
   436  				tail = pipe_update_tail(pipe, buf, tail);
   437  			}
   438  			total_len -= chars;
   439  			if (!total_len)
   440  				break;	/* common path: read succeeded */
   441  			if (!pipe_empty(head, tail))	/* More to do? */
   442  				continue;
   443  		}
   444  
   445  		if (!pipe->writers)
   446  			break;
   447  		if (ret)
   448  			break;
   449  		if ((filp->f_flags & O_NONBLOCK) ||
   450  		    (iocb->ki_flags & IOCB_NOWAIT)) {
   451  			ret = -EAGAIN;
   452  			break;
   453  		}
   454  		mutex_unlock(&pipe->mutex);
   455  		/*
   456  		 * We only get here if we didn't actually read anything.
   457  		 *
   458  		 * But because we didn't read anything, at this point we can
   459  		 * just return directly with -ERESTARTSYS if we're interrupted,
   460  		 * since we've done any required wakeups and there's no need
   461  		 * to mark anything accessed. And we've dropped the lock.
   462  		 */
   463  		if (wait_event_interruptible_exclusive(pipe->rd_wait, pipe_readable(pipe)) < 0)
   464  			return -ERESTARTSYS;
   465  
   466  		wake_next_reader = true;
   467  		mutex_lock(&pipe->mutex);
   468  	}
   469  	if (pipe_is_empty(pipe))
   470  		wake_next_reader = false;
   471  	/* Consumed buffers may have refilled the pool; trim it and unlock. */
   472  	anon_pipe_trim_and_unlock(pipe);
   473  
   474  	if (wake_writer)
   475  		wake_up_interruptible_sync_poll(&pipe->wr_wait, EPOLLOUT | EPOLLWRNORM);
   476  	if (wake_next_reader)
   477  		wake_up_interruptible_sync_poll(&pipe->rd_wait, EPOLLIN | EPOLLRDNORM);
   478  	kill_fasync(&pipe->fasync_writers, SIGIO, POLL_OUT);
   479  	return ret;
   480  }
```

## Risk signals (auto-detected, heuristic)

**Score 27 → high.** user_memory +10; locking +6; barriers +4; lock/unlock count mismatch (2 vs 1; conditional/early-exit paths?) +3; length >100 lines +4

- **locking**: `mutex_lock` (L357,467), `mutex_unlock` (L454)
- **barriers**: `smp_load_acquire` (L369)
- **user_memory**: `copy_page_to_iter` (L418), `copy_to_iter` (L385), `iov_iter_count` (L346)

## Referenced types and macros (definitions via ctags)

### struct file  — include/linux/fs.h:1255
```c
  1255  struct file {
  1256  	spinlock_t			f_lock;
  1257  	fmode_t				f_mode;
  1258  	const struct file_operations	*f_op;
  1259  	struct address_space		*f_mapping;
  1260  	void				*private_data;
  1261  	struct inode			*f_inode;
  1262  	unsigned int			f_flags;
  1263  	unsigned int			f_iocb_flags;
  1264  	const struct cred		*f_cred;
  1265  	struct fown_struct		*f_owner;
  1266  	/* --- cacheline 1 boundary (64 bytes) --- */
  1267  	union {
  1268  		const struct path	f_path;
  1269  		struct path		__f_path;
  1270  	};
  1271  	union {
  1272  		/* regular files (with FMODE_ATOMIC_POS) and directories */
  1273  		struct mutex		f_pos_lock;
  1274  		/* pipes */
  1275  		u64			f_pipe;
  1276  	};
  1277  	loff_t				f_pos;
  1278  #ifdef CONFIG_SECURITY
  1279  	void				*f_security;
  1280  #endif
  1281  	/* --- cacheline 2 boundary (128 bytes) --- */
  1282  	errseq_t			f_wb_err;
  1283  	errseq_t			f_sb_err;
  1284  #ifdef CONFIG_EPOLL
  1285  	struct hlist_head		*f_ep;
  1286  #endif
  1287  	union {
  1288  		struct callback_head	f_task_work;
  1289  		struct llist_node	f_llist;
  1290  		struct file_ra_state	f_ra;
  1291  		freeptr_t		f_freeptr;
  1292  	};
  1293  	file_ref_t			f_ref;
  1294  	/* --- cacheline 3 boundary (192 bytes) --- */
  1295  } __randomize_layout
```

### struct iov_iter  — include/linux/uio.h:43
```c
    43  struct iov_iter {
    44  	u8 iter_type;
    45  	bool nofault;
    46  	bool data_source;
    47  	size_t iov_offset;
    48  	/*
    49  	 * Hack alert: overlay ubuf_iovec with iovec + count, so
    50  	 * that the members resolve correctly regardless of the type
    51  	 * of iterator used. This means that you can use:
    52  	 *
    53  	 * &iter->__ubuf_iovec or iter->__iov
    54  	 *
    55  	 * interchangably for the user_backed cases, hence simplifying
    56  	 * some of the cases that need to deal with both.
    57  	 */
    58  	union {
    59  		/*
    60  		 * This really should be a const, but we cannot do that without
    61  		 * also modifying any of the zero-filling iter init functions.
    62  		 * Leave it non-const for now, but it should be treated as such.
    63  		 */
    64  		struct iovec __ubuf_iovec;
    65  		struct {
    66  			union {
    67  				/* use iter_iov() to get the current vec */
    68  				const struct iovec *__iov;
    69  				const struct kvec *kvec;
    70  				const struct bio_vec *bvec;
    71  				const struct folio_queue *folioq;
    72  				struct xarray *xarray;
    73  				void __user *ubuf;
    74  			};
    75  			size_t count;
    76  		};
    77  	};
    78  	union {
    79  		unsigned long nr_segs;
    80  		u8 folioq_slot;
    81  		loff_t xarray_start;
    82  	};
    83  };
```

### struct kiocb  — include/linux/fs.h:379
```c
   379  struct kiocb {
   380  	struct file		*ki_filp;
   381  	loff_t			ki_pos;
   382  	void (*ki_complete)(struct kiocb *iocb, long ret);
   383  	void			*private;
   384  	int			ki_flags;
   385  	u16			ki_ioprio; /* See linux/ioprio.h */
   386  	u8			ki_write_stream;
   387  
   388  	/*
   389  	 * Only used for async buffered reads, where it denotes the page
   390  	 * waitqueue associated with completing the read.
   391  	 * Valid IFF IOCB_WAITQ is set.
   392  	 */
   393  	struct wait_page_queue	*ki_waitq;
   394  };
```

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

### struct watch_notification  — include/uapi/linux/watch_queue.h:29
```c
    29  struct watch_notification {
    30  	__u32			type:24;	/* enum watch_notification_type */
    31  	__u32			subtype:8;	/* Type-specific subtype (filterable) */
    32  	__u32			info;
    33  #define WATCH_INFO_LENGTH	0x0000007f	/* Length of record */
    34  #define WATCH_INFO_LENGTH__SHIFT 0
    35  #define WATCH_INFO_ID		0x0000ff00	/* ID of watchpoint */
    36  #define WATCH_INFO_ID__SHIFT	8
    37  #define WATCH_INFO_TYPE_INFO	0xffff0000	/* Type-specific info */
    38  #define WATCH_INFO_TYPE_INFO__SHIFT 16
    39  #define WATCH_INFO_FLAG_0	0x00010000	/* Type-specific info, flag bit 0 */
    40  #define WATCH_INFO_FLAG_1	0x00020000	/* ... */
    41  #define WATCH_INFO_FLAG_2	0x00040000
    42  #define WATCH_INFO_FLAG_3	0x00080000
    43  #define WATCH_INFO_FLAG_4	0x00100000
    44  #define WATCH_INFO_FLAG_5	0x00200000
    45  #define WATCH_INFO_FLAG_6	0x00400000
    46  #define WATCH_INFO_FLAG_7	0x00800000
    47  };
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

### macro `EPOLLIN` — include/uapi/linux/eventpoll.h:31
```c
    31  #define EPOLLIN		(__force __poll_t)0x00000001
```

### macro `EPOLLOUT` — include/uapi/linux/eventpoll.h:33
```c
    33  #define EPOLLOUT	(__force __poll_t)0x00000004
```

### macro `EPOLLRDNORM` — include/uapi/linux/eventpoll.h:37
```c
    37  #define EPOLLRDNORM	(__force __poll_t)0x00000040
```

### macro `EPOLLWRNORM` — include/uapi/linux/eventpoll.h:39
```c
    39  #define EPOLLWRNORM	(__force __poll_t)0x00000100
```

### macro `ERESTARTSYS` — include/linux/errno.h:14
```c
    14  #define ERESTARTSYS	512
```

### macro `IOCB_NOWAIT` — include/linux/fs.h:342
```c
   342  #define IOCB_NOWAIT		(__force int) RWF_NOWAIT
```

### macro `PIPE_BUF_FLAG_PACKET` — include/linux/pipe_fs_i.h:10
```c
    10  #define PIPE_BUF_FLAG_PACKET	0x08	/* read() as a packet */
```

### macro `PIPE_BUF_FLAG_WHOLE` — include/linux/pipe_fs_i.h:12
```c
    12  #define PIPE_BUF_FLAG_WHOLE	0x20	/* read() must return entire buffer or error */
```

### typedef `size_t` — include/linux/types.h:61
```c
    61  typedef __kernel_size_t		size_t;
```

### typedef `ssize_t` — include/linux/types.h:66
```c
    66  typedef __kernel_ssize_t	ssize_t;
```

## Callees (signature + truncated body)

Well-known primitives (locking, alloc, RCU, printk, ...) are omitted; see risk signals.

### `pipe_empty` (called 2x) — include/linux/pipe_fs_i.h:203
```c
   203  static inline bool pipe_empty(unsigned int head, unsigned int tail)
   204  {
   205  	return !pipe_occupancy(head, tail);
   206  }
```

### `wake_up_interruptible_sync_poll` (called 2x) — include/linux/wait.h:246
```c
   246  #define wake_up_interruptible_sync_poll(x, m)					\
   247  	__wake_up_sync_key((x), TASK_INTERRUPTIBLE, poll_to_key(m))
```

### `anon_pipe_trim_and_unlock` (called 1x) — fs/pipe.c:181
```c
   181  static void anon_pipe_trim_and_unlock(struct pipe_inode_info *pipe)
   182  {
   183  	struct page *excess[PIPE_PREALLOC_MAX];
   184  	unsigned int nexcess = 0;
   185  
   186  	while (pipe->prealloc.count > PIPE_PREALLOC_KEEP)
   187  		excess[nexcess++] = anon_pipe_prealloc_pop(&pipe->prealloc);
   188  	mutex_unlock(&pipe->mutex);
   189  
   190  	while (nexcess)
   191  		put_page(excess[--nexcess]);
   192  }
```

- `copy_page_to_iter` (called 1x) — prototype only: `include/linux/uio.h:190  size_t copy_page_to_iter(struct page *page, size_t offset, size_t bytes,`

### `copy_to_iter` (called 1x) — include/linux/uio.h:216
```c
   216  static __always_inline __must_check
   217  size_t copy_to_iter(const void *addr, size_t bytes, struct iov_iter *i)
   218  {
   219  	if (check_copy_size(addr, bytes, true))
   220  		return _copy_to_iter(addr, bytes, i);
   221  	return 0;
   222  }
```

### `iov_iter_count` (called 1x) — include/linux/uio.h:312
```c
   312  static inline size_t iov_iter_count(const struct iov_iter *i)
   313  {
   314  	return i->count;
   315  }
```

- `kill_fasync` (called 1x) — prototype only: `include/linux/fs.h:1386  extern void kill_fasync(struct fasync_struct **, int, int);`

### `pipe_buf` (called 1x) — include/linux/pipe_fs_i.h:252
```c
   252  static inline struct pipe_buffer *pipe_buf(const struct pipe_inode_info *pipe,
   253  					   unsigned int slot)
   254  {
   255  	return &pipe->bufs[slot & (pipe->ring_size - 1)];
   256  }
```

### `pipe_buf_confirm` (called 1x) — include/linux/pipe_fs_i.h:299
```c
   299  static inline int pipe_buf_confirm(struct pipe_inode_info *pipe,
   300  				   struct pipe_buffer *buf)
   301  {
   302  	if (!buf->ops->confirm)
   303  		return 0;
   304  	return buf->ops->confirm(pipe, buf);
   305  }
```

### `pipe_full` (called 1x) — include/linux/pipe_fs_i.h:214
```c
   214  static inline bool pipe_full(unsigned int head, unsigned int tail,
   215  			     unsigned int limit)
   216  {
   217  	return pipe_occupancy(head, tail) >= limit;
   218  }
```

### `pipe_is_empty` (called 1x) — include/linux/pipe_fs_i.h:233
```c
   233  static inline bool pipe_is_empty(const struct pipe_inode_info *pipe)
   234  {
   235  	return pipe_empty(pipe->head, pipe->tail);
   236  }
```

### `pipe_readable` (called 1x) — fs/pipe.c:305
```c
   305  static inline bool pipe_readable(const struct pipe_inode_info *pipe)
   306  {
   307  	union pipe_index idx = { .head_tail = READ_ONCE(pipe->head_tail) };
   308  	unsigned int writers = READ_ONCE(pipe->writers);
   309  
   310  	return !pipe_empty(idx.head, idx.tail) || !writers;
   311  }
```

### `pipe_update_tail` (called 1x) — fs/pipe.c:313 [truncated]
```c
   313  static inline unsigned int pipe_update_tail(struct pipe_inode_info *pipe,
   314  					    struct pipe_buffer *buf,
   315  					    unsigned int tail)
   316  {
   317  	pipe_buf_release(pipe, buf);
   318  
   319  	/*
   320  	 * If the pipe has a watch_queue, we need additional protection
   321  	 * by the spinlock because notifications get posted with only
   322  	 * this spinlock, no mutex
   323  	 */
   324  	if (pipe_has_watch_queue(pipe)) {
   325  		spin_lock_irq(&pipe->rd_wait.lock);
   326  #ifdef CONFIG_WATCH_QUEUE
   327  		if (buf->flags & PIPE_BUF_FLAG_LOSS)
   328  			pipe->note_loss = true;
   329  #endif
   330  		pipe->tail = ++tail;
   331  		spin_unlock_irq(&pipe->rd_wait.lock);
   332  		return tail;
   333  	}
   334  
   335  	/*
   336  	 * Without a watch_queue, we can simply increment the tail
        ...
   341  }
```

### `wait_event_interruptible_exclusive` (called 1x) — include/linux/wait.h:626
```c
   626  #define wait_event_interruptible_exclusive(wq, condition)			\
   627  ({										\
   628  	int __ret = 0;								\
   629  	might_sleep();								\
   630  	if (!(condition))							\
   631  		__ret = __wait_event_interruptible_exclusive(wq, condition);	\
   632  	__ret;									\
   633  })
```

### `watch_sizeof` (called 1x) — include/linux/watch_queue.h:123
```c
   123  #define watch_sizeof(STRUCT) (sizeof(STRUCT) << WATCH_INFO_LENGTH__SHIFT)
```

## Callers / references (call-site context, capped)

### fs/pipe.c:485 in `fifo_pipe_read()` — call
```c
   482  static ssize_t
   483  fifo_pipe_read(struct kiocb *iocb, struct iov_iter *to)
   484  {
   485> 	int ret = anon_pipe_read(iocb, to);
   486  	if (ret > 0)
   487  		file_accessed(iocb->ki_filp);
   488  	return ret;
```

### fs/pipe.c:1340 in `pipeanon_fops (struct file_operations initializer)` — reference (e.g. ops table / fn pointer)
```c
  1337  
  1338  static const struct file_operations pipeanon_fops = {
  1339  	.open		= fifo_open,
  1340> 	.read_iter	= anon_pipe_read,
  1341  	.write_iter	= anon_pipe_write,
  1342  	.poll		= pipe_poll,
  1343  	.unlocked_ioctl	= pipe_ioctl,
```
