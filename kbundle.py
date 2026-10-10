#!/usr/bin/env python3
"""kbundle.py - build a compact, token-budgeted review bundle for ONE kernel function.

Usage:
  kbundle.py <kernel_root> <file.c> <function> [--max-tokens 120000] [--diff patch]
             [--meta-json out.json] [--arch x86]
  kbundle.py <kernel_root> <file.c> --list-functions

Python 3 stdlib only.  Uses universal-ctags / cscope / git when available, falls
back to regex scanning and `git grep` / os.walk otherwise.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk as riskmod  # noqa: E402

CHARS_PER_TOKEN = 4
CTAGS_IGNORE = ("__init,__exit,__always_inline,noinline,__must_check,__cold,__hot,"
                "notrace,__sched,__weak,__maybe_unused,__used,__rcu,__user,__iomem,"
                "__percpu,__force,__latent_entropy,__randomize_layout,"
                "__acquires+,__releases+,__must_hold+,__attribute__+,__aligned+,"
                "____cacheline_aligned,____cacheline_aligned_in_smp,__cacheline_aligned,"
                "__packed,asmlinkage,__visible,__noreturn,__printf+,__ro_after_init")

C_KEYWORDS = set("""auto break case char const continue default do double else enum extern
float for goto if inline int long register restrict return short signed sizeof static
struct switch typedef union unsigned void volatile while _Bool bool true false NULL
typeof __typeof__ asm __asm__ __volatile__ __attribute__ unsigned""".split())

# Callees not worth a body: well-known primitives / helpers.  They still feed risk signals.
SKIP_CALLEES = set("""likely unlikely WARN_ON WARN_ON_ONCE BUG_ON BUG WARN WARN_ONCE
min max min_t max_t clamp ARRAY_SIZE offsetof container_of sizeof READ_ONCE WRITE_ONCE
pr_err pr_warn pr_info pr_debug pr_notice printk dev_err dev_warn dev_info dev_dbg
IS_ERR PTR_ERR ERR_PTR IS_ERR_OR_NULL ERR_CAST memcpy memset memmove strlen strcmp
spin_lock spin_unlock spin_lock_irq spin_unlock_irq spin_lock_irqsave spin_unlock_irqrestore
spin_lock_bh spin_unlock_bh mutex_lock mutex_unlock mutex_lock_interruptible mutex_trylock
rcu_read_lock rcu_read_unlock kfree kmalloc kzalloc kcalloc kvfree kvmalloc
might_sleep lockdep_assert_held BUILD_BUG_ON""".split())




ATTR_MACROS = set(x.rstrip("+") for x in CTAGS_IGNORE.split(","))


def est_tokens(s):
    return (len(s) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def run(cmd, cwd=None, timeout=300):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def have(tool):
    return shutil.which(tool) is not None


def is_universal_ctags():
    if not have("ctags"):
        return False
    _, out = run(["ctags", "--version"])
    return "Universal Ctags" in out


def read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().split("\n")
    except OSError:
        return []


def strip_comments_strings(text):
    """Blank out comments and string/char literals, preserving newlines/offsets."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j])); i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(c + " " * max(0, j - i - 2) + (c if j - i >= 2 else "")); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


# ---------------------------------------------------------------- function location
FUNC_HDR_RE = re.compile(r"^(?:[A-Za-z_][\w\s\*]*?[\s\*])?([A-Za-z_]\w*)\s*\(")
INIT_RE = re.compile(r"^(?:static\s+)?(?:const\s+)?struct\s+(\w+)\s+(\w+)(?:\[\])?\s*=\s*\{")


def brace_end(lines, start_idx):
    """Given 0-based index of a line at/before the opening brace, return 0-based end idx."""
    code = strip_comments_strings("\n".join(lines[start_idx:])).split("\n")
    depth, seen = 0, False
    for k, l in enumerate(code):
        for ch in l:
            if ch == "{":
                depth += 1; seen = True
            elif ch == "}":
                depth -= 1
                if seen and depth == 0:
                    return start_idx + k
        if not seen and l.rstrip().endswith(";"):
            return None  # prototype
    return None


