# kbundle: `anon_pipe_read()` in fs/pipe.c

<!-- kbundle-meta: {"file": "fs/pipe.c", "function": "anon_pipe_read", "lines": 138, "start": 343, "end": 480, "risk": 27, "level": "high", "model": "grok-4.7-xhigh", "tokens": 2445} -->

| field | value |
|---|---|
| estimated tokens | ~2445 (budget 2500, ~4 chars/token) |
| function lines | 138 |
| risk score | 27 (high) |
| recommended model | grok-4.7-xhigh |
| resolvers | ctags, git grep |
| trimming | Callers / references→reduced(2); Callees→reduced(13); Callers / references→dropped(2); Referenced types and macros→reduced(16); Callees→dropped(15); Referenced types and macros→dropped(1) |

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

### struct file  — include/linux/fs.h:1255 (reduced)
```c
  1255  struct file {
  1256  	spinlock_t			f_lock;
  1257  	fmode_t				f_mode;
        ...
  1260  	void				*private_data;
        ...
  1262  	unsigned int			f_flags;
        ...
  1272  		/* regular files (with FMODE_ATOMIC_POS) and directories */
  1273  		struct mutex		f_pos_lock;
        ...
  1295  } __randomize_layout
```

### struct iov_iter  — include/linux/uio.h:43 (reduced)
```c
    43  struct iov_iter {
    44  	u8 iter_type;
    45  	bool nofault;
        ...
    49  	 * Hack alert: overlay ubuf_iovec with iovec + count, so
    50  	 * that the members resolve correctly regardless of the type
        ...
    83  };
```

### struct kiocb  — include/linux/fs.h:379 (reduced)
```c
   379  struct kiocb {
   380  	struct file		*ki_filp;
   381  	loff_t			ki_pos;
        ...
   384  	int			ki_flags;
        ...
   388  	/*
   389  	 * Only used for async buffered reads, where it denotes the page
        ...
   394  };
```

### struct pipe_buffer  — include/linux/pipe_fs_i.h:29 (reduced)
```c
    29  struct pipe_buffer {
    30  	struct page *page;
    31  	unsigned int offset, len;
    32  	const struct pipe_buf_operations *ops;
    33  	unsigned int flags;
    34  	unsigned long private;
    35  };
```

### struct pipe_inode_info  — include/linux/pipe_fs_i.h:102 (reduced)
```c
   102  struct pipe_inode_info {
   103  	struct mutex mutex;
   104  	wait_queue_head_t rd_wait, wr_wait;
        ...
   108  	unsigned int max_usage;
        ...
   112  	unsigned int writers;
        ...
   118  	bool note_loss;
        ...
   122  	struct fasync_struct *fasync_writers;
        ...
   128  };
```

### struct watch_notification  — include/uapi/linux/watch_queue.h:29 (reduced)
```c
    29  struct watch_notification {
    30  	__u32			type:24;	/* enum watch_notification_type */
    31  	__u32			subtype:8;	/* Type-specific subtype (filterable) */
    32  	__u32			info;
        ...
    37  #define WATCH_INFO_TYPE_INFO	0xffff0000	/* Type-specific info */
        ...
    39  #define WATCH_INFO_FLAG_0	0x00010000	/* Type-specific info, flag bit 0 */
        ...
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
    31  #define EPOLLIN		(__force __poll_t)0x00000001 ...
```

### macro `EPOLLOUT` — include/uapi/linux/eventpoll.h:33
```c
    33  #define EPOLLOUT	(__force __poll_t)0x00000004 ...
```

### macro `EPOLLRDNORM` — include/uapi/linux/eventpoll.h:37
```c
    37  #define EPOLLRDNORM	(__force __poll_t)0x00000040 ...
```

### macro `EPOLLWRNORM` — include/uapi/linux/eventpoll.h:39
```c
    39  #define EPOLLWRNORM	(__force __poll_t)0x00000100 ...
```

### macro `ERESTARTSYS` — include/linux/errno.h:14
```c
    14  #define ERESTARTSYS	512 ...
```

### macro `IOCB_NOWAIT` — include/linux/fs.h:342
```c
   342  #define IOCB_NOWAIT		(__force int) RWF_NOWAIT ...
```

### macro `PIPE_BUF_FLAG_PACKET` — include/linux/pipe_fs_i.h:10
```c
    10  #define PIPE_BUF_FLAG_PACKET	0x08	/* read() as a packet */ ...
```

### macro `PIPE_BUF_FLAG_WHOLE` — include/linux/pipe_fs_i.h:12
```c
    12  #define PIPE_BUF_FLAG_WHOLE	0x20	/* read() must return entire buffer or error */ ...
```

### typedef `size_t` — include/linux/types.h:61
```c
    61  typedef __kernel_size_t		size_t; ...
```

_[budget: 1 item(s) dropped, 15 reduced to signature/short form]_

## Callees (signature + truncated body)

Well-known primitives (locking, alloc, RCU, printk, ...) are omitted; see risk signals.



_[budget: 15 item(s) dropped, 0 reduced to signature/short form]_

## Callers / references (call-site context, capped)



_[budget: 2 item(s) dropped, 0 reduced to signature/short form]_
