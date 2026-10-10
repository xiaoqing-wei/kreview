"""static_pass.py - optional static-analysis prepass: checkpatch.pl on patches, and parsers for
sparse / smatch logs that the user produced separately (we never build the kernel ourselves)."""
import os, re, subprocess

# sparse/gcc style:  fs/pipe.c:123:9: warning: incorrect type in argument 1 (different address spaces)
GCC_RE = re.compile(r"^(?P<file>[\w./+-]+\.[ch]):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<kind>warning|error|note)?:?\s*(?P<msg>.+)$")
# smatch:  fs/pipe.c:123 anon_pipe_read() warn: inconsistent returns '&pipe->mutex'.
SMATCH_RE = re.compile(r"^(?P<file>[\w./+-]+\.[ch]):(?P<line>\d+)\s+(?P<func>[\w$]+)\(\)\s+(?P<kind>warn|error|info):\s*(?P<msg>.+)$")
# checkpatch --terse:  my.patch:42: WARNING: line length of 105 exceeds 100 columns   (or file path with -f)
CP_TERSE_RE = re.compile(r"^(?P<src>[^:]+):(?P<line>\d+):\s*(?P<kind>ERROR|WARNING|CHECK):\s*(?P<msg>.+)$")
CP_FILE_RE = re.compile(r"^#\d+:\s*FILE:\s*(?P<file>[^:]+):(?P<line>\d+):")


def _norm_path(p, root):
    p = p.strip()
    if root and os.path.isabs(p):
        try:
            p = os.path.relpath(p, root)
        except ValueError:
            pass
    return p[2:] if p.startswith("./") else p


def parse_tool_log(text, tool, root=None):
    """Parse sparse or smatch output into [{tool,file,line,kind,msg}]."""
    out = []
    for l in text.splitlines():
        l = l.rstrip()
        m = SMATCH_RE.match(l)
        if m:
            out.append({"tool": tool, "file": _norm_path(m["file"], root), "line": int(m["line"]),
                        "kind": m["kind"], "msg": "%s(): %s" % (m["func"], m["msg"])})
            continue
        m = GCC_RE.match(l)
        if m and m["kind"] != "note":
            out.append({"tool": tool, "file": _norm_path(m["file"], root), "line": int(m["line"]),
                        "kind": m["kind"] or "warning", "msg": m["msg"].strip()})
    return out


def run_checkpatch(root, patch_text, workdir):
    """Run <root>/scripts/checkpatch.pl on the patch if present. Returns (findings, raw_or_reason)."""
    cp = os.path.join(root, "scripts", "checkpatch.pl")
    if not os.path.isfile(cp):
        return [], "skipped: %s not present (sparse tree?)" % os.path.relpath(cp, root)
    pf = os.path.join(workdir, "checkpatch-input.patch")
    with open(pf, "w") as f:
        f.write(patch_text)
    try:
        p = subprocess.run(["perl", cp, "--no-tree", "--no-signoff", "--show-types", "--emacs", pf],
                           cwd=root, capture_output=True, text=True, errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], "checkpatch failed to run: %s" % e
    raw = p.stdout + p.stderr
    out, last_kind, last_msg = [], None, None
    for l in raw.splitlines():
        m = CP_TERSE_RE.match(l)
        # --emacs on a patch prints "<patchfile>:<patchline>: KIND:TYPE: msg", then "#N: FILE: path:line:"
        if m:
            last_kind, last_msg = m["kind"], m["msg"]
            continue
        m2 = CP_FILE_RE.match(l)
        if m2 and last_kind:
            out.append({"tool": "checkpatch", "file": _norm_path(m2["file"], root), "line": int(m2["line"]),
                        "kind": last_kind.lower(), "msg": last_msg})
            last_kind = None
    return out, raw


def notes_for(findings, file, start, end, slack=2):
    """Static findings that land inside [start-slack, end+slack] of file, as bundle bullet strings."""
    res = []
    for f in findings:
        if f["file"] == file and start - slack <= f["line"] <= end + slack:
            res.append("%s %s L%d: %s" % (f["tool"], f["kind"], f["line"], f["msg"]))
    return res
