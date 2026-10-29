# Connect your agent

[README](../README.md) · [Daily use](usage.md) · [Commands](commands.md)

Install Naru with the [quick start](../README.md#install), then choose the
section for your agent. Use the same Naru store for each agent you want to
share memory with.

| Agent | Recommended setup | How memory arrives |
| --- | --- | --- |
| Claude Code | [Memory hooks](#claude-code) | At session start, on a later prompt when memory changes, and for subagents. |
| Codex | [Bundled plugin](#codex) | Through the same lifecycle events. |
| Cursor | [Project instruction file](#cursor) | When Cursor reads that file. |
| Other agents | [Instruction file](#other-agents) | When the agent reads that file. |

## Claude Code

Merge this `hooks` configuration into `~/.claude/settings.json`. Keep any
existing hooks and settings. `naru` must be on the `PATH` that Claude Code uses.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [{"type": "command", "command": "naru claude-hook", "timeout": 5}]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [{"type": "command", "command": "naru claude-hook", "timeout": 2}]
      }
    ],
    "SubagentStart": [
      {
        "hooks": [{"type": "command", "command": "naru claude-hook", "timeout": 5}]
      }
    ]
  }
}
```

Start a new session, then [check the connection](#check-the-connection).

The hooks load approved memory and explain how to propose a claim. They also
give memory to subagents and ask them to report new facts to their parent
agent. If the same approved document is already in a loaded instruction file,
the session-start hook avoids sending a second copy.

<details>
<summary>Use a file instead of hooks</summary>

Write approved memory to your global Claude Code instruction file:

```bash
mkdir -p "$HOME/.claude"
naru inject "$HOME/.claude/CLAUDE.md"
```

Then add the [claim instructions](#tell-your-agent-to-file-claims) outside the
Naru block. The file contains general memory. Repository facts need hooks.

</details>

## Codex

From the Naru checkout, add the bundled marketplace and plugin:

```bash
codex plugin marketplace add "$PWD/codex"
codex plugin add naru-codex@naru
```

Start a new Codex thread. If Codex asks you to trust the plugin hooks, review
and accept them to enable the integration. Then
[check the connection](#check-the-connection).

The plugin calls `naru codex-hook`. It loads approved memory, explains claim
filing, and sends changes on the next prompt. Subagents receive the same
memory and report new facts to their parent agent. You do not need to run
`inject` for this setup.

<details>
<summary>Use a file instead of the plugin</summary>

Write approved memory to the `AGENTS.md` in your Codex home:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}"
naru inject "${CODEX_HOME:-$HOME/.codex}/AGENTS.md"
```

Then add the [claim instructions](#tell-your-agent-to-file-claims) outside the
Naru block. The file contains general memory. Repository facts need the
plugin's hooks.

If you use both methods, the session-start hook detects an identical block in
this file and sends only the filing instructions and any repository facts.

</details>

The bundled Codex plugin delivers memory only. It does not replace tool
results. Use [`naru spill`](usage.md#keep-large-output-out-of-context) when you
want to shorten a large command result.

## Cursor

Use the project rule file your Cursor setup reads. For a setup that reads
`.cursorrules`, run this from each project:

```bash
naru inject .cursorrules
```

Add the [claim instructions](#tell-your-agent-to-file-claims) outside the Naru
block. Start a new session so Cursor reads the file. If your setup uses another
instruction file, pass that path to `naru inject` instead.

## Other agents

Point `naru inject` at the instruction file your agent reads. For DeepSeek
Harness or another setup that reads `AGENTS.md`:

```bash
naru inject AGENTS.md
```

Add the [claim instructions](#tell-your-agent-to-file-claims) below. Your agent
needs shell access to propose facts and search the log. It needs only file
access to read memory that you already injected.

## Tell your agent to file claims

The Claude Code hooks and Codex plugin supply these instructions. For a file
setup, add this text **outside** the `<!-- naru:begin -->` and
`<!-- naru:end -->` markers:

```markdown
## Naru

File one lasting fact per claim:

    naru claim "<text>" --key <topic> --by <agent>

File a fact an agent would not guess, such as a tool quirk or a non-standard
rule. I review proposals with `naru inbox`. Only approved facts enter the
shared document. Do not promote claims or edit the Naru block yourself.

Use `naru skill` for a repeatable procedure. Keep each claim or skill within
400 UTF-8 bytes. Use `naru search "<terms>"` to find saved context.
Subagents report lasting facts to their parent instead of filing claims.
```

Use `--repo` for a fact that applies to one repository. Those facts reach
sessions through the Claude Code or Codex hooks; `inject` does not write them
to files.

## How injected files stay current

Run `naru inject PATH` once for each file. Naru creates or updates its marked
block, keeps the rest of the file, and registers the path in the store.
Later decisions update registered files that still have both markers.

To stop updates, remove the entire Naru block from the file. Naru leaves the
file alone at the next refresh. To reconnect it, run `inject` again.

Create the parent directory before you inject into a new path. If an
instruction file is committed, its approved memory is committed too.

## Check the connection

1. Run `naru inject` in your terminal to see the approved document. If it says
   `(nothing promoted yet)`, use `naru inbox` to approve a useful fact first.
2. Start a new agent session. Ask which approved Naru facts it received.
3. Compare the answer with the document from step 1.

If memory is missing, check that the agent can run `naru --help`. Confirm that
the agent and your terminal use the same `NARU_DB` setting. With a file setup,
confirm that the agent reads the exact file you passed to `inject`.

## Optional Claude Code output hook

The memory hooks above deliver facts. This separate hook stores large tool
results and returns a short preview with a command to recover the full text.
You can also [spill output yourself](usage.md#keep-large-output-out-of-context)
without a hook.

Merge this entry into the `hooks` object in your Claude Code settings. Replace
`/path/to/naru` with the absolute path to your checkout:

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

The hook handles supported text fields in tool responses. If storing a result
fails, it leaves the result inline. Use `naru stats` to inspect spills and
later recovery. See the [spill settings](commands.md#settings) to change sizes.

## Installation help

| Problem | What to do |
| --- | --- |
| `No module named sqlite3` or an FTS5 error | Use a Python build with SQLite and FTS5 support. Re-run `python3 ms.py`. |
| `naru: command not found` | Run `export PATH="$HOME/.local/bin:$PATH"` and add that line to your shell profile. Check with `command -v naru`. |
| `ln` reports `File exists` | Inspect the existing path with `ls -l "$HOME/.local/bin/naru"`. Keep your current install or remove that link deliberately before creating another. |
| A symlink stops working after you move the checkout | Recreate the link so it points to the new absolute path of `naru.py`. |
| Codex does not recognize `plugin` | Use the file setup above, or use a Codex release with the plugin commands shown by `codex plugin --help`. |
| `inject` reports that a folder does not exist | Create the intended parent directory, then retry with the same file path. |

For available commands, use `naru --help`. Subcommands do not have separate
`--help` handlers.
