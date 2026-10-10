You are reviewing ONE Linux kernel function for bugs. The complete review context is the
"kbundle" document that follows (or the single bundle file named below). It contains:
the target function with real line numbers, definitions of referenced structs/macros/typedefs,
truncated callee bodies, caller call-sites, auto-detected risk signals, and (optionally) the patch.

RULES
- Do NOT read, open, list, grep, or search any other file or directory. Do NOT run shell commands.
  Use ONLY the bundle. If something you need is missing, say so in the rationale and lower confidence;
  do not go looking for it.
- Review only the TARGET function. Callee/caller code is context; report a callee bug only if the
  target function's use of it is wrong.
- If a patch is included, focus on what the patch changes and what it could break.
- Do not report style, naming, comments, or checkpatch-level nits.
- Prefer few, real findings over many speculative ones. An empty array is a valid answer.

CHECKLIST (go through every item)
1. Locking: lock/unlock balance on every path (incl. early return/goto/break), correct lock
   for the data touched, sleeping while holding a spinlock / in atomic or RCU read-side context,
   lock ordering / ABBA, irqsave vs irq vs bh variants, lockdep annotations vs reality.
2. Lifetime / refcount: get/put balance on all paths, use-after-free, use after put/unlock,
   object freed while still reachable (RCU grace period, list removal), double free, leaks.
3. Error paths: every failure path unwinds exactly what was set up; errors propagated with the
   right sign/value; partial-progress return semantics (e.g. bytes done vs -EFAULT).
4. Integer overflow / truncation: size arithmetic (mult/add before alloc or copy), signed/unsigned
   mixing, implicit narrowing (size_t -> int), shift overflow, off-by-one, index bounds.
5. User pointers: __user deref without copy_*_user/get_user, missing access_ok, TOCTOU / double
   fetch, kernel info leak (uninitialized struct padding/bytes copied to user), unchecked length.
6. Endianness: missing/incorrect le/be conversions, mixing __le/__be with CPU-order values,
   unaligned access.
7. Memory ordering: lockless accesses missing READ_ONCE/WRITE_ONCE, missing or misplaced barriers,
   acquire/release pairing, publish-before-init, data races on fields protected elsewhere.

OUTPUT
Return ONLY a JSON array (no prose, no markdown fences). Each element:
  {"line": <int, line number in the target file as shown in the bundle>,
   "severity": "critical"|"high"|"medium"|"low",
   "category": "locking"|"lifetime"|"error-path"|"integer-overflow"|"user-pointer"|"endianness"|"memory-ordering"|"other",
   "rationale": "<concise explanation: the triggering path and the consequence>",
   "confidence": <float 0.0-1.0>}
If you find nothing, return [].
