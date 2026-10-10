#!/usr/bin/env python3
"""Fake `agent` for offline tests. Behaviour via env FAKE_AGENT_MODE:
ok (default) | fail_once | garbage | sleep | fenced.  Reads prompt from argv[-1] or stdin."""
import json, os, sys, time
mode = os.environ.get("FAKE_AGENT_MODE", "ok")
argv = sys.argv[1:]
assert "-p" in argv and "--mode" in argv and argv[argv.index("--mode") + 1] == "ask", argv
model = argv[argv.index("--model") + 1]
prompt = argv[-1] if not argv[-1].startswith("--") and argv[-2] not in ("--workspace", "--model", "--mode", "--output-format") else ""
if not prompt and not sys.stdin.isatty():
    prompt = sys.stdin.read()
log = os.environ.get("FAKE_AGENT_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"model": model, "len": len(prompt), "stdin": not argv[-1] == prompt}) + "\n")
if mode == "fail_once":
    flag = os.environ["FAKE_AGENT_FLAG"]
    if not os.path.exists(flag):
        open(flag, "w").close()
        sys.stderr.write("Error: upstream 503\n"); sys.exit(1)
if mode == "sleep":
    time.sleep(30)
if mode == "fatal":
    sys.stderr.write("Error: Authentication required. Please run 'agent login' first\n"); sys.exit(1)
import re
m = re.search(r"Target function: `(\w+)` \([^:]+:(\d+)-", prompt)
fn, line = (m.group(1), int(m.group(2)) + 2) if m else ("?", 1)
findings = [{"line": line, "severity": "high" if "lock" in fn else "low", "category": "locking",
             "rationale": "fake finding for %s" % fn, "confidence": 0.7},
            {"line": 10, "severity": "medium", "category": "other", "rationale": "shared dup", "confidence": 0.4}]
txt = json.dumps(findings)
if mode == "garbage":
    txt = "I could not decide. No JSON here."
if mode == "fenced":
    txt = "Here you go:\n```json\n" + txt + "\n```\nThanks."
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5, "duration_api_ms": 5,
                  "result": txt, "session_id": "s", "request_id": "r",
                  "usage": {"inputTokens": 20000 + len(prompt) // 4, "outputTokens": 100, "cacheReadTokens": 0,
                            "cacheWriteTokens": 0}}))
