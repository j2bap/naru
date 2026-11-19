# naru

[![checks](https://github.com/j2bap/naru/actions/workflows/checks.yml/badge.svg)](https://github.com/j2bap/naru/actions/workflows/checks.yml)

**Memory for coding agents that you approve.**

Your agents learn useful things while they work: the test command, the port
the API uses, the shell quirk that cost an hour. Then the session ends and the
next agent learns it all again.

Naru lets any agent write down what it learned as a *claim*. You approve or
reject each claim. Approved claims go into the context file your agent already
reads, like `CLAUDE.md` or `AGENTS.md`. Claude Code, Codex, Cursor, and any
tool that can run a shell command can share the same memory.

```
  agent ──naru claim──▶  inbox  ──you approve──▶  approved doc  ──naru inject──▶  CLAUDE.md
                                                                                  AGENTS.md
                                                                                  .cursorrules
```

Naru is one folder of Python with no dependencies. To use it, you need the
`naru` command and its hooks. The rest of this repository is research code
that implements and measures the paper Naru is built on. The
[repository map](#repository-map) shows which files do which job.

## Quick start

You need Python 3.9 or later and a SQLite build with FTS5. Most Python builds
include it.

```bash
git clone https://github.com/j2bap/naru
cd naru
python3 ms.py                             # checks the SQLite store naru uses; prints "ok"
mkdir -p ~/.local/bin
ln -s "$PWD/naru.py" ~/.local/bin/naru    # or any folder on your PATH
command -v naru                           # prints the path when naru is on your PATH
```

The self-check runs offline and writes only to a temporary folder. If it fails
with an FTS5 error, your Python's SQLite build lacks FTS5.

The symlink matters. Your agents run `naru` from their own project folders, so
the command must be on your `PATH`. If `command -v naru` prints nothing, add
`~/.local/bin` to your `PATH`, or link `naru.py` into a folder that is already
on it.

### Try the loop

Naru keeps everything in `~/.naru/log.db`. To try the loop without adding an
example fact to that store, use a scratch folder and a scratch store:

```bash
mkdir -p /tmp/naru-try && cd /tmp/naru-try
export NARU_DB="$PWD/log.db"
printf '# My notes\n\nKeep this line.\n' > AGENTS.md
```

An agent files a claim:

```console
$ naru claim "Run tests with: make test" --key tests --by claude
claim 1 pending — `naru inbox` to decide
```

You review the inbox:

```console
$ naru inbox
Promote only a fact that helps in many repositories and that an agent would not guess, such as a non-standard rule or a tool quirk. Never promote a repository overview.

[1/1] claim seq 1 · claude · 2026-10-15T16:25 · key: tests
  + Run tests with: make test
  promote / archive / reject / skip ?
```

You type `promote`, or just `p`. Then you write the approved doc into your
context file:

```console
$ naru inject AGENTS.md
wrote 108 chars into AGENTS.md (kept the 28 chars already there)
```

Naru writes only between two markers and keeps the rest of your file:

```markdown
# My notes

Keep this line.

<!-- naru:begin -->
# naru · seq 1 · ~10 tokens

## Decisions
- Run tests with: make test
<!-- naru:end -->
```

When you finish, run `unset NARU_DB` to go back to your real store.

## Connect your agent

Pick one setup for each harness you use:

| Harness | Setup | What it does |
| --- | --- | --- |
| Claude Code | [`naru claude-hook`](#claude-code) | Loads the doc at session start, refreshes it after each decision, and gives it to subagents. |
| Codex | [The Codex plugin](#codex) | Does the same through Codex hooks. |
| Cursor | [`naru inject .cursorrules`](#cursor-and-other-tools) | Writes the doc into the file Cursor reads. |
| Any tool that runs a shell command | [`naru inject <file>`](#cursor-and-other-tools) | Writes the doc into the file that tool reads. |

The Claude Code hook and the Codex plugin also tell the agent how to file
claims. With a file-only setup, you add that instruction yourself. See
[Tell your agent to file claims](#tell-your-agent-to-file-claims).

### Claude Code

`naru claude-hook` loads the approved doc at session start, refreshes it after
you promote or retire a claim, and gives the doc to subagents. It tells a
subagent not to file claims, because a subagent cannot judge what is already
known. Add it to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{"matcher": "startup|resume|clear|compact",
      "hooks": [{"type": "command", "command": "naru claude-hook", "timeout": 5}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "naru claude-hook", "timeout": 2}]}],
    "SubagentStart": [{"hooks": [{"type": "command", "command": "naru claude-hook", "timeout": 5}]}]
  }
}
```

If a file Claude Code loads at start already holds the doc, the hook sends only
the filing instructions, so the doc does not appear twice.

To use a file instead of the hook, write the doc into the file Claude Code
loads in every folder:

```bash
naru inject ~/.claude/CLAUDE.md
```

### Codex

The Codex plugin loads the approved doc for you, so you do not need to run
`inject`. Install it from the naru folder, after `naru` is on your `PATH`:

```bash
codex plugin marketplace add "$PWD/codex"
codex plugin add naru-codex@naru
```

Start a new Codex thread and trust the plugin hooks when Codex asks. The plugin
loads the approved doc at session start, refreshes it after you promote or
retire a claim, and gives the same doc to subagents. It tells a subagent not
to file claims, because a subagent cannot judge what is already known.

Codex also reads `AGENTS.md` in its home folder, `$CODEX_HOME`, which is
`~/.codex` when the variable is not set. If you write the doc there, session
start sends only the filing instructions:

```bash
naru inject "${CODEX_HOME:-$HOME/.codex}/AGENTS.md"
```

The plugin stores only the last doc sequence and fingerprint that each Codex
session saw. That data lives in a `context_delivery` table, not in the
searchable log.

The plugin does not replace Codex tool results. In Codex 0.152, the only
working way to replace a result marks the tool as blocked after it already ran,
which can make an agent repeat a write or a request.

### Cursor and other tools

For a harness without hooks, write the doc into the file it reads:

```bash
naru inject .cursorrules     # Cursor, in each project
naru inject AGENTS.md        # DeepSeek Harness, or any tool that reads AGENTS.md
```

Run `inject` once per file. Naru records the file, and every later `promote`
or `drop`, including a decision in `naru inbox`, writes the new doc into it.
To stop Naru writing a file, delete the markers. Without a path,
`naru inject` prints the doc to stdout.

A file inside a project, such as `.cursorrules`, is often committed. If it is,
your approved facts are committed with it.

### Tell your agent to file claims

The Claude Code hook and the Codex plugin already send this instruction. For a
file-only setup, add it to the file yourself, outside the Naru markers:

```markdown
## Naru

File one lasting fact per claim:

    naru claim "<text>" --key <topic> --by <agent>

For a repeatable procedure, use `naru skill` instead of `naru claim`. A claim or skill holds at most
400 bytes, so the doc stays small.
```

### Claude Code hook for large output

A single command can print thousands of lines into your agent's context.
`hook_spill.py` is a `PostToolUse` hook that moves large tool output into the
Naru log and leaves a short preview with a recovery handle in its place. The
agent runs `naru show <seq>` if it needs the full text. `show` prints at most
20,000 characters per call, which fits the output limits of Claude Code and
Codex, and ends with the command that prints the rest.

To bound one large command yourself, pipe its output through `naru spill`.
Output under 2,400 characters passes through unchanged. Larger output goes
into the log, and you get a preview with a recovery handle. The preview keeps
the first and last 300 characters and a few line signposts.

To change these sizes, set `NARU_SPILL_THRESHOLD` or `NARU_SPILL_PREVIEW` in
your environment. If you set them only on the hook command, `naru stats`
judges output against a threshold that the hook did not use.

Add this to your Claude Code `settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/naru/hook_spill.py"
          }
        ]
      }
    ]
  }
}
```

`naru stats` shows how often output spills and how often an agent reopens it.
It counts a spill as reopened only when a later `naru show` covers that exact
spill and names the spill's run or no run. A spill too large for one `show`
counts when one of its slices is shown.

## Keep facts current

### Change an approved fact

Retire the old claim first, then approve the new one:

```bash
naru drop 1 --yes
naru claim "Run tests with: make check" --key tests --by you
naru promote 2 --yes
```

If you approve two claims on the same `--key` without retiring one, Naru shows
both under `## Unresolved`. It never picks one for you:

