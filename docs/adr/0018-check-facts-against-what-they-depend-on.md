# 0018. Check facts against what they depend on

## Context

A promoted fact stays in every session until someone retires it, and nothing
told that person when it went out of date. On 2026-10-19, 6 of 25 promoted
facts needed a recheck: one named Codex 0.154 while 0.163 was installed, and
five described Imoten files that changed after the facts were filed. Two of
those five named receipt fields that no commit of the runner ever held. MemTrace (arXiv 2610.04838) validates recalled evidence against the
current repository before reuse, and gains 21.2 points on DeepSWE under Codex
CLI. Most Naru facts are general, so a file link alone covers few of them. A
command covers both versions and files.

## Decision

- `naru claim --check "<command>"` stores a command with the claim. `inbox`
  shows it before you decide.
- When a fact is promoted, Naru runs the command and stores its output as the
  confirmed baseline. `promote` prints that output.
- `naru check SEQ "<command>" --yes` attaches a check to a fact that is already
  promoted, and records today's output as confirmed. Run it only after you
  confirm the fact holds today.
- `naru inbox` runs every check. A fact whose output changed appears as
  `[stale]` with the old and new output. `keep` records the new output as
  confirmed. `drop` retires the fact.
- A command runs without a shell, from the home folder, with `~` expanded, for
  at most 10 seconds. Output is capped at 1,000 characters.
- A command that fails confirms nothing. A failure is a non-zero exit, a
  timeout, or a command that cannot start. `promote` still promotes the fact
  but records no baseline, and `inbox` lists the fact as `[stale]` with
  `was: (never confirmed)` until you keep or drop it. `naru check` refuses a
  failing command. Before this rule, the error text became the baseline and
  the fact read as confirmed until the day the command worked.

## Why

The check runs at the human decision points that already exist, so no
background process or schedule is needed. Running from the home folder keeps a
relative command from giving a different answer in each folder.

## Limits

An agent writes the command and Naru runs it later, on your machine. You see
the command in `inbox` and its output at `promote`, and `naru check` needs a
terminal or `--yes`. This is a review step, not a sandbox. A check proves only
that its output is unchanged. It cannot tell whether the fact itself is still
true.
