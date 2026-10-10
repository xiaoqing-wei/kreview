# kbundle: `rwsem_down_write_slowpath()` in kernel/locking/rwsem.c

<!-- kbundle-meta: {"file": "kernel/locking/rwsem.c", "function": "rwsem_down_write_slowpath", "lines": 101, "start": 1138, "end": 1238, "risk": 21, "level": "high", "model": "grok-4.7-xhigh", "tokens": 3675} -->

| field | value |
|---|---|
| estimated tokens | ~3675 (budget 120000, ~4 chars/token) |
| function lines | 101 |
| risk score | 21 (high) |
| recommended model | grok-4.7-xhigh |
| resolvers | ctags, git grep |
| trimming | none |

Everything needed for review is in this bundle. Line numbers refer to kernel/locking/rwsem.c.

## Target function: `rwsem_down_write_slowpath` (kernel/locking/rwsem.c:1138-1238, 101 lines)

```c
  1138  static struct rw_semaphore __sched *
  1139  rwsem_down_write_slowpath(struct rw_semaphore *sem, int state)
  1140  {
  1141  	struct rwsem_waiter waiter, *first;
  1142  	DEFINE_WAKE_Q(wake_q);
  1143  
  1144  	/* do optimistic spinning and steal lock if possible */
  1145  	if (rwsem_can_spin_on_owner(sem) && rwsem_optimistic_spin(sem)) {
  1146  		/* rwsem_optimistic_spin() implies ACQUIRE on success */
  1147  		return sem;
  1148  	}
  1149  
  1150  	/*
  1151  	 * Optimistic spinning failed, proceed to the slowpath
  1152  	 * and block until we can acquire the sem.
  1153  	 */
  1154  	waiter.task = current;
  1155  	waiter.type = RWSEM_WAITING_FOR_WRITE;
  1156  	waiter.timeout = jiffies + RWSEM_WAIT_TIMEOUT;
  1157  	waiter.handoff_set = false;
  1158  
  1159  	raw_spin_lock_irq(&sem->wait_lock);
  1160  
  1161  	first = sem->first_waiter;
  1162  	if (first) {
  1163  		list_add_tail(&waiter.list, &first->list);
  1164  		rwsem_cond_wake_waiter(sem, atomic_long_read(&sem->count),
  1165  				       &wake_q);
  1166  		if (!wake_q_empty(&wake_q)) {
  1167  			/*
  1168  			 * We want to minimize wait_lock hold time especially
  1169  			 * when a large number of readers are to be woken up.
  1170  			 */
  1171  			raw_spin_unlock_irq(&sem->wait_lock);
  1172  			wake_up_q(&wake_q);
  1173  			raw_spin_lock_irq(&sem->wait_lock);
  1174  		}
  1175  	} else {
  1176  		INIT_LIST_HEAD(&waiter.list);
  1177  		sem->first_waiter = &waiter;
  1178  		atomic_long_or(RWSEM_FLAG_WAITERS, &sem->count);
  1179  	}
  1180  
  1181  	/* wait until we successfully acquire the lock */
  1182  	set_current_state(state);
  1183  	trace_contention_begin(sem, LCB_F_WRITE);
  1184  
  1185  	if (state == TASK_UNINTERRUPTIBLE)
  1186  		hung_task_set_blocker(sem, BLOCKER_TYPE_RWSEM_WRITER);
  1187  
  1188  	for (;;) {
  1189  		if (rwsem_try_write_lock(sem, &waiter)) {
  1190  			/* rwsem_try_write_lock() implies ACQUIRE on success */
  1191  			break;
  1192  		}
  1193  
  1194  		raw_spin_unlock_irq(&sem->wait_lock);
  1195  
  1196  		if (signal_pending_state(state, current))
  1197  			goto out_nolock;
  1198  
  1199  		/*
  1200  		 * After setting the handoff bit and failing to acquire
  1201  		 * the lock, attempt to spin on owner to accelerate lock
  1202  		 * transfer. If the previous owner is a on-cpu writer and it
  1203  		 * has just released the lock, OWNER_NULL will be returned.
  1204  		 * In this case, we attempt to acquire the lock again
  1205  		 * without sleeping.
  1206  		 */
  1207  		if (waiter.handoff_set) {
  1208  			enum owner_state owner_state;
  1209  
  1210  			owner_state = rwsem_spin_on_owner(sem);
  1211  			if (owner_state == OWNER_NULL)
  1212  				goto trylock_again;
  1213  		}
  1214  
  1215  		schedule_preempt_disabled();
  1216  		lockevent_inc(rwsem_sleep_writer);
  1217  		set_current_state(state);
  1218  trylock_again:
  1219  		raw_spin_lock_irq(&sem->wait_lock);
  1220  	}
  1221  
  1222  	if (state == TASK_UNINTERRUPTIBLE)
  1223  		hung_task_clear_blocker();
  1224  
  1225  	__set_current_state(TASK_RUNNING);
  1226  	raw_spin_unlock_irq(&sem->wait_lock);
  1227  	lockevent_inc(rwsem_wlock);
  1228  	trace_contention_end(sem, 0);
  1229  	return sem;
  1230  
  1231  out_nolock:
  1232  	__set_current_state(TASK_RUNNING);
  1233  	raw_spin_lock_irq(&sem->wait_lock);
  1234  	rwsem_del_wake_waiter(sem, &waiter, &wake_q);
  1235  	lockevent_inc(rwsem_wlock_fail);
  1236  	trace_contention_end(sem, -EINTR);
  1237  	return ERR_PTR(-EINTR);
  1238  }
```

