#!/usr/bin/env python3
"""Offline tests: kbundle + patch mapping on the sparse tree, jsonx, static parsers, full dry-run,
and the runner/report against a fake agent (no Cursor calls, no cost).

  KREVIEW_TEST_ROOT=/path/to/linux python3 -m unittest discover -s tests -v
"""
import csv, json, os, shutil, subprocess, sys, tempfile, unittest, warnings

warnings.simplefilter("ignore", ResourceWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path.insert(0, TOP)
import kbundle, patchmap, jsonx, static_pass, kreview  # noqa: E402

SRC = os.environ.get("KREVIEW_TEST_ROOT", "/workspace/ksrc/linux")
FAKE = os.path.join(HERE, "fake_agent.py")

PATCH = """diff --git a/fs/pipe.c b/fs/pipe.c
--- a/fs/pipe.c
+++ b/fs/pipe.c
@@ -88,5 +88,6 @@ void pipe_lock(struct pipe_inode_info *pipe)
 void pipe_lock(struct pipe_inode_info *pipe)
 {
 	if (pipe->files)
 		mutex_lock(&pipe->mutex);
+	/* kreview test */
 }
"""


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


@unittest.skipUnless(os.path.isfile(os.path.join(SRC, "fs/pipe.c")), "sparse kernel tree not found")
class TreeTests(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kreview-test.")
        cls.root = os.path.join(cls.tmp, "linux")
        # copy the working tree into a fresh repo (the sparse tree may be a partial clone, so no `git clone`)
        shutil.copytree(SRC, cls.root, ignore=shutil.ignore_patterns(".git", "cscope.*", "tags"))
        for c in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "kreview test"],
                  ["add", "-A"], ["commit", "-qm", "base"]):
            r = sh(["git", "-C", cls.root] + c)
            assert r.returncode == 0, r.stderr
        # build a realistic patch: edit inside pipe_lock, free_pipe_info, and a top-level macro
        p = os.path.join(cls.root, "fs/pipe.c")
        src = open(p).read().split("\n")
        loc_fp = kbundle.find_function(p, "free_pipe_info")
        cls.orig = list(src)
        src[loc_fp[0] + 2] = src[loc_fp[0] + 2] + "  /* kreview: touched */"
        loc_pl = kbundle.find_function(p, "pipe_lock")
        src.insert(loc_pl[1] - 1, "\t/* kreview: added in pipe_lock */")
        for i, l in enumerate(src):  # a hunk outside any function
            if l.startswith("#define") or l.startswith("unsigned int pipe_max_size"):
                src[i] = l + " /* top-level */"
                break
        open(p, "w").write("\n".join(src))
        cls.patch_text = sh(["git", "-C", cls.root, "diff", "-U3"]).stdout
        sh(["git", "-C", cls.root, "commit", "-qam", "test change"])
        cls.patch_file = os.path.join(cls.tmp, "t.patch")
        open(cls.patch_file, "w").write(cls.patch_text)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_list_functions(self):
        fns = {n: (s, e) for n, s, e in kbundle.list_functions(os.path.join(SRC, "fs/pipe.c"))}
        self.assertEqual(fns["pipe_lock"], (88, 92))
        self.assertIn("sys_pipe2", fns)  # SYSCALL_DEFINE special case
        self.assertIn("anon_pipe_read", fns)

    def test_bundle_contents(self):
        text, meta = kbundle.build_bundle(kbundle.make_args(SRC, "fs/pipe.c", "pipe_lock"))
        self.assertIn("88  void pipe_lock(struct pipe_inode_info *pipe)", text)
        self.assertIn("struct pipe_inode_info", text)
        self.assertIn("kbundle-meta", text)
        self.assertEqual((meta["start"], meta["end"]), (88, 92))
        self.assertIn(meta["risk"]["level"], ("trivial", "low", "medium", "high"))

    def test_bundle_budget_trims(self):
        text, meta = kbundle.build_bundle(kbundle.make_args(SRC, "fs/pipe.c", "anon_pipe_read", max_tokens=2500))
        self.assertTrue(meta["trimmed"])
        self.assertIn("480  }", text)  # the target is never cut

    def test_diff_parse(self):
        files = patchmap.parse_diff(PATCH)
        self.assertEqual(len(files), 1)
        h = files[0].hunks[0]
        self.assertEqual(files[0].path, "fs/pipe.c")
        self.assertEqual(h.new_changed, [92])
        self.assertEqual(h.old_changed, [92])

    def test_patch_map_post_image(self):
        res = patchmap.map_patch(self.root, self.patch_text, info={"source": "file"})
        self.assertEqual(res["side"], "new")
        names = sorted(t["function"] for t in res["targets"])
        self.assertEqual(names, ["free_pipe_info", "pipe_lock"])
        self.assertTrue(any("outside any function" in u["reason"] for u in res["unmapped"]))
        for t in res["targets"]:
            self.assertIn("diff --git a/fs/pipe.c", t["diff"])
            self.assertIn("kreview", t["diff"])

    def test_patch_map_pre_image(self):
        res = patchmap.map_patch(SRC, self.patch_text, info={"source": "file"})  # original tree = pre-image
        self.assertEqual(res["side"], "old")
        self.assertEqual(sorted(t["function"] for t in res["targets"]), ["free_pipe_info", "pipe_lock"])

    def test_git_range(self):
        txt, info = patchmap.load_patch(self.root, "HEAD~1..HEAD")
        res = patchmap.map_patch(self.root, txt, info=info)
        self.assertEqual(res["side"], "new")
        self.assertIn("HEAD == range end", res["side_note"])
        self.assertEqual(sorted(t["function"] for t in res["targets"]), ["free_pipe_info", "pipe_lock"])
        _, info2 = patchmap.load_patch(self.root, "HEAD")  # single commit
        self.assertEqual(info2["spec"], "HEAD^..HEAD")

    def _run(self, *argv, env=None):
        e = dict(os.environ, **(env or {}))
        return sh([sys.executable, os.path.join(TOP, "kreview.py")] + list(argv), env=e)

    def test_full_dry_run_file(self):
        out = os.path.join(self.tmp, "dry1")
        r = self._run("file", "--root", SRC, "fs/pipe.c", "--out", out, "--dry-run", "--agent-bin", "/nonexistent")
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.load(open(os.path.join(out, "plan.json")))
        n = len(kbundle.list_functions(os.path.join(SRC, "fs/pipe.c")))
        self.assertEqual(len(plan["jobs"]), n)
        self.assertTrue(plan["dry_run"])
        risks = [j["risk"] for j in plan["jobs"]]
        self.assertEqual(risks, sorted(risks, reverse=True))
        self.assertTrue(all(os.path.exists(j["bundle"]) for j in plan["jobs"]))
        self.assertFalse(os.path.exists(os.path.join(out, "usage.csv")))
        models = {j["model"] for j in plan["jobs"]}
        self.assertTrue(models <= {"grok-4.7-low", "grok-4.7-high", "grok-4.7-xhigh"})

    def test_full_dry_run_patch(self):
        out = os.path.join(self.tmp, "dry2")
        r = self._run("patch", "--root", self.root, "HEAD~1..HEAD", "--out", out, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.load(open(os.path.join(out, "plan.json")))
        self.assertEqual(sorted(j["function"] for j in plan["jobs"]), ["free_pipe_info", "pipe_lock"])
        b = open(plan["jobs"][0]["bundle"]).read()
        self.assertIn("## Patch under review", b)
        self.assertIn("checkpatch", r.stderr)  # reported as skipped (no scripts/ in sparse tree)

    def test_runner_fake_agent(self):
        out = os.path.join(self.tmp, "run1")
        log = os.path.join(self.tmp, "fake.log")
        env = {"FAKE_AGENT_LOG": log}
        args = ["file", "--root", SRC, "fs/pipe.c", "--filter", "^pipe_(lock|unlock|double_lock)$", "--out", out,
                "--agent-bin", FAKE, "-j", "3", "--backoff", "0.1"]
        r = self._run(*args, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(open(log).read().splitlines()), 3)
        res = json.load(open(os.path.join(out, "fs_pipe.c", "pipe_lock.json")))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["findings"][0]["severity"], "high")
        self.assertEqual(res["usage"]["output"], 100)
        rows = list(csv.DictReader(open(os.path.join(out, "usage.csv"))))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(int(x["reported_input_tokens"]) > 20000 for x in rows))
        # second run: all cache hits, no new agent calls
        r = self._run(*args, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(open(log).read().splitlines()), 3)
        self.assertIn("cache hit", r.stderr)
        # report: sorted by severity, shared finding deduped
        rep = list(csv.DictReader(open(os.path.join(out, "report.csv"))))
        sev = [jsonx.SEV_RANK[x["severity"]] for x in rep]
        self.assertEqual(sev, sorted(sev))
        dup = [x for x in rep if x["rationale"] == "shared dup"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["dupes"], "3")
        self.assertIn("# kreview report", open(os.path.join(out, "report.md")).read())

    def test_runner_stdin_retry_budget_parse(self):
        out = os.path.join(self.tmp, "run2")
        flag = os.path.join(self.tmp, "flag")
        r = self._run("func", "--root", SRC, "fs/pipe.c", "pipe_lock", "--out", out, "--agent-bin", FAKE,
                      "--input-mode", "stdin", "--backoff", "0.1",
                      env={"FAKE_AGENT_MODE": "fail_once", "FAKE_AGENT_FLAG": flag})
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = list(csv.DictReader(open(os.path.join(out, "usage.csv"))))
        self.assertEqual([x["status"] for x in rows], ["agent-error", "ok"])
        self.assertEqual(rows[1]["input_mode"], "stdin")
        # budget: 3 functions, max 1 request -> 2 skipped
        out3 = os.path.join(self.tmp, "run3")
        r = self._run("file", "--root", SRC, "fs/pipe.c", "--filter", "^pipe_(lock|unlock|double_lock)$",
                      "--out", out3, "--agent-bin", FAKE, "--max-requests", "1")
        self.assertEqual(r.returncode, 1)
        st = [json.load(open(os.path.join(out3, "fs_pipe.c", f)))["status"]
              for f in os.listdir(os.path.join(out3, "fs_pipe.c")) if f.endswith(".json") and ".meta." not in f]
        self.assertEqual(sorted(st), ["ok", "skipped-budget", "skipped-budget"])
        # token cap
        out4 = os.path.join(self.tmp, "run4")
        r = self._run("func", "--root", SRC, "fs/pipe.c", "pipe_lock", "--out", out4, "--agent-bin", FAKE,
                      "--max-est-tokens", "1000")
        self.assertIn("skipped-budget", r.stderr)
        # parse error -> raw saved, not retried
        out5 = os.path.join(self.tmp, "run5")
        r = self._run("func", "--root", SRC, "fs/pipe.c", "pipe_lock", "--out", out5, "--agent-bin", FAKE,
                      env={"FAKE_AGENT_MODE": "garbage"})
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.exists(os.path.join(out5, "fs_pipe.c", "pipe_lock.raw.txt")))
        self.assertEqual(len(list(csv.DictReader(open(os.path.join(out5, "usage.csv"))))), 1)
        # fenced output parses
        out6 = os.path.join(self.tmp, "run6")
        r = self._run("func", "--root", SRC, "fs/pipe.c", "pipe_lock", "--out", out6, "--agent-bin", FAKE,
                      env={"FAKE_AGENT_MODE": "fenced"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_timeout_and_fatal(self):
        out = os.path.join(self.tmp, "run7")
        r = self._run("func", "--root", SRC, "fs/pipe.c", "pipe_lock", "--out", out, "--agent-bin", FAKE,
                      "--timeout", "2", "--retries", "0", env={"FAKE_AGENT_MODE": "sleep"})
        self.assertIn("timeout", r.stderr)
        out = os.path.join(self.tmp, "run8")
        r = self._run("file", "--root", SRC, "fs/pipe.c", "--filter", "^pipe_(lock|unlock)$", "--out", out,
                      "--agent-bin", FAKE, "-j", "1", env={"FAKE_AGENT_MODE": "fatal"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("FATAL", r.stderr)
        rows = list(csv.DictReader(open(os.path.join(out, "usage.csv"))))
        self.assertEqual(len(rows), 1)  # no retries, no further calls after an auth error

    def test_static_logs_attached(self):
        out = os.path.join(self.tmp, "run9")
        lf = os.path.join(self.tmp, "sparse.log")
        open(lf, "w").write("fs/pipe.c:91:17: warning: context imbalance in 'pipe_lock' - wrong count at exit\n"
                            "fs/pipe.c:2000:1: warning: far away\n")
        sm = os.path.join(self.tmp, "smatch.log")
        open(sm, "w").write("fs/pipe.c:90 pipe_lock() warn: inconsistent returns '&pipe->mutex'.\n")
        r = self._run("func", "--root", SRC, "fs/pipe.c", "pipe_lock", "--out", out, "--dry-run",
                      "--sparse-log", lf, "--smatch-log", sm)
        self.assertEqual(r.returncode, 0, r.stderr)
        b = open(os.path.join(out, "fs_pipe.c", "pipe_lock.bundle.md")).read()
        self.assertIn("context imbalance", b)
        self.assertIn("inconsistent returns", b)
        self.assertNotIn("far away", b)
        r = self._run("report", "--out", out)
        self.assertIn("static:sparse", open(os.path.join(out, "report.md")).read())


class UnitTests(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)

    def test_extract_variants(self):
        f = jsonx.extract_findings('[{"line": 5, "severity": "HIGH", "category": "locking", "rationale": "x", "confidence": 0.9}]')
        self.assertEqual(f[0]["severity"], "high")
        self.assertEqual(jsonx.extract_findings("[]"), [])
        f = jsonx.extract_findings('Sure.\n```json\n[{"line": "L12", "severity": "major", "category": "use after free", "rationale": "y", "confidence": 80}]\n```')
        self.assertEqual((f[0]["line"], f[0]["severity"], f[0]["category"], f[0]["confidence"]), (12, "high", "other", 0.8))
        f = jsonx.extract_findings('Analysis... {"findings": [{"line": 3, "severity": "low", "rationale": "z"}]} done')
        self.assertEqual(f[0]["line"], 3)
        f = jsonx.extract_findings('Note [1] see below: [{"line": 7, "severity": "medium", "category": "locking", "rationale": "w"}]')
        self.assertEqual(f[0]["line"], 7)
        with self.assertRaises(jsonx.ExtractError):
            jsonx.extract_findings("no json at all")

    def test_envelope(self):
        t, e = jsonx.parse_envelope('{"type":"result","result":"[]","usage":{"inputTokens":5}}\n')
        self.assertEqual(t, "[]")
        t, e = jsonx.parse_envelope('warning: something\n{"type":"result","result":"[1]"}\n')
        self.assertEqual(t, "[1]")
        self.assertEqual(kreview.reported_tokens({"usage": {"inputTokens": 5, "outputTokens": 2,
                                                            "cacheReadTokens": 1, "cacheWriteTokens": 0}}), (5, 2, 1, 0))

    def test_static_parsers(self):
        s = static_pass.parse_tool_log("/k/fs/pipe.c:12:3: warning: dubious: !x & y\n"
                                       "fs/pipe.c:40 foo() error: double unlock 'mutex'\n"
                                       "  CC fs/pipe.o\n", "sparse", "/k")
        self.assertEqual([(x["file"], x["line"]) for x in s], [("fs/pipe.c", 12), ("fs/pipe.c", 40)])
        self.assertEqual(static_pass.notes_for(s, "fs/pipe.c", 10, 20), ["sparse warning L12: dubious: !x & y"])

    def test_checkpatch_parse(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "scripts"))
        cp = os.path.join(tmp, "scripts", "checkpatch.pl")
        with open(cp, "w") as fh:
            fh.write('print "x.patch:9: WARNING:LONG_LINE: line length of 120 exceeds 100 columns\\n'
                     '#9: FILE: fs/pipe.c:91:\\n";\n')
        f, raw = static_pass.run_checkpatch(tmp, PATCH, tmp)
        shutil.rmtree(tmp)
        self.assertEqual(f, [{"tool": "checkpatch", "file": "fs/pipe.c", "line": 91, "kind": "warning",
                              "msg": "LONG_LINE: line length of 120 exceeds 100 columns"}])

    def test_cache_key_changes(self):
        class A:  # minimal args
            max_requests, max_est_tokens, cache_dir, out = 1, 1, tempfile.mkdtemp(), tempfile.mkdtemp()
        r = kreview.Runner(A, "t")
        j = kreview.Job("func", "f.c", "fn", "bundle", {"model": "m1", "risk": {"score": 1, "level": "low"},
                                                        "start": 1, "end": 2, "tokens": 1}, A.out)
        k1 = r.key(j); j.model = "m2"; k2 = r.key(j); j.bundle = "bundle2"; k3 = r.key(j)
        self.assertEqual(len({k1, k2, k3}), 3)


if __name__ == "__main__":
    unittest.main()
