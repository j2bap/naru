# Use Naru

[Back to the quick start](../README.md#-get-started).

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
spill.

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