## Risk signals (auto-detected, heuristic)

**Score 21 → high.** locking +6; atomics +6; lock/unlock count mismatch (4 vs 3; conditional/early-exit paths?) +3; goto error paths x2 +2; length >100 lines +4

- **locking**: `raw_spin_lock_irq` (L1159,1173,1219,1233), `raw_spin_unlock_irq` (L1171,1194,1226)
- **atomics**: `atomic_long_or` (L1178), `atomic_long_read` (L1164)

## Referenced types and macros (definitions via ctags)

### enum owner_state  — kernel/locking/rwsem.c:702
```c
   702  enum owner_state {
   703  	OWNER_NULL		= 1 << 0,
   704  	OWNER_WRITER		= 1 << 1,
   705  	OWNER_READER		= 1 << 2,
   706  	OWNER_NONSPINNABLE	= 1 << 3,
   707  };
```

### struct rwsem_waiter  — kernel/locking/rwsem.c:338
```c
   338  struct rwsem_waiter {
   339  	struct list_head list;
   340  	struct task_struct *task;
   341  	enum rwsem_waiter_type type;
   342  	unsigned long timeout;
   343  	bool handoff_set;
   344  };
```

### macro `BLOCKER_TYPE_RWSEM_WRITER` — include/linux/hung_task.h:36
```c
    36  #define BLOCKER_TYPE_RWSEM_WRITER	0x03UL
```

### macro `RWSEM_FLAG_WAITERS` — kernel/locking/rwsem.c:119
```c
   119  #define RWSEM_FLAG_WAITERS	(1UL << 1)
```

### macro `RWSEM_WAIT_TIMEOUT` — kernel/locking/rwsem.c:357
```c
   357  #define RWSEM_WAIT_TIMEOUT	DIV_ROUND_UP(HZ, 250)
```

### macro `TASK_RUNNING` — include/linux/sched.h:107
```c
   107  #define TASK_RUNNING			0x00000000
```

### macro `TASK_UNINTERRUPTIBLE` — include/linux/sched.h:109
```c
   109  #define TASK_UNINTERRUPTIBLE		0x00000002
```

## Callees (signature + truncated body)

Well-known primitives (locking, alloc, RCU, printk, ...) are omitted; see risk signals.

### `lockevent_inc` (called 3x) — kernel/locking/lock_events.h:43
```c
    43  #define lockevent_inc(ev)	  __lockevent_inc(LOCKEVENT_ ##ev, true)
```

### `__set_current_state` (called 2x) — include/linux/sched.h:240
```c
   240  #define __set_current_state(state_value)				\
   241  	do {								\
   242  		debug_normal_state_change((state_value));		\
   243  		trace_set_current_state(state_value);			\
   244  		WRITE_ONCE(current->__state, (state_value));		\
   245  	} while (0)
```

### `set_current_state` (called 2x) — include/linux/sched.h:247
```c
   247  #define set_current_state(state_value)					\
   248  	do {								\
   249  		debug_normal_state_change((state_value));		\
   250  		trace_set_current_state(state_value);			\
   251  		smp_store_mb(current->__state, (state_value));		\
   252  	} while (0)
```

### `DEFINE_WAKE_Q` (called 1x) — include/linux/sched/wake_q.h:48
```c
    48  #define DEFINE_WAKE_Q(name)					\
    49  	struct wake_q_head name = WAKE_Q_HEAD_INITIALIZER(name)
```

### `INIT_LIST_HEAD` (called 1x) — include/linux/list.h:51
```c
    51  static inline void INIT_LIST_HEAD(struct list_head *list)
    52  {
    53  	WRITE_ONCE(list->next, list);
    54  	WRITE_ONCE(list->prev, list);
    55  }
```

