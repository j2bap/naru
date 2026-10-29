# Daily use

[README](../README.md) · [Agent setup](setup.md) · [Commands](commands.md)

Naru has two places for memory: a searchable local log and a document of facts
you approve. You can save more in the log than you want to load into every
agent session.

[Review](#review-your-inbox) · [Save](#save-a-fact-or-procedure) · [Find](#find-saved-context) · [Update](#change-an-approved-fact) · [Repository facts](#facts-for-one-repository) · [Large output](#keep-large-output-out-of-context)

## Review your inbox

```bash
naru inbox
```

Read each proposal, then type a choice or its first letter:

| Choice | Effect |
| --- | --- |
| **Promote** (`p`) | Add the fact to approved memory. |
| **Archive** (`a`) | Keep it out of approved memory, but leave it available as a lead. |
| **Reject** (`r`) | Mark it wrong and exclude it from automatic archive recall. |
| **Skip** (`s`) | Leave it pending for a later review. |

Approve facts that will help again, such as a tool quirk or a non-standard
working rule. Use a repository fact for a rule that applies to one project.
Each approved fact adds text to future sessions, so keep it focused.

Your decision updates registered instruction files. Hooks deliver a changed
document at the next prompt or session event. Run `naru inject` without a path
to inspect the current approved document.

## Save a fact or procedure

Agents with the memory hooks receive filing instructions. You can also file a
claim yourself:

```bash
naru claim "Naru CLI help is at the top level: run naru --help." \
  --key naru.help --by you
```

Use a short, stable `--key` to group facts about the same topic. `--by` records
who proposed the fact. The command prints its sequence number, which identifies
that entry in the store.

For a repeatable procedure, use `skill`:

```bash
naru skill "To review Naru memory, run naru inbox, decide each item, then run naru inject to inspect the approved document." \
  --key naru.review --by you
```

Both commands create pending items. Review them with `naru inbox`. Each claim
or skill can contain at most **400 UTF-8 bytes**. Save longer material as a
note instead:

```bash
naru add "release notes" < notes.md
```

Naru saves notes by paragraph so search can return the relevant part.

## Find saved context

```bash
naru search "Naru help"
naru outline
```

Search returns up to eight matches by default. Each result starts with a
sequence number. `outline` lists saved session ranges for browsing.

Use the number from your result to read the entry. For example, if it is `12`:

```bash
naru show 12
```

To read a range, use `naru show 12 18`. A call prints up to 20,000 characters
by default. If more remains, copy the continuation command Naru prints.

### Archive and reject

Archived and pending claims can still help as leads. On each prompt, the
Claude Code and Codex hooks can add up to three matching claims under
**From the Naru archive (unreviewed)**. The section includes dates and asks the
agent to verify the claims. A match needs two significant prompt words, or the
one significant word when the prompt has only one.

Rejected claims do not appear in this automatic recall. They remain in the
log, where an explicit search or `show` can still find them. To change a
rejected claim back into an archived lead, use `naru drop SEQ --yes` with its
sequence number.

## Change an approved fact

Retire the old fact, then propose and approve its replacement. First find the
old sequence number with `naru search`. If that number is `12`:

```bash
naru drop 12 --yes
```

File the replacement with the same `--key`, then review it with `naru inbox`.
Use the actual sequence number from your store whenever you run a decision
command.

If two approved claims share the same key and scope, Naru puts both under
`## Unresolved`. It leaves the choice to you. Use `naru drop` to retire the
old version, or `naru reject` if it is wrong. Claims and skills have separate
key groups. Naru does not detect contradictions between different keys.

`promote`, `drop`, `reject`, and `check` require `--yes` when they run without
a terminal. This prevents accidental scripted decisions. It is not an access
control: an agent that can run the command can also pass `--yes`.

## Facts for one repository

File the claim from inside its Git repository and add `--repo`. For example,
from your Naru checkout:

```bash
naru claim "Naru's offline store check is: python3 ms.py." \
  --key checks.store --by you --repo
```

Review it with `naru inbox`. The Claude Code and Codex hooks deliver approved
repository facts only for the matching repository, under
`## This repository (owner/name)`.

Naru derives `owner/name` from the `origin` URL. With no remote, it uses the
repository folder name. It does not write repository facts into the general
document or any injected file. A file-only integration therefore does not
receive them.

## Check for stale facts

If a fact depends on a tool or file that can change, attach a command whose
output will change with it. For an approved claim at sequence `12`:

```bash
naru check 12 "python3 --version" --yes
```

This runs the command and saves its output. You can also attach a check while
filing a claim with `--check "COMMAND"`; that check first runs when you approve
the claim.

Each `naru inbox` reruns checks on approved items. If the saved output differs,
Naru lists the fact as `[stale]`. Choose **keep** to confirm it and save the new
output, **drop** to archive it, or **skip** to leave it for later.

Check commands run from your home directory, without a shell, for at most
10 seconds. Use absolute file paths. Shell pipes, redirects, and variable
expansion do not work unless you explicitly invoke a shell. Review the command
before you approve a claim that carries one.

## Keep large output out of context

Pipe saved output through `naru spill`:

```bash
naru spill --tool build < build.log
```

Output of up to 2,400 characters passes through unchanged. Larger output goes
into the log. Naru returns a preview with the first and last 300 characters,
line signposts, and a `naru show` command to recover the text.

Copy that recovery command when you need the full result. `show` prints in
pages and tells you how to continue. To spill a live command, pipe its output
to `naru spill`; use your shell's pipeline exit handling if the original
command's failure status matters.

Claude Code can do this through an
[optional output hook](setup.md#optional-claude-code-output-hook). The Codex
plugin does not replace tool output.

```bash
naru stats
naru stats 7
```

These commands show spill and recovery counts for all recorded time or the
last seven days. A spill counts as reopened only when a later `show` covers
that exact spill. See [settings](commands.md#settings) to change the threshold
and preview size. Apply the same settings to the hook and CLI so their reports
agree.

## Clean up old log entries

Preview the effect before you delete old rows:

```bash
naru prune 30 --dry-run
```

If the preview matches what you want, run `naru prune 30` to delete eligible
rows older than 30 days. This can include pending claims, notes, and spilled
output. Naru preserves decided claims and skills, including archived and
rejected ones. It also preserves log evidence cited by approved items.

Pruning runs only when you request it. The default age is 30 days if you omit
the number. `naru gc` removes orphaned payload files that no remaining row
references.

## Connect your agent

Use the [setup guide](setup.md) for hooks, plugins, and instruction files.
For every command and option, use the [command reference](commands.md).
