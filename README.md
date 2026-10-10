# kreview — per-function AI review of Linux kernel code with the Cursor Agent CLI

A fresh `agent -p` run per function usually explores the tree on its own, and requests grow past 400K input
tokens. Grok 4.7 bills input above 256K at 2x. kreview avoids this. It builds a small, budgeted **context bundle**
for each function (target source, risk signals, the patch, referenced types/macros, callees, callers). It runs the
agent in read-only `ask` mode in an **empty temp workspace** and tells it to use only that bundle. Then it collects
structured JSON findings into a deduplicated report.

Measured with the real CLI (2026-10-09): **~18–23K input tokens per call** (about 1–2K of that is the bundle; the rest
is the agent's own system prompt/tool overhead), plus 3–8K output tokens (including reasoning), at 50–110 s per call.

## Files
| file | purpose |
|---|---|
| `kreview` / `kreview.py` | **Single entrypoint**: `file`, `func`, `patch`, `report` subcommands; runner, cache, guardrails, report. |
| `kbundle.py` | Builds the markdown bundle for one function (library + CLI; stdlib only; ctags/cscope/git grep). |
| `risk.py` | Risk signals, score and model recommendation (library + CLI). |
| `patchmap.py` | Unified-diff parser; maps hunks to enclosing functions; detects pre/post-image tree (library + CLI). |
| `jsonx.py` | Robust extraction and normalization of the findings array from model output. |
| `static_pass.py` | Optional prepass: `scripts/checkpatch.pl` on patches; parses sparse/smatch logs. |
| `prompt.md` | Fixed review prompt (7-item checklist, "bundle only" rule, JSON schema). Its hash is part of the cache key. |
| `review.sh` | Deprecated wrapper around `kreview` (kept for old invocations). |
| `tests/` | Offline tests (sparse tree + fake agent): `python3 -m unittest discover -s tests -v`. |
| `examples/` | Example bundles, and `examples/smoke_run/` with the real 3-call smoke test (results, usage.csv, report). |

## Setup
```sh
# 1. Cursor Agent CLI (https://cursor.com/docs/cli/installation)
curl https://cursor.com/install -fsS | bash        # installs ~/.local/bin/agent (symlink to ~/.local/share/cursor-agent/versions/<v>)
export PATH="$HOME/.local/bin:$PATH"
agent --version                                      # tested: 2026.10.01-e373342
# 2. Tools: python3 (3.8+), git, universal-ctags (strongly recommended), cscope (optional)
sudo apt-get install -y universal-ctags cscope
# 3. Optional: cscope index for better callers: (cd ~/linux && cscope -b -q -k -R)
```

## Auth
Headless use needs an API key (Cursor Dashboard → API Keys); see https://cursor.com/docs/cli/reference/authentication.
```sh
export CURSOR_API_KEY=...        # or put it in ~/.profile / your CI secret store; never commit it
agent status                     # "✓ Logged in as <email>"   (agent status --format json for scripts)
agent --list-models              # or: agent models
```
`agent login` (browser flow) also works on a workstation. kreview never passes the key on the command line
(no `--api-key`), so it doesn't show up in `ps`. It inherits `CURSOR_API_KEY` from the environment.

## Usage
```sh
export KREVIEW_ROOT=~/linux                     # or pass --root
./kreview func  fs/pipe.c anon_pipe_read        # one function
./kreview file  fs/pipe.c --filter '^pipe_'     # all (matching) functions in a file, highest risk first
./kreview patch my.patch                        # functions touched by a patch file
./kreview patch HEAD~3..HEAD                    # ... or a git range / single commit (`./kreview patch abc123`)
./kreview report                                # re-aggregate results/ -> results/report.md + report.csv
./kreview file fs/pipe.c --dry-run              # bundles + plan.json + cost estimate, NO agent calls
```
Each run writes, under `--out` (default `./results`):
`<file>/<func>.bundle.md`, `.meta.json`, `.json` (status + findings + CLI-reported usage), `.raw.txt` on failure
(raw stdout/stderr), `.raw.json` with `--keep-raw`; `usage.csv` (one row per agent call); `plan.json`;
`patch_<spec>.map.json`; `static/`; and `report.md` + `report.csv` (regenerated after every real run).

Important options (`./kreview file -h` lists them all):

| option | default | meaning |
|---|---|---|
| `-j/--concurrency` | 2 | parallel agent processes |
| `--timeout` | 900 s | per-call timeout (the process group is killed) |
| `--retries` / `--backoff` | 2 / 10 s | retries on agent error/timeout, with exponential backoff and jitter (x2 on rate limits). Auth/unknown-model errors abort the run without retrying. Parse errors aren't retried unless you pass `--retry-parse`. |
| `--max-requests` | 40 | **hard cap on agent calls per run (retries count)** |
| `--max-est-tokens` | 1.5M | **hard cap on estimated tokens per run** (input est. + output est. per call) |
| `--overhead-tokens` | 22000 | per-call agent overhead used in estimates (measured ~21K) |
| `--est-output-tokens` | 6000 | per-call output estimate (measured 3–8K, reasoning included) |
| `--max-bundle-tokens` | 60000 | kbundle budget per function |
| `--no-cache`, `--cache-dir` | `results/.cache` | cache key = sha256(bundle text + model + prompt version) |
| `--input-mode` | auto | `arg` (< 120 KB), `stdin` (larger; verified), `file` (agent reads bundle.md through a tool) |
| `--model` / `--model-{low,high,xhigh}` / `--no-low` | see below | routing overrides |
| `--sparse-log F`, `--smatch-log F` | – | attach static-analysis output (repeatable) |
| `--no-checkpatch`, `--side {auto,new,old}` | – | patch mode only |
| `--agent-arg=...` | – | extra agent flags, e.g. `--agent-arg=--sandbox=enabled` |

Env equivalents: `KREVIEW_ROOT KREVIEW_OUT KREVIEW_CONCURRENCY KREVIEW_TIMEOUT KREVIEW_MAX_REQUESTS
KREVIEW_MAX_EST_TOKENS KREVIEW_MAX_BUNDLE_TOKENS KREVIEW_OVERHEAD_TOKENS KREVIEW_CACHE KREVIEW_MODEL_{LOW,HIGH,XHIGH}
KREVIEW_HIGH_RISK KREVIEW_TRIVIAL KREVIEW_PROMPT AGENT_BIN INPUT_MODE`.

### Patch mode
Hunks are mapped to enclosing functions with ctags on the files in `--root`. kreview uses git to find out
which side of the diff the tree holds:
- git range whose end is `HEAD` (or a patch that reverse-applies) → **post-image** (best: bundles show the new code)
- range start is `HEAD`, or the patch applies forward → **pre-image**. Mapping then uses old-side line numbers, and the bundle shows the old code plus the diff.

Each function's bundle includes only *its* hunks (with the file header). If the patch exists and
`scripts/checkpatch.pl` exists in the tree, checkpatch runs first. Its findings go into the matching bundles
("Static analysis prepass") and the report. Hunks outside any function (globals, struct/macro edits) and
non-C files are listed as "unmapped" in the log and in `patch_*.map.json`. They aren't reviewed.

### Static analysis (optional, nothing heavy is installed)
- checkpatch: automatic in patch mode when `<root>/scripts/checkpatch.pl` exists (needs perl). It's skipped on the sparse tree.
- sparse: `make C=2 CF="-D__CHECK_ENDIAN__" fs/pipe.o 2> sparse.log`, then `--sparse-log sparse.log`.
- smatch: `~/smatch/smatch_scripts/kchecker fs/pipe.c > smatch.log`, then `--smatch-log smatch.log`.
Parsed formats: `file:line[:col]: warning|error: msg` (sparse/gcc) and `file:line func() warn|error: msg` (smatch).
Lines within ±2 of a function go into its bundle. All parsed items appear in the report as `static:<tool>` (`--no-static` hides them).

### Report
`report.md` / `report.csv` sort findings by severity, then confidence, then location. Duplicates (same file +
category + line) are merged, and so are near-duplicates (±1 line) that come from *different* result files. The
merged entry keeps the most severe and most confident version, the list of sources, and a count. The report also
lists functions reviewed with no findings, failed/skipped calls, and a usage summary from `usage.csv`.

## Model routing
Model IDs come from `agent --list-models` on 2026-10-09 (CLI 2026.10.01-e373342):
`grok-4.7-low`, `grok-4.7-medium`, `grok-4.7-high`, `grok-4.7-xhigh` (plus `-fast` variants of each) and
`composer-2.5` / `composer-2.5-fast`.

| risk.py level | default model |
|---|---|
| high (score ≥ 14: locking/RCU/barriers/user-copy heavy, long, many error paths) | `grok-4.7-xhigh` |
| low / medium | `grok-4.7-high` |
| trivial (score ≤ 2, ≤ 15 lines, no heavy primitives) | `grok-4.7-low` (`--no-low` to disable) |

Examples: `--model composer-2.5` (one cheap model for everything), `--model-xhigh grok-4.7-high` (no xhigh),
`KREVIEW_HIGH_RISK=18` (fewer xhigh calls). On fs/pipe.c (58 functions) the routing is 7 xhigh / 24 high / 27 low.

## Cost notes
- One call ≈ 20–25K input tokens (mostly agent overhead), plus about 1–8K output. Reviewing a whole file costs functions × that. Use
  `--dry-run` first: it prints the per-call and total estimates, and the run will refuse to go beyond `--max-requests` / `--max-est-tokens`.
  When the plan exceeds a cap, the lowest-risk functions are skipped (`skipped-budget`).
- **Grok 4.7: input above 256K tokens is billed at 2x.** Bundles default to ≤ 60K tokens, so a single call
  doesn't get close. The plan flags any call estimated above 256K.
- `xhigh` uses much more reasoning (output) than `high`. Route sparingly.
- Re-runs are free for unchanged bundles (cache). A change to prompt.md, the model, or the code invalidates the cache.
- Token numbers in `usage.csv` (`reported_*`) come from the CLI's JSON `usage` object. The `est_*` columns are kreview's
  chars/4 estimates. Billing is authoritative only in the Cursor dashboard.

## Cursor CLI facts this relies on (verified 2026-10-09, CLI 2026.10.01-e373342)
Docs: https://cursor.com/docs/cli/installation · https://cursor.com/docs/cli/reference/parameters ·
https://cursor.com/docs/cli/headless · https://cursor.com/docs/cli/reference/output-format ·
https://cursor.com/docs/cli/reference/authentication
- Command: `agent -p --model <id> --mode ask --output-format json --trust --workspace <empty tmpdir> [prompt]`.
  `--mode ask` is read-only. kreview never passes `-f/--force/--yolo`.
- `--output-format json` prints one object: `{type, subtype, is_error, duration_ms, duration_api_ms, result,
  session_id, request_id}`. The answer is in `.result`. This version **also emits `usage: {inputTokens, outputTokens,
  cacheReadTokens, cacheWriteTokens}`** (not in the docs; kreview logs it if present).
- **The prompt via stdin works with `-p`** (verified in the smoke test, pipe_lock, `--input-mode stdin`). The docs show only a positional
  prompt. A positional argument is limited to 128 KiB (Linux MAX_ARG_STRLEN), so `auto` uses argv below 120 KB and stdin above.
- On a failure the CLI exits non-zero and writes to stderr, with no JSON.

## Limitations
- Heuristic C parsing: all `#ifdef` branches are shown. Macro-generated functions aren't found (only `SYSCALL_DEFINEn` is
  special-cased). Same-named static functions in other files can be picked (`--line-hint` / patch mode disambiguate within a file).
- Definitions come from `include/`, `arch/<arch>/include` and the target's directory. Callees in other `.c` files
  show only a prototype. Callers come from text search/cscope, so indirect dispatch through ops tables shows only the table entry.
- Risk scores are keyword-based and good for routing only. Weights are untuned.
- Even with the "bundle only" rule and an empty workspace, the agent might still use web search or MCP tools if they're
  configured for the account. Results are model output and need human verification. Expect false positives and misses.
  An empty array doesn't mean "no bug".
- The agent's ~20K-token overhead per call dominates cost for small functions. Batching several small functions into one
  call isn't implemented.
- Patch mode doesn't review hunks outside functions (struct layout, macros, Kconfig) and doesn't build the kernel.
  sparse/smatch must be run separately.
- Tags are cached in `~/.cache/kbundle/<hash(root@HEAD)>`. Header edits outside the target dir aren't seen until HEAD
  changes (delete the cache to force a rebuild).
- The token estimates (chars/4) are rough. `usage.csv` has the CLI-reported numbers after a run.