def list_functions(path):
    """Return list of (name, start_line, end_line) 1-based."""
    res = []
    if is_universal_ctags():
        rc, out = run(["ctags", "--languages=C", "--language-force=C", "--kinds-C=f",
                       "--fields=+ne", "-I", CTAGS_IGNORE, "--output-format=json",
                       "-o", "-", path])
        if rc == 0:
            for l in out.splitlines():
                try:
                    t = json.loads(l)
                except ValueError:
                    continue
                if t.get("kind") == "function" and "line" in t:
                    res.append((t["name"], t["line"], t.get("end", t["line"])))
    if not res:  # regex fallback
        lines = read_lines(path)
        for i, l in enumerate(lines):
            if not l or l[0] in " \t#{}/*" or l.rstrip().endswith(";"):
                continue
            m = FUNC_HDR_RE.match(l)
            if not m or m.group(1) in C_KEYWORDS:
                continue
            e = brace_end(lines, i)
            if e is not None and e - i < 5000:
                res.append((m.group(1), i + 1, e + 1))
    # SYSCALL_DEFINEn(name, ...) - ctags sees these as 'SYSCALL_DEFINE3'
    res = [r for r in res if not r[0].startswith(("SYSCALL_DEFINE", "COMPAT_SYSCALL_DEFINE"))]
    lines = read_lines(path)
    for i, l in enumerate(lines):
        m = re.match(r"^(?:COMPAT_)?SYSCALL_DEFINE\d\((\w+)", l)
        if m:
            e = brace_end(lines, i)
            if e is not None:
                res.append(("sys_" + m.group(1), i + 1, e + 1))
    res.sort(key=lambda r: r[1])
    return res


class KBundleError(Exception):
    pass


def find_function(path, name, line_hint=None):
    """Return (start, end) 1-based incl. leading return-type lines. With line_hint, prefer the
    definition containing that line (same-named static functions under #ifdef)."""
    cands = [(n, s, e) for n, s, e in list_functions(path) if n == name]
    if line_hint is not None:
        inside = [c for c in cands if c[1] <= line_hint <= c[2]]
        cands = inside or cands
    for n, s, e in cands[:1]:
            # ctags 'line' is the name line; pull in preceding return-type/attr lines
            lines = read_lines(path)
            s0 = s
            while s0 > 1:
                prev = lines[s0 - 2]
                if prev.strip() == "" or prev.rstrip().endswith((";", "}", "*/")) or prev.startswith("#"):
                    break
                s0 -= 1
            return s0, e
    return None


