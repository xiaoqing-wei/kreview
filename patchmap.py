#!/usr/bin/env python3
"""patchmap.py - parse unified diffs and map hunks to enclosing C functions.

Library: parse_diff(text) -> [FileDiff]; map_patch(root, text, side) -> plan dict.
CLI:     patchmap.py <kernel_root> <patchfile|git-range>   (prints JSON)
"""
import json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kbundle  # noqa: E402

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class Hunk:
    def __init__(self, old_start, old_len, new_start, new_len, header, text):
        self.old_start, self.old_len, self.new_start, self.new_len = old_start, old_len, new_start, new_len
        self.header, self.text = header, text  # text includes the @@ line
        self.old_changed, self.new_changed = [], []  # changed line numbers on each side

    def changed(self, side):
        """Line numbers touched on `side` ('new' or 'old'). Pure deletions on the new side map to
        the line where the deletion happened, so the enclosing function is still found."""
        return self.new_changed if side == "new" else self.old_changed


class FileDiff:
    def __init__(self, old_path, new_path, header):
        self.old_path, self.new_path, self.header = old_path, new_path, header
        self.hunks = []

    @property
    def path(self):
        return self.new_path if self.new_path != "/dev/null" else self.old_path


def _strip_prefix(p):
    p = p.strip().split("\t")[0]
    if p == "/dev/null":
        return p
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_diff(text):
    files, cur, hunk_lines, hunk = [], None, None, None
    header_lines = []

    def close_hunk():
        nonlocal hunk, hunk_lines
        if hunk is not None:
            hunk.text = "\n".join(hunk_lines)
            cur.hunks.append(hunk)
        hunk, hunk_lines = None, None

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("diff --git ") or (l.startswith("--- ") and i + 1 < len(lines)
                                             and lines[i + 1].startswith("+++ ") and hunk is None):
            if cur is not None:
                close_hunk()
            header_lines = []
            old = new = None
            if l.startswith("diff --git "):
                m = re.match(r"diff --git (\S+) (\S+)", l)
                old, new = (_strip_prefix(m.group(1)), _strip_prefix(m.group(2))) if m else (None, None)
                header_lines.append(l)
                i += 1
                while i < len(lines) and not lines[i].startswith(("--- ", "diff --git ", "@@")):
                    header_lines.append(lines[i]); i += 1
            if i < len(lines) and lines[i].startswith("--- "):
                old = _strip_prefix(lines[i][4:]); header_lines.append(lines[i]); i += 1
                if i < len(lines) and lines[i].startswith("+++ "):
                    new = _strip_prefix(lines[i][4:]); header_lines.append(lines[i]); i += 1
            cur = FileDiff(old, new or old, "\n".join(header_lines))
            files.append(cur)
            continue
        m = HUNK_RE.match(l)
        if m and cur is not None:
            close_hunk()
            os_, ol, ns, nl = int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
            hunk = Hunk(os_, ol, ns, nl, m.group(5).strip(), "")
            hunk_lines = [l]
            o, n = os_, ns
            i += 1
            while i < len(lines) and (o < os_ + ol or n < ns + nl):
                t = lines[i]
                if t.startswith("\\"):  # "\ No newline at end of file"
                    hunk_lines.append(t); i += 1; continue
                c = t[:1]
                if c == "+":
                    hunk.new_changed.append(n)
                    hunk.old_changed.append(o)  # insertion point on the old side
                    n += 1
                elif c == "-":
                    hunk.old_changed.append(o)
                    hunk.new_changed.append(n)  # deletion point on the new side
                    o += 1
                elif c in (" ", ""):
                    if c == "" and t == "" and i == len(lines) - 1:
                        break
                    o += 1; n += 1
                else:
                    break
                hunk_lines.append(t); i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                hunk_lines.append(lines[i]); i += 1
            hunk.new_changed = sorted(set(hunk.new_changed))
            hunk.old_changed = sorted(set(hunk.old_changed))
            continue
        i += 1
    if cur is not None:
        close_hunk()
    return files


def git(root, *args):
    p = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True, errors="replace")
    return p.returncode, p.stdout, p.stderr