```markdown
## Unresolved
▲ claim tests — 2 versions
  · claude: Run tests with: make test
  · codex: Run tests with: make check
```

### Facts for one repository

Some facts hold only in one repository. Add `--repo` when you file one:

```bash
naru claim "Run the export with: node scripts/export.mjs" --key export --repo
```

Naru names the repository `owner/name` from its `origin` URL, or by its folder
name when it has no remote. A repository fact never goes into the general doc or
into any file, because a repository's `AGENTS.md` is usually committed. The
Codex and Claude Code hooks add the facts for the folder the session runs in,
under `## This repository (owner/name)`.

### Archive and reject

`naru drop` archives a claim. It leaves the doc, but it can still come back as
a lead. On each prompt, the Codex and Claude Code hooks add up to three
archived or pending claims that share at least two significant words with the
prompt, under "From the Naru archive (unreviewed)" with each claim's date.

`naru reject` marks a claim wrong, and the hooks never show it. Run `naru drop`
on a rejected claim to archive it again.

### Checks for stale facts

A fact that depends on a tool version or on files can carry a check:

```bash
naru claim "Codex 0.163 ignores X" --key codex.x --check "codex --version"
naru check 12 "/bin/bash --version" --yes   # add a check to an approved fact
```

Naru runs the check when you approve the fact and saves its output. Each
`naru inbox` runs the checks again. A fact whose output changed shows up as
`[stale]`, and you keep it or retire it. A check runs without a shell, from your
home folder, for at most 10 seconds.