# ---------------------------------------------------------------- tags index
class TagIndex:
    """Cached ctags index of include/ (+arch include, +target dir). Falls back to regex."""

    KINDS = "dfgpstue"  # macro func enum proto struct typedef union enumerator

    def __init__(self, root, target_rel, arch="x86"):
        self.root = root
        self.target_dir = os.path.dirname(target_rel) or "."
        self.arch = arch
        self.backend = "ctags" if is_universal_ctags() else "regex"
        self.notes = []
        _, head = run(["git", "-C", root, "rev-parse", "HEAD"]) if have("git") else (1, "")
        key = os.path.abspath(root) + "@" + head.strip()  # new commit -> new cache
        self.cache_dir = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                                      "kbundle", hashlib.sha1(key.encode()).hexdigest()[:12])
        os.makedirs(self.cache_dir, exist_ok=True)

    def _scopes(self):
        s = [("include", "include")]
        a = os.path.join("arch", self.arch, "include")
        if os.path.isdir(os.path.join(self.root, a)):
            s.append(("arch_" + self.arch, a))
        s.append(("dir_" + self.target_dir.replace("/", "_"), self.target_dir))
        return s

    def _build(self, key, rel, recursive):
        out = os.path.join(self.cache_dir, key + ".tags.json")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            stale = False
            if not recursive:  # target dir: rebuild if any source there was edited since
                t0 = os.path.getmtime(out)
                d = os.path.join(self.root, rel)
                stale = any(os.path.getmtime(os.path.join(d, f)) > t0 for f in os.listdir(d)
                            if f.endswith((".c", ".h")))
            if not stale:
                return out
        if not os.path.isdir(os.path.join(self.root, rel)):
            return None
        cmd = ["ctags", "--languages=C,C++", "--langmap=C:+.h", "--kinds-C=" + self.KINDS,
               "--fields=+neKS", "--output-format=json", "-I", CTAGS_IGNORE, "-f", out]
        if recursive:
            cmd += ["-R", rel]
        else:
            files = [os.path.join(rel, f) for f in os.listdir(os.path.join(self.root, rel))
                     if f.endswith((".c", ".h"))]
            if not files:
                return None
            cmd += files
        rc, _ = run(cmd, cwd=self.root, timeout=900)
        return out if rc == 0 and os.path.exists(out) else None

    def lookup(self, names):
        """names: set of identifiers -> dict name -> list of tag dicts."""
        found = {}
        if self.backend == "ctags":
            for key, rel in self._scopes():
                f = self._build(key, rel, recursive=not key.startswith("dir_"))
                if not f:
                    continue
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for l in fh:
                        # cheap prefilter before json parse
                        q = l.find('"name": "')
                        if q < 0:
                            continue
                        nm = l[q + 9:l.find('"', q + 9)]
                        if nm not in names:
                            continue
                        try:
                            t = json.loads(l)
                        except ValueError:
                            continue
                        found.setdefault(nm, []).append(t)
        else:
            found = self._regex_lookup(names)
        return found

    def _regex_lookup(self, names):
        """Fallback: batched git grep for #define / struct X { / typedef ... X; / function defs."""
        self.notes.append("ctags unavailable: regex/git-grep fallback for definitions")
        found = {}
        names = sorted(n for n in names if re.match(r"^[A-Za-z_]\w*$", n))
        kinds = [("macro", r"^#\s*define\s+(%s)\b", 1),
                 ("struct", r"^(struct|union|enum)\s+(%s)\s*\{", 2),
                 ("typedef", r"^typedef\b.*\b(%s)\s*;", 1),
                 ("function", r"^[A-Za-z][^;=]*\b(%s)\s*\([^;]*$", 1)]
        for i in range(0, len(names), 200):  # keep regexes a sane size
            alt = "|".join(names[i:i + 200])
            for kind, pat, grp in kinds:
                rx = re.compile(pat % alt)
                for path, ln, txt in grep_tree(self.root, pat % alt, ["include", self.target_dir], limit=10 ** 6):
                    m = rx.search(txt)
                    if not m:
                        continue
                    k = kind if kind != "struct" else m.group(1)
                    found.setdefault(m.group(grp), []).append({"name": m.group(grp), "path": path, "line": ln, "kind": k})
        return found


def grep_tree(root, pattern, paths, limit=50, fixed_word=None):
    """Yield (relpath, line, text). git grep if repo, else os.walk + re."""
    res = []
    if have("git") and os.path.isdir(os.path.join(root, ".git")):
        cmd = ["git", "grep", "-n", "-I", "--no-color"]
        if fixed_word:
            cmd += ["-w", "-F", "-e", fixed_word]
        else:
            cmd += ["-E", "-e", pattern]
        cmd += ["--"] + [p for p in paths]
        rc, out = run(cmd, cwd=root, timeout=120)
        for l in out.splitlines():
            parts = l.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                res.append((parts[0], int(parts[1]), parts[2]))
                if len(res) >= limit * 20:
                    break
    else:
        rx = re.compile(r"\b%s\b" % re.escape(fixed_word)) if fixed_word else re.compile(pattern)
        if any("*" in p for p in paths):
            paths = ["."]  # globs like '*.c' -> walk whole tree (extension filter below)
        for p in paths:
            base = os.path.join(root, p)
            walk = [(os.path.dirname(base), [], [os.path.basename(base)])] if os.path.isfile(base) else os.walk(base)
            for d, dirs, fs in walk:
                dirs[:] = [x for x in dirs if not x.startswith(".")]
                for f in fs:
                    if not f.endswith((".c", ".h")):
                        continue
                    fp = os.path.join(d, f)
                    for i, l in enumerate(read_lines(fp)):
                        if rx.search(l):
                            res.append((os.path.relpath(fp, root), i + 1, l))
    return res


