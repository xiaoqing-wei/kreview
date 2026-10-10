#!/usr/bin/env python3
"""risk.py - heuristic risk signals + model recommendation for one kernel function.

Library use (from kbundle.py):  detect_signals(), score(), recommend(), render_signals()
CLI:  risk.py <kernel_root> <file.c> <function>   -> prints JSON {score, level, model, ...}
Thresholds can be tuned via env: KREVIEW_HIGH_RISK (default 14), KREVIEW_TRIVIAL (default 2).
Model IDs verified with `agent --list-models` (agent 2026.10.01, 2026-10-09): grok-4.7-low, grok-4.7-high,
grok-4.7-xhigh (also -medium and -fast variants), composer-2.5. kreview.py can override the routing.
"""
import json, os, re, sys

# category -> (weight per distinct primitive, cap, regex)
CATEGORIES = {
    "locking": (3, 9, r"\b((raw_)?spin_(try)?lock\w*|(raw_)?spin_unlock\w*|(raw_)?(read|write)_(try)?(un)?lock\w*|"
                      r"mutex_(un)?lock\w*|mutex_trylock\w*|down_(read|write)\w*|up_(read|write)\w*|"
                      r"down|up|down_interruptible|down_killable|down_trylock|(raw_)?write_seq\w*|read_seq\w*|"
                      r"seqcount_\w+|local_irq_\w+|local_bh_\w+|preempt_(disable|enable)\w*|lockdep_assert\w*|"
                      r"lock_page|unlock_page|folio_(un)?lock|guard|scoped_guard|(raw_)?spin_lock_init|mutex_init)\s*\("),
    "rcu": (3, 6, r"\b(rcu_read_(un)?lock\w*|rcu_dereference\w*|rcu_assign_pointer|"
                  r"synchronize_rcu\w*|call_rcu\w*|kfree_rcu|list_\w+_rcu|hlist_\w+_rcu|srcu_\w+)\s*\("),
    "atomics": (3, 8, r"\b(atomic(64)?_\w+|atomic_long_\w+|cmpxchg\w*|xchg\w*|try_cmpxchg\w*|"
                      r"test_and_(set|clear)_bit\w*|set_bit|clear_bit|test_bit)\s*\("),
    "barriers": (4, 8, r"\b(smp_(r|w)?mb|smp_mb__\w+|mb|rmb|wmb|barrier|smp_load_acquire|"
                       r"smp_store_release|smp_cond_load_\w+|dma_(r|w)mb|virt_(r|w)?mb)\s*\("),
    "once_accessors": (1, 3, r"\b(READ_ONCE|WRITE_ONCE|data_race)\s*\("),
    "user_memory": (5, 10, r"\b(copy_(from|to)_user\w*|get_user|put_user|__get_user|__put_user|"
                           r"strncpy_from_user|clear_user|copy_\w+_iter|copy_page_\w+_iter|"
                           r"user_access_begin|unsafe_(get|put)_user|access_ok|memdup_user\w*|"
                           r"import_iovec|iov_iter_\w+)\s*\("),
    "alloc_free": (2, 6, r"\b(k[mzv]?alloc\w*|kcalloc|krealloc\w*|kv[mz]alloc\w*|kvcalloc|vmalloc\w*|vzalloc|"
                         r"kfree\w*|kvfree\w*|vfree|kmem_cache_(alloc|free)\w*|alloc_pages?\w*|"
                         r"__free_pages?|free_pages?|put_page|get_page|devm_\w*alloc\w*)\s*\("),
    "refcount": (3, 9, r"\b(refcount_\w+|kref_\w+|\w+_get|\w+_put|get_\w+|put_\w+|"
                       r"\w+_tryget\w*|dget|dput|iget\w*|iput|fget\w*|fput|"
                       r"get_task_struct|put_task_struct)\s*\("),
    "endianness": (2, 4, r"\b((le|be)(16|32|64)_to_cpu\w*|cpu_to_(le|be)(16|32|64)\w*|"
                         r"get_unaligned\w*|put_unaligned\w*|ntoh[sl]|hton[sl])\s*\("),
    "overflow_helpers": (1, 3, r"\b(check_(add|mul|sub)_overflow|array_size|struct_size|size_mul|"
                               r"size_add|array3_size|flex_array_size)\s*\("),
}
_RX = {k: re.compile(v[2]) for k, v in CATEGORIES.items()}

ALL_PRIMITIVE_NAMES = set("""spin_lock spin_unlock spin_lock_irq spin_unlock_irq spin_lock_irqsave
spin_unlock_irqrestore spin_lock_bh spin_unlock_bh raw_spin_lock raw_spin_unlock mutex_lock mutex_unlock
mutex_trylock mutex_lock_interruptible mutex_lock_killable down_read up_read down_write up_write
rcu_read_lock rcu_read_unlock rcu_dereference rcu_assign_pointer synchronize_rcu call_rcu kfree_rcu
smp_mb smp_rmb smp_wmb smp_load_acquire smp_store_release barrier READ_ONCE WRITE_ONCE
copy_from_user copy_to_user get_user put_user kmalloc kzalloc kcalloc kfree kvfree kvmalloc vmalloc vfree
refcount_inc refcount_dec refcount_dec_and_test kref_get kref_put atomic_inc atomic_dec atomic_read
atomic_set atomic_dec_and_test atomic_inc_return cmpxchg xchg local_irq_save local_irq_restore
preempt_disable preempt_enable lockdep_assert_held guard scoped_guard""".split())

