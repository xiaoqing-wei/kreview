#!/usr/bin/env python3
"""kreview - per-function AI review of Linux kernel code with the Cursor Agent CLI.

  kreview file  <path.c> [--filter REGEX]          review every (matching) function in a file
  kreview func  <path.c> <function>                review one function
  kreview patch <patchfile|git-range>              map hunks -> enclosing functions, review those (diff in bundle)
  kreview report                                   aggregate results/**/*.json -> report.md + report.csv

Common: --root KERNEL_TREE (default $KREVIEW_ROOT or .)  --out results  --dry-run
        --concurrency 2  --timeout 900  --retries 2  --max-requests 40  --max-est-tokens 1500000
Run `kreview <cmd> -h` for all options.  Python 3.8+ stdlib only.
"""
import argparse, concurrent.futures as cf, csv, datetime, glob, hashlib, json, os, random, re, shutil, \
    signal, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kbundle, patchmap, jsonx, static_pass, risk as riskmod  # noqa: E402

PROMPT_FILE = os.environ.get("KREVIEW_PROMPT", os.path.join(HERE, "prompt.md"))
PROMPT_SCHEMA = "2"          # bump when the output schema / prompt semantics change
ARG_LIMIT = 120000           # bytes; Linux MAX_ARG_STRLEN is 131072 per argv string
LONG_CONTEXT_TOKENS = 256000 # Grok 4.7: input above this is billed at 2x
RESULT_SCHEMA = "kreview/1"
USAGE_FIELDS = ["ts", "run_id", "mode", "file", "function", "model", "attempt", "status", "rc", "duration_ms",
                "api_duration_ms", "est_input_tokens", "est_output_tokens", "reported_input_tokens",
                "reported_output_tokens", "reported_cache_read_tokens", "reported_cache_write_tokens", "input_mode", "cache_key", "session_id", "request_id", "note"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def prompt_text():
    with open(PROMPT_FILE, encoding="utf-8") as f:
        return f.read()


def prompt_version():
    return "%s-%s" % (PROMPT_SCHEMA, hashlib.sha256(prompt_text().encode()).hexdigest()[:10])


def safe(s):
    return re.sub(r"[^\w.+-]", "_", s)


# ------------------------------------------------------------------------------- jobs / planning
class Job:
    def __init__(self, mode, file, function, bundle, meta, out_dir, diff=None):
        self.mode, self.file, self.function = mode, file, function
        self.bundle, self.meta, self.out_dir, self.diff = bundle, meta, out_dir, diff
        self.model = meta["model"]
        self.status, self.findings, self.note = "planned", [], ""
        self.attempts, self.cache_key, self.input_mode = 0, None, None
        self.est_in = self.est_out = 0

    @property
    def base(self):
        return os.path.join(self.out_dir, safe(self.function))

    def full_prompt(self):
        return prompt_text() + "\n\n---\n\n" + self.bundle


def route_model(meta, args):
    if args.model:
        return args.model
    lvl = meta["risk"]["level"]
    if lvl == "high":
        return args.model_xhigh
    if lvl == "trivial" and not args.no_low:
        return args.model_low
    return args.model_high


def make_job(args, mode, rel, func, out_dir, line_hint=None, diff=None, static=None):
    kw = dict(max_tokens=args.max_bundle_tokens, arch=args.arch, line_hint=line_hint, diff_text=diff,
              static_notes=None, max_callers=args.max_callers, callee_lines=args.callee_lines)
    if static:
        loc = kbundle.find_function(os.path.join(args.root, rel), func, line_hint)
        if loc:
            kw["static_notes"] = static_pass.notes_for(static, rel, loc[0], loc[1])
    ns = kbundle.make_args(args.root, rel, func, **kw)
    text, meta = kbundle.build_bundle(ns)
    meta["model"] = route_model(meta, args)
    if meta["tokens"] > args.max_bundle_tokens:
        log("warning: %s:%s bundle ~%d tokens exceeds --max-bundle-tokens (target function alone)" % (
            rel, func, meta["tokens"]))
    return Job(mode, rel, func, text, meta, out_dir, diff)


def rel_path(args, p):
    ap = os.path.abspath(p if os.path.isabs(p) else os.path.join(args.root, p))
    if not os.path.isfile(ap) and os.path.isfile(os.path.abspath(p)):
        ap = os.path.abspath(p)
    return os.path.relpath(ap, args.root)


def load_static(args, patch_text=None):
    """Static findings: checkpatch on patch text (if scripts/checkpatch.pl exists) + user-supplied logs."""
    found, notes = [], []
    sdir = os.path.join(args.out, "static")
    if patch_text is not None and not args.no_checkpatch:
        os.makedirs(sdir, exist_ok=True)
        cp, raw = static_pass.run_checkpatch(args.root, patch_text, sdir)
        found += cp
        notes.append("checkpatch: %s" % (("%d finding(s)" % len(cp)) if not raw.startswith(("skipped", "checkpatch failed")) else raw))
        if not raw.startswith("skipped"):
            open(os.path.join(sdir, "checkpatch.txt"), "w").write(raw)
    for tool in ("sparse", "smatch"):
        for lf in getattr(args, tool + "_log") or []:
            items = static_pass.parse_tool_log(open(lf, encoding="utf-8", errors="replace").read(), tool, args.root)
            found += items
            notes.append("%s: %d finding(s) from %s" % (tool, len(items), lf))
    if found:
        os.makedirs(sdir, exist_ok=True)
        json.dump(found, open(os.path.join(sdir, "static.json"), "w"), indent=2)
    for n in notes:
        log("static: " + n)
    return found


def plan_file(args):
    rel = rel_path(args, args.path)
    funcs = kbundle.list_functions(os.path.join(args.root, rel))
    if args.filter:
        rx = re.compile(args.filter)
        funcs = [f for f in funcs if rx.search(f[0])]
    if not funcs:
        sys.exit("kreview: no functions matched in %s" % rel)
    static = load_static(args)
    out_dir = os.path.join(args.out, safe(rel))
    jobs = []
    for n, s, e in funcs:
        try:
            jobs.append(make_job(args, "file", rel, n, out_dir, line_hint=s, static=static))
        except kbundle.KBundleError as ex:
            log("bundle failed: %s: %s" % (n, ex))
    return jobs, {}


def plan_func(args):
    rel = rel_path(args, args.path)
    static = load_static(args)
    try:
        return [make_job(args, "func", rel, args.name, os.path.join(args.out, safe(rel)), static=static)], {}
    except kbundle.KBundleError as ex:
        sys.exit("kreview: %s" % ex)


def plan_patch(args):
    try:
        text, info = patchmap.load_patch(args.root, args.spec)
    except ValueError as ex:
        sys.exit("kreview: %s" % ex)
    res = patchmap.map_patch(args.root, text, side=args.side, info=info)
    log("patch: %s; %d function(s) touched, %d unmapped hunk(s)/file(s)" % (
        res["side_note"], len(res["targets"]), len(res["unmapped"])))
    for u in res["unmapped"]:
        log("  unmapped: %s: %s %s" % (u["file"], u["reason"], u.get("hunk", "")))
    static = load_static(args, text)
    slug = safe(os.path.basename(args.spec) if os.path.isfile(args.spec) else args.spec)[:60]
    jobs = []
    for t in res["targets"]:
        out_dir = os.path.join(args.out, "patch_" + slug, safe(t["file"]))
        try:
            jobs.append(make_job(args, "patch", t["file"], t["function"], out_dir, line_hint=t["line_hint"],
                                 diff=t["diff"], static=static))
        except kbundle.KBundleError as ex:
            log("bundle failed: %s: %s" % (t["function"], ex))
    os.makedirs(args.out, exist_ok=True)
    pm = dict(res); pm["targets"] = [{k: v for k, v in t.items() if k != "diff"} for t in res["targets"]]
    pm["info"] = info
    json.dump(pm, open(os.path.join(args.out, "patch_" + slug + ".map.json"), "w"), indent=2)
    return jobs, {"patch": info, "unmapped": res["unmapped"], "side": res["side"]}


# ------------------------------------------------------------------------------- agent runner
class Budget:
    def __init__(self, max_requests, max_tokens):
        self.max_requests, self.max_tokens = max_requests, max_tokens
        self.requests = self.tokens = 0
        self.lock = threading.Lock()

    def take(self, est_tokens):
        with self.lock:
            if self.requests + 1 > self.max_requests or self.tokens + est_tokens > self.max_tokens:
                return False
            self.requests += 1
            self.tokens += est_tokens
            return True


class UsageLog:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    def _migrate(self):
        """Rewrite an existing usage.csv whose header predates the current column set."""
        if not os.path.exists(self.path):
            return
        with open(self.path, newline="") as f:
            rd = csv.reader(f)
            hdr = next(rd, None)
            rows = list(rd)
        if hdr == USAGE_FIELDS:
            return
        with open(self.path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=USAGE_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: v for k, v in zip(hdr or [], r) if k in USAGE_FIELDS})

    def write(self, row):
        with self.lock:
            if not getattr(self, "_checked", False):
                self._migrate(); self._checked = True
            new = not os.path.exists(self.path)
            with open(self.path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=USAGE_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow({k: row.get(k, "") for k in USAGE_FIELDS})


def choose_input_mode(args, nbytes):
    if args.input_mode != "auto":
        return args.input_mode
    if nbytes < ARG_LIMIT:
        return "arg"
    return "stdin"  # verified with agent 2026.10.01 (see README); file mode is the fallback


def agent_cmd(args, model, ws):
    cmd = [args.agent_bin, "-p", "--model", model, "--mode", "ask", "--output-format", "json",
           "--trust", "--workspace", ws]
    return cmd + (args.agent_arg or [])


def reported_tokens(env):
    """Token counts from the CLI envelope. agent 2026.10.01 emits
    "usage": {"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"} (undocumented)."""
    if not isinstance(env, dict):
        return "", "", "", ""
    u = env.get("usage") or env.get("token_usage") or {}
    if not isinstance(u, dict):
        return "", "", "", ""
    g = lambda *ks: next((u[k] for k in ks if k in u), "")
    return (g("inputTokens", "input_tokens", "prompt_tokens"), g("outputTokens", "output_tokens", "completion_tokens"),
            g("cacheReadTokens", "cache_read_tokens"), g("cacheWriteTokens", "cache_write_tokens"))


def run_agent_once(args, job, mode):
    """Returns (rc, stdout, stderr, duration_ms, timed_out)."""
    ws = tempfile.mkdtemp(prefix="kreview.")
    try:
        with open(os.path.join(ws, "bundle.md"), "w") as f:
            f.write(job.bundle)
        cmd = agent_cmd(args, job.model, ws)
        stdin_data = None
        if mode == "arg":
            cmd.append(job.full_prompt())
        elif mode == "stdin":
            stdin_data = job.full_prompt()
        else:  # file
            cmd.append(prompt_text() + "\n\nThe bundle is the file bundle.md in the workspace root. "
                       "Read that ONE file and nothing else.")
        t0 = time.time()
        p = subprocess.Popen(cmd, cwd=ws, stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                             start_new_session=True)
        try:
            out, err = p.communicate(stdin_data, timeout=args.timeout)
            return p.returncode, out, err, int((time.time() - t0) * 1000), False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            out, err = p.communicate()
            return 124, out or "", (err or "") + "\n[kreview] timeout after %ss" % args.timeout, \
                int((time.time() - t0) * 1000), True
    finally:
        shutil.rmtree(ws, ignore_errors=True)


FATAL_RX = re.compile(r"Authentication required|not authenticated|invalid api key|unknown model|"
                      r"model .* (not found|not available|invalid)|Cannot use this model", re.I)
RATE_RX = re.compile(r"rate.?limit|429|too many requests|resource.?exhausted|overloaded", re.I)


class Runner:
    def __init__(self, args, run_id):
        self.args, self.run_id = args, run_id
        self.budget = Budget(args.max_requests, args.max_est_tokens)
        self.usage = UsageLog(os.path.join(args.out, "usage.csv"))
        self.cache_dir = args.cache_dir or os.path.join(args.out, ".cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.fatal = None
        self.pv = prompt_version()

    def key(self, job):
        h = hashlib.sha256()
        for part in (job.bundle, job.model, self.pv):
            h.update(part.encode()); h.update(b"\0")
        return h.hexdigest()[:24]

    def save(self, job, extra=None):
        os.makedirs(job.out_dir, exist_ok=True)
        rec = {"schema": RESULT_SCHEMA, "mode": job.mode, "file": job.file, "function": job.function,
               "start": job.meta["start"], "end": job.meta["end"], "model": job.model, "status": job.status,
               "note": job.note, "risk": job.meta["risk"]["score"], "level": job.meta["risk"]["level"],
               "bundle_tokens": job.meta["tokens"], "prompt_version": self.pv, "cache_key": job.cache_key,
               "attempts": job.attempts, "input_mode": job.input_mode, "run_id": self.run_id,
               "usage": getattr(job, "reported", None),
               "findings": job.findings}
        rec.update(extra or {})
        json.dump(rec, open(job.base + ".json", "w"), indent=2)

    def review(self, job):
        a = self.args
        job.cache_key = self.key(job)
        cpath = os.path.join(self.cache_dir, job.cache_key + ".json")
        if not a.no_cache and os.path.exists(cpath):
            c = json.load(open(cpath))
            job.findings, job.status, job.note = c["findings"], "ok", "cache hit"
            job.input_mode = c.get("input_mode")
            self.save(job, {"cached": True})
            self.usage.write({"ts": now(), "run_id": self.run_id, "mode": job.mode, "file": job.file,
                              "function": job.function, "model": job.model, "attempt": 0, "status": "cache-hit",
                              "cache_key": job.cache_key, "duration_ms": 0, "est_input_tokens": 0})
            return job
        full = job.full_prompt()
        mode = choose_input_mode(a, len(full.encode()))
        job.input_mode = mode
        job.est_in = kbundle.est_tokens(full) + a.overhead_tokens
        for attempt in range(1, a.retries + 2):
            if self.fatal:
                job.status, job.note = "aborted", "fatal error earlier in run: " + self.fatal
                break
            if not self.budget.take(job.est_in + a.est_output_tokens):
                job.status, job.note = "skipped-budget", "max requests / max estimated tokens reached"
                break
            job.attempts = attempt
            rc, out, err, dur, timed_out = run_agent_once(a, job, mode)
            res_text, env, status, note = "", None, "ok", ""
            if rc == 0:
                try:
                    res_text, env = jsonx.parse_envelope(out)
                    if env and env.get("is_error"):
                        raise jsonx.ExtractError("agent reported is_error")
                    job.findings = jsonx.extract_findings(res_text)
                except jsonx.ExtractError as ex:
                    status, note = "parse-error", str(ex)
            else:
                status = "timeout" if timed_out else "agent-error"
                note = (err.strip().splitlines() or ["rc=%d" % rc])[-1][:300]
            est_out = kbundle.est_tokens(res_text or out or "")
            ri, ro, rcr, rcw = reported_tokens(env)
            self.usage.write({"ts": now(), "run_id": self.run_id, "mode": job.mode, "file": job.file,
                              "function": job.function, "model": job.model, "attempt": attempt, "status": status,
                              "rc": rc, "duration_ms": dur, "api_duration_ms": (env or {}).get("duration_api_ms", ""),
                              "est_input_tokens": job.est_in, "est_output_tokens": est_out,
                              "reported_input_tokens": ri, "reported_output_tokens": ro,
                              "reported_cache_read_tokens": rcr, "reported_cache_write_tokens": rcw, "input_mode": mode,
                              "cache_key": job.cache_key, "session_id": (env or {}).get("session_id", ""),
                              "request_id": (env or {}).get("request_id", ""), "note": note})
            job.status, job.note = status, note
            job.reported = {"input": ri, "output": ro, "cache_read": rcr, "cache_write": rcw,
                            "duration_ms": dur, "session_id": (env or {}).get("session_id", "")}
            if status == "ok":
                json.dump({"findings": job.findings, "model": job.model, "input_mode": mode,
                           "prompt_version": self.pv, "ts": now()}, open(cpath, "w"))
                if a.keep_raw:
                    open(job.base + ".raw.json", "w").write(out)
                break
            # failure: always keep raw output for inspection
            os.makedirs(job.out_dir, exist_ok=True)
            open(job.base + ".raw.txt", "w").write("# rc=%s status=%s\n# stderr:\n%s\n# stdout:\n%s" % (rc, status, err, out))
            if FATAL_RX.search(err + out):
                self.fatal = note or "fatal agent error"
                break
            if status == "parse-error" and not a.retry_parse:
                break
            if attempt <= a.retries:
                delay = min(120, a.backoff * (2 ** (attempt - 1))) * (2 if RATE_RX.search(err + out) else 1)
                delay *= 0.8 + 0.4 * random.random()
                log("  retry %s:%s in %.0fs (%s)" % (job.file, job.function, delay, note))
                time.sleep(delay)
        self.save(job)
        return job


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_bundles(jobs):
    for j in jobs:
        os.makedirs(j.out_dir, exist_ok=True)
        open(j.base + ".bundle.md", "w").write(j.bundle)
        json.dump(j.meta, open(j.base + ".meta.json", "w"), indent=2)


def execute(args, jobs, ctx):
    os.makedirs(args.out, exist_ok=True)
    write_bundles(jobs)
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    pv = prompt_version()
    # priority: highest risk first so budget caps drop the least interesting functions
    jobs.sort(key=lambda j: (-j.meta["risk"]["score"], j.file, j.meta["start"]))
    total_est, rows = 0, []
    runner = Runner(args, run_id) if not args.dry_run else None
    for j in jobs:
        full = j.full_prompt()
        j.input_mode = choose_input_mode(args, len(full.encode()))
        j.est_in = kbundle.est_tokens(full) + args.overhead_tokens
        total_est += j.est_in + args.est_output_tokens
        flag = "  [>256K input: billed 2x]" if j.est_in > LONG_CONTEXT_TOKENS else ""
        rows.append("%-28s %-34s lines=%-4d risk=%-3d %-8s model=%-16s ~%dk tok in=%s%s" % (
            j.file[-28:], j.function[:34], j.meta["lines"], j.meta["risk"]["score"], j.meta["risk"]["level"],
            j.model, round(j.est_in / 1000), j.input_mode, flag))
    log("\n".join(rows))
    log("plan: %d call(s), est. ~%d tokens (incl. %d overhead + %d output per call); caps: %d requests, %d tokens" % (
        len(jobs), total_est, args.overhead_tokens, args.est_output_tokens, args.max_requests, args.max_est_tokens))
    if len(jobs) > args.max_requests or total_est > args.max_est_tokens:
        log("note: plan exceeds caps; lowest-risk functions will be skipped (raise --max-requests/--max-est-tokens)")
    plan = {"run_id": run_id, "prompt_version": pv, "dry_run": args.dry_run, "context": ctx,
            "est_total_tokens": total_est, "jobs": [{"file": j.file, "function": j.function, "model": j.model,
                                                    "risk": j.meta["risk"]["score"], "level": j.meta["risk"]["level"],
                                                    "bundle_tokens": j.meta["tokens"], "est_input_tokens": j.est_in,
                                                    "input_mode": j.input_mode, "bundle": j.base + ".bundle.md",
                                                    "cmd": agent_cmd(args, j.model, "<tmpdir>") + ["<prompt via %s>" % j.input_mode]}
                                                   for j in jobs]}
    json.dump(plan, open(os.path.join(args.out, "plan.json"), "w"), indent=2)
    if args.dry_run:
        log("dry-run: bundles + plan.json written to %s; no agent calls made" % args.out)
        return 0
    if not shutil.which(args.agent_bin):
        sys.exit("kreview: agent binary '%s' not found (install Cursor CLI or use --dry-run)" % args.agent_bin)
    if not os.environ.get("CURSOR_API_KEY"):
        log("warning: CURSOR_API_KEY not set; relying on `agent login` credentials")
    with cf.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(runner.review, j): j for j in jobs}
        for f in cf.as_completed(futs):
            j = f.result()
            log("%-9s %s:%s model=%s findings=%d %s" % (j.status, j.file, j.function, j.model, len(j.findings),
                                                        ("(" + j.note + ")") if j.note else ""))
    st = {}
    for j in jobs:
        st[j.status] = st.get(j.status, 0) + 1
    log("done: %s; requests=%d est_tokens=%d; usage -> %s" % (
        ", ".join("%s=%d" % kv for kv in sorted(st.items())), runner.budget.requests, runner.budget.tokens,
        os.path.join(args.out, "usage.csv")))
    if runner.fatal:
        log("FATAL: " + runner.fatal)
        return 2
    return 0 if all(j.status in ("ok",) for j in jobs) else 1


# ------------------------------------------------------------------------------- report
def load_results(out):
    recs = []
    for p in sorted(glob.glob(os.path.join(out, "**", "*.json"), recursive=True)):
        b = os.path.basename(p)
        if b.endswith((".meta.json", ".raw.json", ".map.json")) or b in ("plan.json", "static.json") \
                or "/.cache/" in p:
            continue
        try:
            d = json.load(open(p))
        except (ValueError, OSError):
            continue
        if isinstance(d, list):  # legacy review.sh output: bare array in results/<file_>/<func>.json
            d = {"schema": "legacy", "file": os.path.basename(os.path.dirname(p)), "function": b[:-5],
                 "model": "?", "status": "ok", "findings": d, "mode": "legacy"}
        if isinstance(d, dict) and "findings" in d:
            d["_path"] = p
            recs.append(d)
    return recs


def cmd_report(args):
    recs = load_results(args.out)
    merged = {}
    for r in recs:
        for f in r.get("findings", []):
            f = jsonx.normalize(f)
            if f["confidence"] < args.min_confidence:
                continue
            k = (r["file"], f["category"], f["line"])
            # exact dup: same file+category+line. Near dup (+/-1 line) only across different result
            # files (e.g. the same bug seen from a caller and a callee review, or file + patch runs);
            # two findings on adjacent lines in ONE answer are kept separate.
            if k not in merged:
                for dl in (-1, 1):
                    kk = (r["file"], f["category"], f["line"] + dl)
                    if kk in merged and r["_path"] not in merged[kk]["_paths"]:
                        k = kk
                        break
            e = merged.get(k)
            src = "%s()@%s" % (r["function"], r.get("model", "?"))
            if e is None:
                merged[k] = dict(f, file=r["file"], function=r["function"], sources=[src], tool="ai", dupes=1,
                                 _paths={r["_path"]})
            else:
                e["dupes"] += 1
                e["_paths"].add(r["_path"])
                if src not in e["sources"]:
                    e["sources"].append(src)
                if (jsonx.SEV_RANK[f["severity"]], -f["confidence"]) < (jsonx.SEV_RANK[e["severity"]], -e["confidence"]):
                    e.update(severity=f["severity"], confidence=f["confidence"], rationale=f["rationale"],
                             function=r["function"])
    static = []
    sp = os.path.join(args.out, "static", "static.json")
    if os.path.exists(sp) and not args.no_static:
        for s in json.load(open(sp)):
            static.append({"file": s["file"], "line": s["line"], "severity": "low" if s["kind"] in ("check", "info", "warning", "warn") else "medium",
                           "category": "static:" + s["tool"], "rationale": s["msg"], "confidence": 1.0,
                           "function": "", "sources": [s["tool"]], "tool": s["tool"], "dupes": 1})
        seen, uniq = set(), []
        for s in static:
            k = (s["file"], s["line"], s["rationale"])
            if k not in seen:
                seen.add(k); uniq.append(s)
        static = uniq
    rows = sorted(list(merged.values()) + static,
                  key=lambda f: (jsonx.SEV_RANK.get(f["severity"], 9), -f["confidence"], f["file"], f["line"]))
    os.makedirs(args.out, exist_ok=True)
    csv_path = os.path.join(args.out, "report.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["severity", "confidence", "file", "line", "function", "category", "rationale", "sources", "dupes"])
        for f in rows:
            w.writerow([f["severity"], f["confidence"], f["file"], f["line"], f["function"], f["category"],
                        f["rationale"], ";".join(f["sources"]), f["dupes"]])
    # usage summary
    usage = {"calls": 0, "cache_hits": 0, "est_in": 0, "est_out": 0, "ms": 0, "by_model": {}, "rep_in": 0, "rep_out": 0, "rep_cr": 0}
    up = os.path.join(args.out, "usage.csv")
    if os.path.exists(up):
        for u in csv.DictReader(open(up)):
            if u["status"] == "cache-hit":
                usage["cache_hits"] += 1; continue
            usage["calls"] += 1
            usage["est_in"] += int(u["est_input_tokens"] or 0); usage["est_out"] += int(u["est_output_tokens"] or 0)
            usage["ms"] += int(u["duration_ms"] or 0)
            usage["by_model"][u["model"]] = usage["by_model"].get(u["model"], 0) + 1
            for k, c in (("rep_in", "reported_input_tokens"), ("rep_out", "reported_output_tokens"),
                         ("rep_cr", "reported_cache_read_tokens")):
                try:
                    usage[k] += int(u.get(c) or 0)
                except ValueError:
                    pass
    sev_counts = {}
    for f in rows:
        sev_counts[f["severity"]] = sev_counts.get(f["severity"], 0) + 1
    failed = [r for r in recs if r.get("status") not in ("ok",)]
    md = ["# kreview report", "",
          "Generated %s from `%s` (%d function result(s), %d failed/skipped)." % (now(), args.out, len(recs), len(failed)), "",
          "| severity | count |", "|---|---|"]
    md += ["| %s | %d |" % (s, sev_counts.get(s, 0)) for s in jsonx.SEVERITIES if sev_counts.get(s)]
    md += ["", "**Usage:** %d agent call(s) (%s), %d kreview cache hit(s), %.1f min wall time. %s" % (
        usage["calls"], ", ".join("%s×%d" % kv for kv in sorted(usage["by_model"].items())) or "-", usage["cache_hits"],
        usage["ms"] / 60000.0,
        ("CLI-reported tokens: %d input + %d cache-read, %d output (incl. reasoning)." % (
            usage["rep_in"], usage["rep_cr"], usage["rep_out"])) if usage["rep_in"] else
        ("Estimated %d input tokens (chars/4 + overhead); the CLI reported no usage." % usage["est_in"])),
        "", "_Billed usage is authoritative only in the Cursor dashboard._", ""]
    if rows:
        md += ["## Findings (sorted by severity, deduplicated)", "",
               "| # | sev | conf | location | function | category | summary |", "|---|---|---|---|---|---|---|"]
        for i, f in enumerate(rows, 1):
            summ = f["rationale"].replace("|", "\\|").replace("\n", " ")
            md.append("| %d | %s | %.2f | `%s:%d` | %s | %s | %s |" % (
                i, f["severity"], f["confidence"], f["file"], f["line"], f["function"] or "-", f["category"],
                summ if len(summ) <= 160 else summ[:157] + "..."))
        md += ["", "## Details", ""]
        for i, f in enumerate([r for r in rows if r["tool"] == "ai"], 1):
            md += ["### %d. [%s] %s:%d `%s()` — %s (confidence %.2f)" % (
                i, f["severity"], f["file"], f["line"], f["function"], f["category"], f["confidence"]), "",
                f["rationale"], "", "_sources: %s%s_" % (", ".join(f["sources"]),
                                                        (", reported %dx" % f["dupes"]) if f["dupes"] > 1 else ""), ""]
    else:
        md += ["_No findings._", ""]
    clean = [r for r in recs if r.get("status") == "ok" and not r.get("findings")]
    if clean:
        md += ["## Reviewed with no findings", "", ", ".join("`%s:%s`" % (r["file"], r["function"]) for r in clean), ""]
    if failed:
        md += ["## Failed / skipped", "", "| file | function | status | note |", "|---|---|---|---|"]
        md += ["| %s | %s | %s | %s |" % (r["file"], r["function"], r.get("status"), (r.get("note") or "").replace("|", "/"))
               for r in failed]
        md.append("")
    md_path = os.path.join(args.out, "report.md")
    open(md_path, "w").write("\n".join(md))
    log("report: %d finding(s) (%d AI, %d static) -> %s, %s" % (len(rows), len(merged), len(static), md_path, csv_path))
    return 0


# ------------------------------------------------------------------------------- CLI
def env_int(name, d):
    try:
        return int(os.environ.get(name, d))
    except ValueError:
        return d


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("common")
    g.add_argument("--root", default=os.environ.get("KREVIEW_ROOT", "."), help="kernel tree (default $KREVIEW_ROOT or .)")
    g.add_argument("--out", default=os.environ.get("KREVIEW_OUT", "results"), help="results dir (default ./results)")

    run = argparse.ArgumentParser(add_help=False)
    r = run.add_argument_group("review run")
    r.add_argument("--dry-run", action="store_true", help="build bundles + plan.json, print commands; no agent calls")
    r.add_argument("--concurrency", "-j", type=int, default=env_int("KREVIEW_CONCURRENCY", 2))
    r.add_argument("--timeout", type=int, default=env_int("KREVIEW_TIMEOUT", 900), help="per-call timeout, seconds")
    r.add_argument("--retries", type=int, default=2, help="retries per call on agent error/timeout (default 2)")
    r.add_argument("--backoff", type=float, default=10.0, help="initial backoff seconds (doubles; x2 on rate limits)")
    r.add_argument("--retry-parse", action="store_true", help="also retry when the answer has no parsable JSON")
    r.add_argument("--max-requests", type=int, default=env_int("KREVIEW_MAX_REQUESTS", 40),
                   help="max agent calls per run, retries included (default 40)")
    r.add_argument("--max-est-tokens", type=int, default=env_int("KREVIEW_MAX_EST_TOKENS", 1500000),
                   help="max estimated tokens per run (default 1.5M)")
    r.add_argument("--max-bundle-tokens", type=int, default=env_int("KREVIEW_MAX_BUNDLE_TOKENS", 60000),
                   help="kbundle budget per function (default 60000)")
    r.add_argument("--overhead-tokens", type=int, default=env_int("KREVIEW_OVERHEAD_TOKENS", 22000),
                   help="estimated agent system-prompt/tool overhead per call (default 22000, measured)")
    r.add_argument("--est-output-tokens", type=int, default=6000, help="estimated output tokens per call for planning")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--cache-dir", default=os.environ.get("KREVIEW_CACHE"))
    r.add_argument("--keep-raw", action="store_true", help="also save raw agent output for successful calls")
    r.add_argument("--agent-bin", default=os.environ.get("AGENT_BIN", "agent"))
    r.add_argument("--agent-arg", action="append", help="extra arg passed to agent (repeatable), e.g. --agent-arg=--sandbox=enabled")
    r.add_argument("--input-mode", choices=["auto", "arg", "stdin", "file"], default=os.environ.get("INPUT_MODE", "auto"))
    r.add_argument("--model", help="force one model for every function")
    r.add_argument("--model-low", default=os.environ.get("KREVIEW_MODEL_LOW", "grok-4.7-low"))
    r.add_argument("--model-high", default=os.environ.get("KREVIEW_MODEL_HIGH", "grok-4.7-high"))
    r.add_argument("--model-xhigh", default=os.environ.get("KREVIEW_MODEL_XHIGH", "grok-4.7-xhigh"))
    r.add_argument("--no-low", action="store_true", help="never route trivial functions to the low model")
    r.add_argument("--arch", default="x86")
    r.add_argument("--max-callers", type=int, default=8)
    r.add_argument("--callee-lines", type=int, default=25)
    r.add_argument("--sparse-log", action="append", help="sparse output (make C=1 ...) to attach to bundles/report")
    r.add_argument("--smatch-log", action="append", help="smatch output to attach to bundles/report")
    r.add_argument("--no-checkpatch", action="store_true", help="patch mode: skip scripts/checkpatch.pl")

    ap = argparse.ArgumentParser(prog="kreview", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("file", parents=[common, run], help="review all functions in a file")
    p.add_argument("path"); p.add_argument("--filter", help="regex on function names")
    p = sub.add_parser("func", parents=[common, run], help="review one function")
    p.add_argument("path"); p.add_argument("name")
    p = sub.add_parser("patch", parents=[common, run], help="review functions touched by a patch or git range")
    p.add_argument("spec", help="patch file, git range A..B, or a single commit")
    p.add_argument("--side", choices=["auto", "new", "old"], default="auto",
                   help="which side of the diff the tree holds (auto-detected via git)")
    p = sub.add_parser("report", parents=[common], help="aggregate results into report.md + report.csv")
    p.add_argument("--min-confidence", type=float, default=0.0)
    p.add_argument("--no-static", action="store_true", help="leave static-analysis findings out of the report")
    args = ap.parse_args(argv)
    args.root = os.path.abspath(args.root)
    args.out = os.path.abspath(args.out)
    if args.cmd == "report":
        return cmd_report(args)
    if not os.path.isdir(args.root):
        sys.exit("kreview: --root %s is not a directory" % args.root)
    jobs, ctx = {"file": plan_file, "func": plan_func, "patch": plan_patch}[args.cmd](args)
    if not jobs:
        log("nothing to review")
        return 0
    rc = execute(args, jobs, ctx)
    if not args.dry_run:
        cmd_report(argparse.Namespace(out=args.out, min_confidence=0.0, no_static=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