### `hung_task_clear_blocker` (called 1x) — include/linux/hung_task.h:58
```c
    58  static inline void hung_task_clear_blocker(void)
    59  {
    60  	WRITE_ONCE(current->blocker, 0UL);
    61  }
```

### `hung_task_set_blocker` (called 1x) — include/linux/hung_task.h:41
```c
    41  static inline void hung_task_set_blocker(void *lock, unsigned long type)
    42  {
    43  	unsigned long lock_ptr = (unsigned long)lock;
    44  
    45  	WARN_ON_ONCE(!lock_ptr);
    46  	WARN_ON_ONCE(READ_ONCE(current->blocker));
    47  
    48  	/*
    49  	 * If the lock pointer matches the BLOCKER_TYPE_MASK, return
    50  	 * without writing anything.
    51  	 */
    52  	if (lock_ptr & BLOCKER_TYPE_MASK)
    53  		return;
    54  
    55  	WRITE_ONCE(current->blocker, lock_ptr | type);
    56  }
```

### `list_add_tail` (called 1x) — include/linux/list.h:204
```c
   204  static inline void list_add_tail(struct list_head *new, struct list_head *head)
   205  {
   206  	__list_add(new, head->prev, head);
   207  }
```

### `rwsem_can_spin_on_owner` (called 1x) — kernel/locking/rwsem.c:728 [truncated]
```c
   728  static inline bool rwsem_can_spin_on_owner(struct rw_semaphore *sem)
   729  {
   730  	struct task_struct *owner;
   731  	unsigned long flags;
   732  	bool ret = true;
   733  
   734  	if (need_resched()) {
   735  		lockevent_inc(rwsem_opt_fail);
   736  		return false;
   737  	}
   738  
   739  	/*
   740  	 * Disable preemption is equal to the RCU read-side crital section,
   741  	 * thus the task_strcut structure won't go away.
   742  	 */
   743  	owner = rwsem_owner_flags(sem, &flags);
   744  	/*
   745  	 * Don't check the read-owner as the entry may be stale.
   746  	 */
   747  	if ((flags & RWSEM_NONSPINNABLE) ||
   748  	    (owner && !(flags & RWSEM_READER_OWNED) && !owner_on_cpu(owner)))
   749  		ret = false;
   750  
   751  	lockevent_cond_inc(rwsem_opt_fail, !ret);
        ...
   753  }
```

### `rwsem_cond_wake_waiter` (called 1x) — kernel/locking/rwsem.c:996
```c
   996  static inline void rwsem_cond_wake_waiter(struct rw_semaphore *sem, long count,
   997  					  struct wake_q_head *wake_q)
   998  {
   999  	enum rwsem_wake_type wake_type;
  1000  
  1001  	if (count & RWSEM_WRITER_MASK)
  1002  		return;
  1003  
  1004  	if (count & RWSEM_READER_MASK) {
  1005  		wake_type = RWSEM_WAKE_READERS;
  1006  	} else {
  1007  		wake_type = RWSEM_WAKE_ANY;
  1008  		clear_nonspinnable(sem);
  1009  	}
  1010  	rwsem_mark_wake(sem, wake_type, wake_q);
  1011  }
```

### `rwsem_del_wake_waiter` (called 1x) — kernel/locking/rwsem.c:597
```c
   597  static inline void
   598  rwsem_del_wake_waiter(struct rw_semaphore *sem, struct rwsem_waiter *waiter,
   599  		      struct wake_q_head *wake_q)
   600  		      __releases(&sem->wait_lock)
   601  {
   602  	bool first = sem->first_waiter == waiter;
   603  
   604  	wake_q_init(wake_q);
   605  
   606  	/*
   607  	 * If the wait_list isn't empty and the waiter to be deleted is
   608  	 * the first waiter, we wake up the remaining waiters as they may
   609  	 * be eligible to acquire or spin on the lock.
   610  	 */
   611  	if (rwsem_del_waiter(sem, waiter) && first)
   612  		rwsem_mark_wake(sem, RWSEM_WAKE_ANY, wake_q);
   613  	raw_spin_unlock_irq(&sem->wait_lock);
   614  	if (!wake_q_empty(wake_q))
   615  		wake_up_q(wake_q);
   616  }
```