def load_patch(root, spec):
    """spec: path to a patch file, or a git range/commit ('A..B', 'HEAD~3..', 'abc123').
    Returns (diff_text, info dict)."""
    if os.path.isfile(spec):
        return open(spec, encoding="utf-8", errors="replace").read(), {"source": "file", "spec": spec}
    rng = spec if ".." in spec else "%s^..%s" % (spec, spec)
    rc, out, err = git(root, "diff", "--no-color", "-U5", rng)
    if rc != 0:
        raise ValueError("not a file and not a valid git range: %s (%s)" % (spec, err.strip()))
    a, b = rng.split("..", 1)
    b = b or "HEAD"
    return out, {"source": "git", "spec": rng, "from": a, "to": b}


def detect_side(root, text, info):
    """Which side of the diff matches the working tree? 'new' (patched tree) or 'old'."""
    if info.get("source") == "git":
        _, head, _ = git(root, "rev-parse", "HEAD")
        _, to, _ = git(root, "rev-parse", info["to"])
        _, frm, _ = git(root, "rev-parse", info["from"] or "HEAD")
        if head.strip() and head.strip() == to.strip():
            return "new", "working tree HEAD == range end (post-image)"
        if head.strip() and head.strip() == frm.strip():
            return "old", "working tree HEAD == range start (pre-image); bundles show pre-patch code"
    p = subprocess.run(["git", "-C", root, "apply", "--check", "-R", "-"], input=text, capture_output=True, text=True)
    if p.returncode == 0:
        return "new", "patch reverse-applies: tree is post-image"
    p = subprocess.run(["git", "-C", root, "apply", "--check", "-"], input=text, capture_output=True, text=True)
    if p.returncode == 0:
        return "old", "patch applies forward: tree is PRE-image; bundles show pre-patch code (apply it for best results)"
    return "new", "could not verify tree state with git apply; assuming post-image line numbers"


def map_patch(root, text, side="auto", info=None):
    """Return {'side':..., 'side_note':..., 'targets': [...], 'unmapped': [...], 'files': [...]}.
    Each target: {file, function, start, end, line_hint, hunks:[hunk text], diff: file-header + hunks}."""
    info = info or {}
    note = "forced"
    if side == "auto":
        side, note = detect_side(root, text, info)
    targets, unmapped, files_out = {}, [], []
    for fd in parse_diff(text):
        path = fd.new_path if side == "new" else fd.old_path
        files_out.append(fd.path)
        if path in (None, "/dev/null"):
            unmapped.append({"file": fd.path, "reason": "file deleted/added on the other side"})
            continue
        if not path.endswith((".c", ".h")):
            unmapped.append({"file": path, "reason": "not a C source/header"})
            continue
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            unmapped.append({"file": path, "reason": "not present in tree (sparse checkout?)"})
            continue
        funcs = kbundle.list_functions(full)
        for h in fd.hunks:
            hit = set()
            for ln in h.changed(side):
                for n, s, e in funcs:
                    # include the return-type line(s) above the ctags name line
                    loc = (s - 2, e)
                    if loc[0] <= ln <= loc[1]:
                        hit.add((n, s, e))
            if not hit:
                unmapped.append({"file": path, "reason": "hunk outside any function (globals/structs/macros)",
                                 "hunk": h.text.split("\n", 1)[0]})
                continue
            for n, s, e in hit:
                k = (path, n, s)
                t = targets.setdefault(k, {"file": path, "function": n, "start": s, "end": e,
                                           "line_hint": s, "hunks": [], "header": fd.header})
                if h.text not in t["hunks"]:
                    t["hunks"].append(h.text)
    out = []
    for t in sorted(targets.values(), key=lambda t: (t["file"], t["start"])):
        t["diff"] = t.pop("header") + "\n" + "\n".join(t["hunks"]) + "\n"
        out.append(t)
    return {"side": side, "side_note": note, "targets": out, "unmapped": unmapped, "files": files_out}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    root = os.path.abspath(sys.argv[1])
    txt, inf = load_patch(root, sys.argv[2])
    res = map_patch(root, txt, info=inf)
    for t in res["targets"]:
        t.pop("diff"); t["hunks"] = [h.split("\n", 1)[0] for h in t["hunks"]]
    print(json.dumps(res, indent=2))