_PRIM_CATS = ("locking", "rcu", "atomics", "barriers", "once_accessors")


def is_primitive(name):
    """True for well-known sync primitives whose bodies add tokens but not review value."""
    return name in ALL_PRIMITIVE_NAMES or any(_RX[c].match(name + "(") for c in _PRIM_CATS)


ACQ = re.compile(r"\b((raw_)?spin_lock\w*|read_lock\w*|write_lock\w*|mutex_lock\w*|down_read\w*|"
                 r"down_write\w*|rcu_read_lock\w*)\s*\(")
REL = re.compile(r"\b((raw_)?spin_unlock\w*|read_unlock\w*|write_unlock\w*|mutex_unlock|up_read|"
                 r"up_write|rcu_read_unlock\w*)\s*\(")
MULT_SIZE = re.compile(r"\b(k[mz]alloc|kvmalloc|vmalloc|kvzalloc)\s*\([^;]*\*")


def detect_signals(code_lines, first_line):
    """code_lines: comment/string-stripped lines of the function. Returns dict cat -> {prim: [lines]}."""
    sig = {}
    for i, l in enumerate(code_lines):
        for cat, rx in _RX.items():
            for m in rx.finditer(l):
                prim = m.group(0).rstrip("( \t")
                if cat == "refcount" and (prim.startswith(("trace_", "__")) or _RX["user_memory"].match(m.group(0))
                                          or _RX["atomics"].match(m.group(0))):
                    continue  # keep categories roughly disjoint; skip tracepoints
                sig.setdefault(cat, {}).setdefault(prim, []).append(first_line + i)
    return sig


def score(sig, nlines, code=""):
    s, reasons = 0, []
    for cat, prims in sig.items():
        w, cap, _ = CATEGORIES[cat]
        pts = min(cap, w * len(prims))
        s += pts
        reasons.append("%s +%d" % (cat, pts))
    acq = len(ACQ.findall(code)); rel = len(REL.findall(code))
    if acq or rel:
        if acq != rel:
            s += 3; reasons.append("lock/unlock count mismatch (%d vs %d; conditional/early-exit paths?) +3" % (acq, rel))
    gotos = len(re.findall(r"\bgoto\s+\w+", code))
    rets = len(re.findall(r"\breturn\b", code))
    if gotos:
        p = min(4, gotos); s += p; reasons.append("goto error paths x%d +%d" % (gotos, p))
    if rets > 3:
        s += 2; reasons.append("many return paths (%d) +2" % rets)
    if MULT_SIZE.search(code):
        s += 3; reasons.append("multiplication inside allocation size +3")
    if re.search(r"\b__user\b", code):
        s += 2; reasons.append("__user pointer +2")
    if nlines > 100:
        s += 4; reasons.append("length >100 lines +4")
    elif nlines > 40:
        s += 2; reasons.append("length >40 lines +2")
    hi = int(os.environ.get("KREVIEW_HIGH_RISK", "14"))
    tr = int(os.environ.get("KREVIEW_TRIVIAL", "2"))
    heavy = {"locking", "rcu", "atomics", "barriers", "user_memory", "alloc_free", "refcount"}
    trivial = s <= tr and nlines <= 15 and not (heavy & set(sig))
    level = "high" if s >= hi else ("trivial" if trivial else ("medium" if s >= hi // 2 else "low"))
    return {"score": s, "level": level, "reasons": reasons, "acquires": acq, "releases": rel}


def recommend(rs, nlines, allow_low=None):
    """xhigh for high risk regardless of length; low for trivial (opt-in); else high."""
    if allow_low is None:
        allow_low = os.environ.get("KREVIEW_ALLOW_LOW", "1") == "1"
    if rs["level"] == "high":
        return os.environ.get("KREVIEW_MODEL_XHIGH", "grok-4.7-xhigh")
    if rs["level"] == "trivial" and allow_low:
        return os.environ.get("KREVIEW_MODEL_LOW", "grok-4.7-low")
    return os.environ.get("KREVIEW_MODEL_HIGH", "grok-4.7-high")


def render_signals(sig, rs):
    out = ["**Score %d → %s.** %s" % (rs["score"], rs["level"], "; ".join(rs["reasons"]) or "no signals"), ""]
    if not sig:
        out.append("_No locking / RCU / atomic / user-copy / alloc / refcount primitives detected._")
    for cat in CATEGORIES:
        if cat in sig:
            items = ", ".join("`%s` (L%s)" % (p, ",".join(map(str, ls[:6])) + ("…" if len(ls) > 6 else ""))
                              for p, ls in sorted(sig[cat].items()))
            out.append("- **%s**: %s" % (cat, items))
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kbundle
    root, f, fn = sys.argv[1:]
    p = os.path.join(root, f)
    loc = kbundle.find_function(p, fn)
    if not loc:
        sys.exit("function not found")
    lines = kbundle.read_lines(p)[loc[0] - 1:loc[1]]
    code = kbundle.strip_comments_strings("\n".join(lines))
    sig = detect_signals(code.split("\n"), loc[0])
    rs = score(sig, len(lines), code)
    print(json.dumps({"function": fn, "lines": len(lines), **rs, "model": recommend(rs, len(lines)),
                      "signals": {c: sorted(v) for c, v in sig.items()}}, indent=2))