### `rwsem_optimistic_spin` (called 1x) — kernel/locking/rwsem.c:840 [truncated]
```c
   840  static bool rwsem_optimistic_spin(struct rw_semaphore *sem)
   841  {
   842  	bool taken = false;
   843  	int prev_owner_state = OWNER_NULL;
   844  	int loop = 0;
   845  	u64 rspin_threshold = 0;
   846  
   847  	/* sem->wait_lock should not be held when doing optimistic spinning */
   848  	if (!osq_lock(&sem->osq))
   849  		goto done;
   850  
   851  	/*
   852  	 * Optimistically spin on the owner field and attempt to acquire the
   853  	 * lock whenever the owner changes. Spinning will be stopped when:
   854  	 *  1) the owning writer isn't running; or
   855  	 *  2) readers own the lock and spinning time has exceeded limit.
   856  	 */
   857  	for (;;) {
   858  		enum owner_state owner_state;
   859  
   860  		owner_state = rwsem_spin_on_owner(sem);
   861  		if (owner_state == OWNER_NONSPINNABLE)
   862  			break;
   863  
        ...
   956  }
```

### `rwsem_spin_on_owner` (called 1x) — kernel/locking/rwsem.c:767 [truncated]
```c
   767  static noinline enum owner_state
   768  rwsem_spin_on_owner(struct rw_semaphore *sem)
   769  {
   770  	struct task_struct *new, *owner;
   771  	unsigned long flags, new_flags;
   772  	enum owner_state state;
   773  
   774  	lockdep_assert_preemption_disabled();
   775  
   776  	owner = rwsem_owner_flags(sem, &flags);
   777  	state = rwsem_owner_state(owner, flags);
   778  	if (state != OWNER_WRITER)
   779  		return state;
   780  
   781  	for (;;) {
   782  		/*
   783  		 * When a waiting writer set the handoff flag, it may spin
   784  		 * on the owner as well. Once that writer acquires the lock,
   785  		 * we can spin on it. So we don't need to quit even when the
   786  		 * handoff bit is set.
   787  		 */
   788  		new = rwsem_owner_flags(sem, &new_flags);
   789  		if ((new != owner) || (new_flags != flags)) {
   790  			state = rwsem_owner_state(new, new_flags);
        ...
   813  }
```

### `rwsem_try_write_lock` (called 1x) — kernel/locking/rwsem.c:625 [truncated]
```c
   625  static inline bool rwsem_try_write_lock(struct rw_semaphore *sem,
   626  					struct rwsem_waiter *waiter)
   627  	__must_hold(&sem->wait_lock)
   628  {
   629  	struct rwsem_waiter *first = sem->first_waiter;
   630  	long count, new;
   631  
   632  	lockdep_assert_held(&sem->wait_lock);
   633  
   634  	count = atomic_long_read(&sem->count);
   635  	do {
   636  		bool has_handoff = !!(count & RWSEM_FLAG_HANDOFF);
   637  
   638  		if (has_handoff) {
   639  			/*
   640  			 * Honor handoff bit and yield only when the first
   641  			 * waiter is the one that set it. Otherwisee, we
   642  			 * still try to acquire the rwsem.
   643  			 */
   644  			if (first->handoff_set && (waiter != first))
   645  				return false;
   646  		}
   647  
   648  		new = count;
        ...
   689  }
```

- `schedule_preempt_disabled` (called 1x) — prototype only: `include/linux/sched.h:338  extern void schedule_preempt_disabled(void);`

### `signal_pending_state` (called 1x) — include/linux/sched/signal.h:409
```c
   409  static inline int signal_pending_state(unsigned int state, struct task_struct *p)
   410  {
   411  	if (!(state & (TASK_INTERRUPTIBLE | TASK_WAKEKILL)))
   412  		return 0;
   413  	if (!signal_pending(p))
   414  		return 0;
   415  
   416  	return (state & TASK_INTERRUPTIBLE) || __fatal_signal_pending(p);
   417  }
```

### `wake_q_empty` (called 1x) — include/linux/sched/wake_q.h:57
```c
    57  static inline bool wake_q_empty(struct wake_q_head *head)
    58  {
    59  	return head->first == WAKE_Q_TAIL;
    60  }
```

- `wake_up_q` (called 1x) — prototype only: `include/linux/sched/wake_q.h:64  extern void wake_up_q(struct wake_q_head *head);`

## Callers / references (call-site context, capped)

### kernel/locking/rwsem.c:1347 in `__down_write_common()` — call
```c
  1344  
  1345  	preempt_disable();
  1346  	if (unlikely(!rwsem_write_trylock(sem))) {
  1347> 		if (IS_ERR(rwsem_down_write_slowpath(sem, state)))
  1348  			ret = -EINTR;
  1349  	}
  1350  	preempt_enable();
```
