# Command reference

[README](../README.md) · [Agent setup](setup.md) · [Daily use](usage.md)

Run `naru --help` for the CLI overview. Help exists at the top level only;
`naru claim --help`, for example, can file `--help` as claim text.

`SEQ` is an entry's sequence number. Replace uppercase placeholders below with
your own values. Text with spaces belongs inside quotes.

## Propose memory

```text
naru claim "TEXT" [OPTIONS]
naru skill "PROCEDURE" [OPTIONS]
```

Both commands accept text from standard input if you omit the text argument.
Both create pending items with a 400-byte UTF-8 limit.

| Option | Meaning |
| --- | --- |
| `--key TOPIC` | Group related items. Multiple approved items of the same kind, key, and scope become unresolved. |
| `--by AGENT` | Record the author. |
| `--repo` | Scope the item to the current Git repository. Requires a repository; hooks deliver these items. |
| `--check "COMMAND"` | Attach a stale check. It first runs on promotion. |
| `--base SEQ` | Record the document version you used. Defaults to the current approved version. |
| `--run ID --source LO:HI` | Cite a log range from a run you opened. Use these options together. |

For a cited claim, first read the evidence with
`naru show LO HI --run ID`. Naru checks for a recorded read of that range in
the same store and run. A partial character slice does not count as a full
read.

## Review and decide

| Command | Effect |
| --- | --- |
| `naru inbox` | Review pending items and stale checks. |
| `naru promote SEQ --yes` | Approve a pending claim or skill. |
| `naru drop SEQ --yes` | Archive an item, or retire it from approved memory. |
| `naru reject SEQ --yes` | Mark an item wrong and exclude it from automatic archive recall. |
| `naru check SEQ "COMMAND" --yes` | Run and attach a stale check to an approved item. |

The direct commands act immediately. They require `--yes` when standard input
is not a terminal. Use `inbox` for an interactive review. The terminal check
prevents accidental use; it does not stop an agent from deciding if it can run
the same command with `--yes`.

Promotion works on pending items. To revise or restore an old decision, file a
new proposal and review it. `drop` can move a rejected item back to the archive.

## Deliver approved memory

| Command | Effect |
| --- | --- |
| `naru inject` | Print the general approved document. |
| `naru inject PATH` | Update the marked block in a file and register it for later refresh. |
| `naru claude-hook` | Process a Claude Code hook event from standard input. |
| `naru codex-hook` | Process a Codex hook event from standard input. |

Create the parent directory before `inject PATH`. The command keeps text
outside the Naru block. Future decisions refresh registered files that still
have both markers. Remove the block to stop updates.

`inject` excludes repository facts. Hooks add facts for the session's matching
repository. See [agent setup](setup.md) for the event configuration.

## Save and recall

| Command | Effect |
| --- | --- |
| `naru add "TOPIC" "TEXT"` | Save a note, split into paragraphs. |
| `naru add "TOPIC" < FILE` | Save a note from a file. |
| `naru search "QUERY" [COUNT]` | Find matching log entries. Default: eight results. |
| `naru outline` | List saved session ranges. |
| `naru show LO [HI] [--run ID] [--at CHAR]` | Read an entry or inclusive range. Optionally filter by run or start at a character offset. |

Search uses SQLite FTS5 keyword matching. Results show short excerpts; use
`show` to read the full entry. `show` prints up to 20,000 characters by default
and supplies a continuation command when needed.

## Store large output

```text
naru spill [--tool NAME] [--session ID] < FILE
naru stats [DAYS]
```

`spill` passes through text up to the threshold. For larger text, it stores the
full output and prints a preview with a recovery command. Tool and session
labels help identify the source. `stats` reports spill and recovery activity;
omit `DAYS` to use all recorded metrics.

See [large output](usage.md#keep-large-output-out-of-context) for the workflow
and the optional Claude Code hook.

## Maintain the store

| Command | Effect |
| --- | --- |
| `naru prune [DAYS] --dry-run` | Preview eligible rows older than `DAYS`, which defaults to 30. |
| `naru prune [DAYS]` | Delete those rows, clean the search index, and trim old metrics. |
| `naru gc` | Remove this store's orphaned payload files. |

Pruning is manual. It preserves all decided claims and skills, plus evidence
cited by approved items. Pending claims, notes, and tool output can be deleted.

## Settings

Set these environment variables in the terminal or process that runs Naru:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NARU_DB` | `~/.naru/log.db` | Store path. Use the same value for the CLI and hooks. |
| `NARU_METRICS` | `~/.naru/metrics.jsonl` | Spill, recovery, and delivery metrics. This path is separate from `NARU_DB`. |
| `NARU_SPILL_THRESHOLD` | `2400` | Character count above which output spills. |
| `NARU_SPILL_PREVIEW` | `600` | Total characters retained from the start and end, before signposts and recovery text. |
| `NARU_SHOW_CHARS` | `20000` | Maximum text printed by one `show` call. |

Apply spill settings to both the hook and the CLI. Otherwise, `stats` can use
a different threshold from the one that produced the spills.

The legacy `NARU_NOTES` variable overrides the CLI's store path.
`NARU_SPILL_DB` can separately override the output hook's store. Leave these
unset for a single shared store. The quick-start demo unsets `NARU_NOTES` so it
cannot select your normal store by mistake.

For research-only settings, see the [benchmark](benchmark.md) and
[context policy](context-policy.md) guides.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command completed. |
| `1` | No search hits, an empty outline, or a decision that could not apply to that item. |
| `2` | A handled input or usage error, such as a claim that exceeds 400 bytes. |

These are the CLI's handled cases. Other failures can print a Python error.
Hooks catch errors so a Naru failure does not stop the agent.