# ---------------------------------------------------------------- extraction helpers
def extract_def(root, tag, max_lines, keep_words=None):
    """Return (header, body_text, truncated) for a tag."""
    path = os.path.join(root, tag["path"])
    lines = read_lines(path)
    s = tag.get("line", 1) - 1
    kind = tag.get("kind", "")
    if kind == "macro":
        e = s
        while e < len(lines) - 1 and lines[e].rstrip().endswith("\\"):
            e += 1
    elif "end" in tag and tag["end"] >= tag["line"]:
        e = tag["end"] - 1
    else:
        e = brace_end(lines, s)
        if e is None:
            e = s
            while e < len(lines) - 1 and not lines[e].rstrip().endswith(";") and e - s < 15:
                e += 1
    if kind in ("function",):  # include return-type line(s) above
        while s > 0 and lines[s - 1].strip() and not lines[s - 1].rstrip().endswith((";", "}", "*/")) \
                and not lines[s - 1].startswith("#"):
            s -= 1
    seg = list(range(s, e + 1))
    truncated = False
    if len(seg) > max_lines:
        truncated = True
        if keep_words:  # keep head, lines mentioning accessed members, tail
            kw = re.compile(r"\b(%s)\b" % "|".join(map(re.escape, sorted(keep_words))))
            keep = set(seg[:3]) | {seg[-1]}
            for i in seg:
                if kw.search(lines[i]):
                    keep.update({i - 1, i} if lines[i - 1].strip().startswith(("/*", "*")) else {i})
            keep = sorted(k for k in keep if s <= k <= e)[:max(max_lines, 12)]
        else:
            keep = seg[:max_lines - 1] + [seg[-1]]
        seg = keep
    out, prev = [], None
    for i in seg:
        if prev is not None and i != prev + 1:
            out.append("        ...")
        out.append("%6d  %s" % (i + 1, lines[i]))
        prev = i
    return "%s:%d" % (tag["path"], s + 1), "\n".join(out), truncated


def enclosing_function(lines, idx):
    for i in range(idx, max(-1, idx - 400), -1):
        l = lines[i]
        mi = INIT_RE.match(l)
        if mi:
            return "%s (struct %s initializer)" % (mi.group(2), mi.group(1))
        if l and l[0] not in " \t#{}/*" and not l.rstrip().endswith(";"):
            m = FUNC_HDR_RE.match(l)
            if m and m.group(1) not in C_KEYWORDS:
                return m.group(1)
        if l.startswith("}") and i != idx:
            return None
    return None


# ---------------------------------------------------------------- bundle model
class Item:
    def __init__(self, full, reduced=None, name=""):
        self.full, self.reduced, self.name = full, reduced, name
        self.state = "full"  # full | reduced | dropped

    def render(self):
        return {"full": self.full, "reduced": self.reduced or "", "dropped": ""}[self.state]


class Section:
    def __init__(self, title, priority, intro=""):
        self.title, self.priority, self.intro, self.items = title, priority, intro, []

    def render(self):
        body = [i.render() for i in self.items if i.render()]
        dropped = sum(1 for i in self.items if i.state == "dropped")
        reduced = sum(1 for i in self.items if i.state == "reduced")
        if not body and not self.intro and not dropped:
            return ""
        s = "## %s\n\n" % self.title
        if self.intro:
            s += self.intro + "\n\n"
        s += "\n\n".join(body)
        if dropped or reduced:
            s += "\n\n_[budget: %d item(s) dropped, %d reduced to signature/short form]_" % (dropped, reduced)
        return s.rstrip() + "\n"


def fence(code, lang="c"):
    return "```%s\n%s\n```" % (lang, code)


