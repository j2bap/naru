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

Naru is one folder of Python with no dependencies.

## Quick start

You need Python 3.9 or later and a SQLite build with FTS5. Most Python builds
include it.

```bash
git clone https://github.com/j2bap/naru
cd naru
python3 ms.py                             # self-check, offline, writes only to a temp dir
ln -s "$PWD/naru.py" ~/.local/bin/naru    # or any folder on your PATH
```

The symlink matters. Your agents run `naru` from their own project folders, so
the command must be on your `PATH`.

Now try the loop. An agent files a claim:

```console
$ naru claim "Run tests with: make test" --key tests --by claude
claim 1 pending — `naru inbox` to decide
```

You review the inbox:

```console
$ naru inbox
[1/1] claim seq 1 · claude · 2026-10-15T16:25 · key: tests
  + Run tests with: make test
  promote / drop / skip ?
```

You type `promote`, or just `p`. Then you write the approved doc into your context file:

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

## Connect your agent

### Tell your agent to file claims

Add a short instruction to your context file, outside the Naru markers:

```markdown
## Naru

File one lasting fact per claim:

    naru claim "<text>" --key <topic> --by <agent>

For a repeatable procedure, use `naru skill` instead of `naru claim`.
```

### Pick the file your harness reads

```bash
naru inject CLAUDE.md        # Claude Code
naru inject AGENTS.md        # Codex, DeepSeek Harness, most others
naru inject .cursorrules     # Cursor
```

Run `inject` again after you approve new claims. Without a path, `naru inject`
prints the doc to stdout.

### Codex plugin

The Codex plugin loads the approved doc for you, so you do not need to run
`inject`. Install it after `naru` is on your `PATH`:

```bash
codex plugin marketplace add "$PWD/codex"
codex plugin add naru-codex@naru
```

Start a new Codex thread and trust the plugin hooks when Codex asks. The plugin
loads the approved doc at session start, refreshes it after you promote or
retire a claim, and gives the same context to subagents.

The plugin stores only the last doc sequence and fingerprint that each Codex
session saw. That data lives in a `context_delivery` table, not in the
searchable log.

The plugin does not replace Codex tool results. In Codex 0.152, the only
working way to replace a result marks the tool as blocked after it already ran,
which can make an agent repeat a write or a request.

### Claude Code hook for large output

A single command can print thousands of lines into your agent's context.
`hook_spill.py` is a `PostToolUse` hook that moves large tool output into the
Naru log and leaves a short preview with a recovery handle in its place. The
agent runs `naru show <seq>` if it needs the full text.

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
spill.

## Commands

| Command                                 | What it does                                   |
| --------------------------------------- | ---------------------------------------------- |
| `naru claim "<text>" [--key K] [--by A]` | Propose a fact.                                |
| `naru skill "<procedure>" [--key K]`    | Propose a repeatable procedure.                |
| `naru inbox`                            | Approve, reject, or skip pending items.        |
| `naru inject [PATH]`                    | Write the approved doc to a file or stdout.    |
| `naru promote SEQ --yes`                | Approve without the prompt.                    |
| `naru drop SEQ --yes`                   | Reject or retire without the prompt.           |
| `naru add "<topic>" < notes.md`         | Save a note to the log.                        |
| `naru search QUERY`                     | Find matching lines in the log.                |
| `naru show SEQ [SEQ]`                   | Print one entry or a range.                    |
| `naru outline`                          | Show an index of the log.                      |
| `naru stats [DAYS]`                     | Show spill and recovery counts.                |
| `naru prune [DAYS] [--dry-run]`         | Delete old pending claims and log rows.        |
| `naru gc`                               | Remove orphaned blobs from this store.         |

Everything lives in `~/.naru/log.db`. Set `NARU_DB` to use another file.

`promote` and `drop` refuse to run without a terminal unless you pass `--yes`.
This stops a tool loop from approving by accident. It is not a security
boundary, because anything that can run the CLI can also pass `--yes`.

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
log grows. Three columns carry it:

| Column      | Meaning                                                                       |
| ----------- | ----------------------------------------------------------------------------- |
| `promoted`  | Your decision. A rejected row stays readable at its `seq`.                    |
| `topic_key` | Groups claims about one thing. Two approved claims on one key are a conflict. |
| `base_seq`  | The doc version when the claim was written. `inbox` shows how far it moved.   |

The [design decisions](docs/adr/) explain why each part works the way it does.
[`how-it-works.html`](how-it-works.html) walks through the mechanism.

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
- **The kernel runs model-written code in your process** by default.
  `NARU_KERNEL=sandbox` moves it to a child process with CPU, memory, and
  file-size limits and a timeout. That is process isolation, not a sandbox:
  the child has your user, filesystem, and network. Set `NARU_KERNEL_JAIL` to
  wrap it in `sandbox-exec`, `bwrap`, or a container. macOS refuses
  `RLIMIT_AS`, so memory is not capped there, and `limits()` says so. See
  [ADR 0007](docs/adr/0007-sandboxed-kernel.md).
- **Curation has no published production result yet.** It has self-checks and
  a [paired live-check harness](docs/benchmark.md#paired-curation-check).

## Contribute

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before you open a pull request. Every
module has an offline self-check:

```bash
python3 ms.py && python3 kernel.py && python3 eviction.py && python3 agent.py
python3 naru.py --selfcheck && python3 hook_spill.py --selfcheck
python3 noise.py --selfcheck && python3 metrics.py --selfcheck
python3 backend.py --selfcheck && python3 bench.py --selfcheck
python3 context_policy.py && python3 jev.py
python3 experiment.py --selfcheck
python3 beam.py --selfcheck && python3 regrade.py --selfcheck
python3 curation_probe.py --selfcheck
python3 test_mutations.py   # breaks the code on purpose; the checks must fail
```

Two checks call a live model and cost money:

```bash
python3 backend.py      # two cheap calls; prints the harness token floor
python3 test_judge.py   # judge regression cases
```

## License

[MIT](LICENSE)
