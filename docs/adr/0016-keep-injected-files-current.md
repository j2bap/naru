# 0016. Keep injected files current

## Context

`naru inject <path>` wrote the doc once. A file-based harness, such as Claude
Code reading `~/AGENTS.md`, saw a new promotion only after someone ran `inject`
again. On 2026-10-19 that file held the doc at seq 1652 with 10 decisions. The
live doc was at seq 3849 with 25. Codex stayed current only because its plugin
reloads the doc through hooks (ADR 0009).

## Decision

`inject` records the real path of each file it writes in an `inject_target`
table in the store. Every verdict goes through one helper, `_decide`, which
`promote`, `drop`, and `inbox` all call. When a verdict changes a row, the
helper splices the new doc into each recorded file.

A recorded file that is gone, unreadable, or no longer has both markers is
forgotten and left alone. Deleting the markers is how you opt a file out.
Both markers in the wrong order do not make a block, so that file is forgotten
too. A file that has a block but cannot be written stays recorded: Naru prints
a warning and updates the other files, because the verdict is already saved.

## Why

The CLI already owns the doc and the decisions, so it is the one place that
knows when the doc changes. Refreshing at the decision covers every file-based
harness, not only the ones with a plugin. The marker rule keeps ADR 0005's
promise that `inject` never takes over a file it does not own: Naru writes only
between markers the user left in place. The real path stops a file reached
through two symlinks from being recorded twice.

## Bound to one store

Each recorded file also stores the `store_id` of the store that wrote it (ADR
0011), and a decision rewrites only its own store's files. On 2026-10-19 a
simulation run against a backup copy, through `NARU_NOTES`, rewrote the real
`~/AGENTS.md` with the simulated doc, because the copy carried the same file
list. A row recorded before this column existed matches no store until
`inject` writes that file again.