# ---------------------------------------------------------------- main build
def build(args):
    """CLI wrapper: print bundle to stdout (or the function list)."""
    try:
        res = build_bundle(args)
    except KBundleError as e:
        sys.exit("kbundle: %s" % e)
    if res is None:
        return
    text, meta = res
    sys.stdout.write(text)
    if args.meta_json:
        with open(args.meta_json, "w") as f:
            json.dump(meta, f, indent=2)
    print("kbundle: %s %s lines=%d tokens~%d risk=%d(%s) model=%s" % (
        meta["file"], meta["function"], meta["lines"], meta["tokens"], meta["risk"]["score"],
        meta["risk"]["level"], meta["model"]), file=sys.stderr)


def make_args(kernel_root, file, function=None, **kw):
    """Build an argparse-like namespace for library use (defaults match the CLI)."""
    d = dict(kernel_root=kernel_root, file=file, function=function, max_tokens=120000, diff=None,
             diff_text=None, static_notes=None, line_hint=None, meta_json=None, list_functions=False,
             arch="x86", max_callers=8, caller_ctx=3, callee_lines=25)
    d.update(kw)
    return argparse.Namespace(**d)


def build_bundle(args):
    """Return (bundle_text, meta) for one function; raises KBundleError."""
    root = os.path.abspath(args.kernel_root)
    rel = os.path.relpath(os.path.abspath(os.path.join(root, args.file)) if not os.path.isabs(args.file)
                          else args.file, root)
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise KBundleError("no such file: %s" % path)
    if args.list_functions:
        for n, s, e in list_functions(path):
            print("%s\t%d\t%d\t%d" % (n, s, e, e - s + 1))
        return None
    loc = find_function(path, args.function, getattr(args, "line_hint", None))
    if not loc:
        raise KBundleError("function %s not found in %s" % (args.function, rel))
    fs, fe = loc
    lines = read_lines(path)
    body_lines = lines[fs - 1:fe]
    nlines = len(body_lines)
    code = strip_comments_strings("\n".join(body_lines))
    code_lines = code.split("\n")

    tools = []
    tools.append("ctags" if is_universal_ctags() else "regex(no universal-ctags)")

    # --- identifiers
    calls, types, members, idents = {}, set(), set(), set()
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", code):
        nm = m.group(1)
        if nm not in C_KEYWORDS and nm != args.function:
            calls[nm] = calls.get(nm, 0) + 1
    for m in re.finditer(r"\b(struct|union|enum)\s+([A-Za-z_]\w*)", code):
        types.add((m.group(1), m.group(2)))
    for m in re.finditer(r"(?:->|\.)\s*([A-Za-z_]\w*)", code):
        members.add(m.group(1))
    for m in re.finditer(r"\b([A-Za-z_]\w*)\b", code):
        nm = m.group(1)
        if nm not in C_KEYWORDS and nm not in members and nm not in calls:
            idents.add(nm)
    want = set(calls) | {t[1] for t in types} | idents
    idx = TagIndex(root, rel, args.arch)
    tags = idx.lookup(want)

    def pick(nm, kinds):
        cands = [t for t in tags.get(nm, []) if t.get("kind") in kinds]
        if not cands:
            return None
        # prefer same file, then same dir, then include/linux, then shortest path
        d = os.path.dirname(rel)
        cands.sort(key=lambda t: (t["path"] != rel, os.path.dirname(t["path"]) != d,
                                  not t["path"].startswith("include/linux"), len(t["path"])))
        return cands[0]

    # --- sections
    sec_target = Section("Target function: `%s` (%s:%d-%d, %d lines)" % (args.function, rel, fs, fe, nlines), 0)
    sec_target.items.append(Item(fence("\n".join("%6d  %s" % (fs + i, l) for i, l in enumerate(body_lines)))))

    signals = riskmod.detect_signals(code_lines, fs)
    rs = riskmod.score(signals, nlines, code)
    sec_risk = Section("Risk signals (auto-detected, heuristic)", 0)
    sec_risk.items.append(Item(riskmod.render_signals(signals, rs)))

    sec_diff = Section("Patch under review", 1)
    dtxt = getattr(args, "diff_text", None)
    if dtxt is None and args.diff:
        dtxt = open(args.diff, encoding="utf-8", errors="replace").read()
    if dtxt:
        # keep only file sections touching target file if any
        parts = re.split(r"(?m)^(?=diff --git )", dtxt)
        mine = [p for p in parts if rel in p.split("\n", 1)[0]]
        dsel = "".join(mine) if mine else dtxt
        dlines = dsel.split("\n")
        sec_diff.items.append(Item(fence(dsel, "diff"),
                                   fence("\n".join(dlines[:200]) + "\n... [diff truncated]", "diff"), "diff"))

    sec_static = Section("Static analysis prepass (checkpatch / sparse / smatch)", 1,
                         "Tool output for lines in or near the target. Confirm or refute; do not just repeat it.")
    for note in (getattr(args, "static_notes", None) or []):
        sec_static.items.append(Item("- " + note, None, "static"))
    if not sec_static.items:
        sec_static.intro = ""

    sec_types = Section("Referenced types and macros (definitions via %s)" % idx.backend, 2)
    seen_defs = set()
    for kw, nm in sorted(types):
        t = pick(nm, {"struct", "union", "enum"})
        if t and (t["path"], t["line"]) not in seen_defs:
            seen_defs.add((t["path"], t["line"]))
            hdr, txt, tr = extract_def(root, t, 60, keep_words=members)
            note = " (large: showing lines with fields used by target)" if tr else ""
            _, small, _ = extract_def(root, t, 15, keep_words=members)
            sec_types.items.append(Item("### %s %s  — %s%s\n%s" % (kw, nm, hdr, note, fence(txt)),
                                        "### %s %s  — %s (reduced)\n%s" % (kw, nm, hdr, fence(small)), nm))
    nested = set()
    for it in list(sec_types.items):
        for m in re.finditer(r"(?m)^\s*\d+\s+\s*(struct|union)\s+(\w+)\s*;", it.full):
            nested.add((m.group(1), m.group(2)))
    if nested - types:
        extra = idx.lookup({n for _, n in nested - types})
        for k, v in extra.items():
            tags.setdefault(k, []).extend(v)
    for kw, nm in sorted(nested - types):
        t = pick(nm, {"struct", "union"})
        if t and (t["path"], t["line"]) not in seen_defs:
            seen_defs.add((t["path"], t["line"]))
            hdr, txt, tr = extract_def(root, t, 40, keep_words=members)
            sec_types.items.append(Item("### %s %s (embedded anonymously above) — %s\n%s" % (kw, nm, hdr, fence(txt)),
                                        None, nm))
    for nm in sorted(idents | set(calls)):
        if riskmod.is_primitive(nm) or nm in SKIP_CALLEES or nm in ATTR_MACROS:
            continue
        t = pick(nm, {"macro", "typedef"})
        if t and (t["path"], t["line"]) not in seen_defs:
            if t["kind"] == "macro" and nm in calls:
                continue  # function-like macro: shown under Callees
            seen_defs.add((t["path"], t["line"]))
            hdr, txt, tr = extract_def(root, t, 20)
            sec_types.items.append(Item("### %s `%s` — %s\n%s" % (t["kind"], nm, hdr, fence(txt)),
                                        "### %s `%s` — %s\n%s" % (t["kind"], nm, hdr,
                                                                  fence(txt.split("\n")[0] + " ...")), nm))

    sec_callees = Section("Callees (signature + truncated body)", 3,
                          "Well-known primitives (locking, alloc, RCU, printk, ...) are omitted; see risk signals.")
    # order: most-called first; least important at the end (trimmed first)
    for nm, cnt in sorted(calls.items(), key=lambda kv: (-kv[1], kv[0])):
        if riskmod.is_primitive(nm) or nm in SKIP_CALLEES or nm in ATTR_MACROS:
            continue
        t = pick(nm, {"function"}) or pick(nm, {"macro"})
        if not t:
            # try same file / tree-wide git grep for a definition
            for p, ln, txt in grep_tree(root, rf"^[A-Za-z].*\b{nm}\s*\([^;]*$", [rel, os.path.dirname(rel) or "."], limit=2):
                if not txt.rstrip().endswith(";"):
                    t = {"name": nm, "path": p, "line": ln, "kind": "function"}
                    break
        if not t:
            proto = pick(nm, {"prototype"})
            if proto:
                sig = "%s:%d  %s" % (proto["path"], proto["line"], proto.get("pattern", "").strip("/^$"))
                sec_callees.items.append(Item("- `%s` (called %dx) — prototype only: `%s`" % (nm, cnt, sig), None, nm))
            continue
        hdr, txt, tr = extract_def(root, t, args.callee_lines)
        first = txt.split("\n")
        sig_lines = []
        for l in first:
            sig_lines.append(l)
            if "{" in l or l.rstrip().endswith((")", ";")) and len(sig_lines) > 0 and "(" in "".join(sig_lines):
                break
        full = "### `%s` (called %dx) — %s%s\n%s" % (nm, cnt, hdr, " [truncated]" if tr else "", fence(txt))
        red = "- `%s` (called %dx) — %s: `%s`" % (nm, cnt, hdr, " ".join(x.split("  ", 1)[-1].strip() for x in sig_lines[:4]))
        sec_callees.items.append(Item(full, red, nm))

    sec_callers = Section("Callers / references (call-site context, capped)", 4)
    if have("cscope") and os.path.exists(os.path.join(root, "cscope.out")):
        tools.append("cscope")
        rc, out = run(["cscope", "-d", "-L", "-0", args.function], cwd=root)  # all refs (calls + fn-ptr uses)
        sites = []
        for l in out.splitlines():
            p = l.split(None, 3)
            if len(p) >= 3 and p[2].isdigit():
                sites.append((p[0], int(p[2]), p[3] if len(p) > 3 else ""))
    else:
        tools.append("git grep" if os.path.isdir(os.path.join(root, ".git")) else "os.walk grep")
        sites = grep_tree(root, None, ["*.c", "*.h"], fixed_word=args.function)
    if True:  # common filtering
        sites = [s for s in sites if not (s[0] == rel and fs <= s[1] <= fe)]
        sites = [s for s in sites if not s[2].rstrip().endswith(";") or re.search(r"\b%s\s*\(" % re.escape(args.function), s[2])]
        # drop prototypes / EXPORT_SYMBOL lines
        sites = [s for s in sites if not re.match(r"^\s*(extern\s+)?[A-Za-z_].*\b%s\s*\([^)]*\)\s*;" % re.escape(args.function), s[2])
                 or re.search(r"=|return|\bif\b", s[2])]
        sites = [s for s in sites if "EXPORT_SYMBOL" not in s[2]]
    total_sites = len(sites)
    # same file first
    sites.sort(key=lambda s: (s[0] != rel, s[0], s[1]))
    for p, ln, txt in sites[:args.max_callers]:
        fl = read_lines(os.path.join(root, p))
        if not fl:
            continue
        a, b = max(0, ln - 1 - args.caller_ctx), min(len(fl), ln + args.caller_ctx)
        encl = enclosing_function(fl, ln - 1) or "?"
        kind = "call" if re.search(r"\b%s\s*\(" % re.escape(args.function), txt) else "reference (e.g. ops table / fn pointer)"
        ctx = "\n".join("%6d%s %s" % (i + 1, ">" if i == ln - 1 else " ", fl[i]) for i in range(a, b))
        sec_callers.items.append(Item("### %s:%d in `%s` — %s\n%s" % (p, ln, encl if " " in encl else encl + "()", kind, fence(ctx)),
                                      "- %s:%d in `%s`: `%s`" % (p, ln, encl, txt.strip()[:160]), p))
    if total_sites > args.max_callers:
        sec_callers.intro = "_%d sites found; showing %d._" % (total_sites, args.max_callers)
    elif total_sites == 0:
        sec_callers.intro = "_No callers found in the checked-out tree (may be called via function pointer or from elsewhere)._"

    sections = [sec_target, sec_risk, sec_diff, sec_static, sec_types, sec_callees, sec_callers]
    model = riskmod.recommend(rs, nlines)

    def header(tok, trimmed):
        return ("# kbundle: `%s()` in %s\n\n"
                "<!-- kbundle-meta: %s -->\n\n"
                "| field | value |\n|---|---|\n"
                "| estimated tokens | ~%d (budget %d, ~%d chars/token) |\n"
                "| function lines | %d |\n| risk score | %d (%s) |\n| recommended model | %s |\n"
                "| resolvers | %s |\n| trimming | %s |\n\n"
                "Everything needed for review is in this bundle. Line numbers refer to %s.\n\n" % (
                    args.function, rel,
                    json.dumps({"file": rel, "function": args.function, "lines": nlines,
                                "start": fs, "end": fe, "risk": rs["score"], "level": rs["level"],
                                "model": model, "tokens": tok}),
                    tok, args.max_tokens, CHARS_PER_TOKEN, nlines, rs["score"], rs["level"], model,
                    ", ".join(tools), trimmed or "none", rel))

    def render_all(tok=0, trimmed=""):
        body = "\n".join(s.render() for s in sections if s.render())
        return header(tok, trimmed) + body

    # --- budget enforcement: callers -> callee bodies -> callee sigs -> types -> diff
    trims = []
    def total():
        t = render_all(budget, "; ".join(trims))
        return est_tokens(t)

    budget = args.max_tokens
    margin = 40  # header text grows as trim notes are appended
    steps = [(sec_callers, "reduced"), (sec_callees, "reduced"), (sec_callers, "dropped"),
             (sec_types, "reduced"), (sec_callees, "dropped"), (sec_types, "dropped"),
             (sec_static, "dropped"), (sec_diff, "reduced"), (sec_diff, "dropped")]
    for sec, st in steps:
        if total() <= budget - margin:
            break
        changed = 0
        for it in reversed(sec.items):  # least important last -> trimmed first
            if total() <= budget - margin:
                break
            if st == "reduced" and it.state == "full" and it.reduced:
                it.state = "reduced"; changed += 1
            elif st == "dropped" and it.state != "dropped":
                it.state = "dropped"; changed += 1
        if changed:
            trims.append("%s→%s(%d)" % (sec.title.split(" (")[0].split(":")[0], st, changed))
    if total() > budget:
        trims.append("TARGET ALONE EXCEEDS BUDGET")
        print("kbundle: warning: target function + risk section exceed budget", file=sys.stderr)
    trimmed = "; ".join(trims)
    tok = est_tokens(render_all(0, trimmed))
    for _ in range(2):  # header contains its own token count; settle it
        tok = est_tokens(render_all(tok, trimmed))
    text = render_all(tok, trimmed)
    meta = {"file": rel, "function": args.function, "lines": nlines, "start": fs, "end": fe,
            "risk": rs, "model": model, "tokens": tok, "budget": budget, "trimmed": trims,
            "resolvers": tools, "notes": idx.notes}
    return text, meta


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kernel_root")
    ap.add_argument("file")
    ap.add_argument("function", nargs="?")
    ap.add_argument("--max-tokens", type=int, default=120000)
    ap.add_argument("--diff")
    ap.add_argument("--line-hint", type=int, help="pick the definition containing this line (duplicate names)")
    ap.add_argument("--meta-json", help="write bundle metadata (risk, model, tokens) as JSON")
    ap.add_argument("--list-functions", action="store_true", help="print name<TAB>start<TAB>end<TAB>lines")
    ap.add_argument("--arch", default="x86")
    ap.add_argument("--max-callers", type=int, default=8)
    ap.add_argument("--caller-ctx", type=int, default=3)
    ap.add_argument("--callee-lines", type=int, default=25)
    a = ap.parse_args()
    a.diff_text = None
    a.static_notes = None
    if not a.list_functions and not a.function:
        ap.error("function name required (or --list-functions)")
    build(a)


if __name__ == "__main__":
    main()