### See pending claims in your status line

Pending claims wait until you run `naru inbox`, and nothing reminds you.
`prune` deletes a pending claim after 30 days, so a claim you never look at is
the only kind that expires. Add this to your status line script to show a
count:

```bash
db="${NARU_DB:-$HOME/.naru/log.db}" cache="${TMPDIR:-/tmp}/naru-pending.$UID"
if [[ $db -nt $cache ]]; then
  sqlite3 -readonly "$db" \
    "SELECT COUNT(*) FROM conversation_history WHERE kind='claim' AND promoted=0" \
    >"$cache" 2>/dev/null
fi
read -r pending <"$cache" 2>/dev/null
((pending > 0)) && printf ' ✉%d' "$pending"
```

The cache refreshes only when the database changes, so the count is never
stale. A cached render costs nothing you can measure, against 11.5 ms for the
query. It prints nothing when the inbox is clear or the store does not exist
yet.

## Commands

| Command                                 | What it does                                   |
| --------------------------------------- | ---------------------------------------------- |
| `naru claim "<text>" [--key K] [--by A]` | Propose a fact.                                |
| `naru claim ... --repo` or `--check CMD`  | Scope a fact to this repo, or attach a check.  |
| `naru skill "<procedure>" [--key K]`    | Propose a repeatable procedure.                |
| `naru inbox`                            | Promote, archive, reject, or skip items.       |
| `naru inject [PATH]`                    | Write the approved doc to a file or stdout.    |
| `naru promote SEQ --yes`                | Approve without the prompt.                    |
| `naru drop SEQ --yes`                   | Archive or retire without the prompt.          |
| `naru reject SEQ --yes`                 | Mark a claim wrong so the hooks never show it. |
| `naru check SEQ "<command>" --yes`      | Add a stale check to an approved fact.         |
| `naru add "<topic>" < notes.md`         | Save a note to the log.                        |
| `naru search QUERY`                     | Find matching lines in the log.                |
| `naru show SEQ [SEQ] [--at N]`          | Print an entry or range, 20,000 chars a call.  |
| `naru outline`                          | Show an index of the log.                      |
| `naru spill [--tool T] [--session S]`   | Store large stdin in the log; print a preview. |
| `naru stats [DAYS]`                     | Show spill and recovery counts.                |
| `naru prune [DAYS] [--dry-run]`         | Delete old pending claims and log rows.        |
| `naru gc`                               | Remove orphaned blobs from this store.         |
| `naru claude-hook`, `naru codex-hook`   | Hook commands. Claude Code and Codex run them. |

Everything lives in `~/.naru/log.db`. Set `NARU_DB` to use another file.

`promote` and `drop` refuse to run without a terminal unless you pass `--yes`.
This stops a tool loop from approving by accident. It is not a security
boundary, because anything that can run the CLI can also pass `--yes`.

## How it works

Naru has two layers.

**The log.** Naru implements [Context as an Environment: Programmatic Context
Management for Long-Horizon Agents](https://arxiv.org/abs/2608.21690). History
lives in a SQLite log addressed by sequence number. In the agent loop, the
model reaches that history by writing Python, and only what the code prints
enters the next model call. `ms.py` is the log, `kernel.py` is the persistent
Python namespace, and `eviction.py` is the paper's Algorithm 1.

**The curation layer.** This part is not in the paper. Agents propose, you
decide, and only approved items reach the doc. The doc stays small while the
log grows. Four columns carry it:

| Column      | Meaning                                                                       |
| ----------- | ----------------------------------------------------------------------------- |
| `promoted`  | Your decision. A rejected row stays readable at its `seq`.                    |
| `topic_key` | Groups claims about one thing. Two approved claims on one key are a conflict. |
| `base_seq`  | The doc version when the claim was written. `inbox` shows how far it moved.   |
| `scope`     | The repository a fact holds in. Empty for a general fact.                     |

The `naru` command and its hooks use the curation layer and the log. The agent
loop and the benchmark use the log and the kernel.

The [design decisions](docs/adr/) explain why each part works the way it does.
Their numbers skip 0015, which is not part of this repository.
[`how-it-works.html`](how-it-works.html) walks through the mechanism.

## Repository map

| Job | Files |
| --- | --- |
| Use naru with your agents | `naru.py` (the command and its hooks), `ms.py` (the log), `hook_spill.py` (the spill hook), `metrics.py` (data for `naru stats`), `codex/` (the Codex plugin) |
| Run the paper's agent loop | `agent.py`, `kernel.py`, `eviction.py`, `context_policy.py`, `jev.py`, `backend.py` (model backends) |
| Measure the agent loop and curation | `bench.py`, `beam.py`, `noise.py`, `regrade.py`, `curation_probe.py`, `experiment.py`, `test_judge.py`, `cases.jsonl`, `results/published/`, `.audit/` |
| Prove that the self-checks catch bugs | `test_mutations.py` |
| Read why it works this way | [`docs/adr/`](docs/adr/), [`how-it-works.html`](how-it-works.html), [`docs/benchmark.md`](docs/benchmark.md), [`docs/context-policy.md`](docs/context-policy.md) |
| Let an agent verify the command | [`.agents/skills/verify-naru/`](.agents/skills/verify-naru/SKILL.md) |

## Does it work?

The benchmark checks that this code behaves like the paper's system. It does
not claim a new result. This is the reference run: LongMemEval-S, 96
questions, Sonnet 5, judged by Haiku 4.5. The rows are in
[`results/published/`](results/published/), stripped of gold answers.

| Arm    | Correct | 95% CI | Largest context | Calls | Model cost per question |
| ------ | ------- | ------ | --------------- | ----- | ----------------------- |
| `naru` | 72/96   | 65-83% | 2,174 tokens    | 3.7   | $0.3547                 |
| `full` | 64/96   | 57-75% | 124,073 tokens  | 1.0   | $0.8036                 |
| `rag`  | 59/96   | 51-71% | 2,142 tokens    | 1.0   | $0.0753                 |

`full` puts the whole history in the prompt. `rag` pastes the top 8 keyword
hits. `naru` keeps the history in the log and lets the model query it with
code. The cost column excludes the judge, which costs about $0.027 per question
for every arm.

Read this table with care:

- No arm beats another at the corrected significance threshold. A smaller
  Haiku run puts `rag` ahead of `naru`.
- `naru` holds about 57 times less context than `full`, but `rag` is about 4.7
  times cheaper than `naru`.
- The judge is not LongMemEval's official prompt, so you cannot compare these
  numbers with published scores.

[Measure Naru](docs/benchmark.md) has the full method, per-category results,
and how to run the benchmark with other models. [Context policy and
Jev](docs/context-policy.md) covers the optional context gate in the agent
loop.

## Limitations

- **Conflict checks use exact keys.** Two contradictory claims under different
  `--key` values both appear as approved.
- **One database on one machine.** Two databases would break `seq` as a total
  order.
- **Search is BM25 keyword search**, as in the paper. The optional `rsm`
  benchmark arm uses embeddings but does not change runtime search.
- **The agent loop runs model-written code in your process** by default. This
  applies to `agent.py` and the benchmark. The `naru` command and its hooks do
  not run model-written code. `NARU_KERNEL=sandbox` moves the kernel to a child
  process with CPU, memory, and file-size limits and a timeout. That is process
  isolation, not a sandbox: the child has your user, filesystem, and network.
  Set `NARU_KERNEL_JAIL` to wrap it in `sandbox-exec`, `bwrap`, or a container.
  macOS refuses `RLIMIT_AS`, so memory is not capped there, and `limits()` says
  so. See [ADR 0007](docs/adr/0007-sandboxed-kernel.md).
- **Curation has no published production result yet.** It has self-checks and
  a [paired live-check harness](docs/benchmark.md#paired-curation-check).

## Contribute

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before you open a pull request. Every
module has an offline self-check. CI runs this set on Ubuntu and macOS, with
Python 3.9 and 3.13:

```bash
python3 ms.py && python3 kernel.py && python3 eviction.py && python3 agent.py
python3 naru.py --selfcheck && python3 hook_spill.py --selfcheck
python3 noise.py --selfcheck && python3 metrics.py --selfcheck
python3 backend.py --selfcheck && python3 bench.py --selfcheck
python3 beam.py --selfcheck && python3 regrade.py --selfcheck
python3 curation_probe.py --selfcheck && python3 triage_probe.py --selfcheck
python3 test_mutations.py   # breaks the code on purpose; the checks must fail
```

Some checks print error messages that they trigger on purpose. A check passed
when its last line starts with `ok`.

CI does not run these three offline checks. Run them when you change the agent
loop or the experiment tools:

```bash
python3 context_policy.py && python3 jev.py
python3 experiment.py --selfcheck
```

Two checks call a live model and cost money:

```bash
python3 backend.py      # two cheap calls; prints the harness token floor
python3 test_judge.py   # judge regression cases
```

## License

[MIT](LICENSE)
