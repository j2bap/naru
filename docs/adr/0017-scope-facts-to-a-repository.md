# 0017. Scope facts to a repository

## Context

Every promoted fact reached every session in every repository. On 2026-10-19,
6 of the 25 promoted facts applied only to Imoten, and a Haiku sort of all 503
claims labeled 62% of the dropped ones as repository facts. Many of those were
not wrong. They had no place to go except the general doc.

A repository's own `AGENTS.md` cannot hold its facts. That file is committed,
and in `j2bap/imoten` it also flows into the public export, so a private fact
would leak.

Claude Code had no hook, so it saw only the block in `~/AGENTS.md`. It also
reads `AGENTS.md` only when no `CLAUDE.md`, `.claude/CLAUDE.md`, or
`CLAUDE.local.md` exists in the working directory or a parent, so in such a
repository it saw no Naru doc at all.

## Decision

- `naru claim --repo` stores the current repository's id in a `scope` column.
  The id is `owner/name` from the `origin` URL, so it matches across
  workstations. A repository without a remote uses its folder name.
- `ms.doc()` renders only general facts, as before. `ms.repo_doc(id)` renders
  one repository's facts. `inject` writes only the general doc to files.
- One hook serves both harnesses: `naru codex-hook` and `naru claude-hook`. It
  reads `cwd` from the hook input and sends the general doc plus that
  repository's section. Each harness keeps its own delivery state.
- At `SessionStart`, when a file the harness loads already holds the exact
  general doc, the hook sends only the filing line and the repository section.
  For Claude Code that file list follows Claude's own rule: `CLAUDE.md`,
  `.claude/CLAUDE.md`, and `CLAUDE.local.md` up the path, and `AGENTS.md` only
  when none of those exist.

## Why

Delivering repository facts by hook keeps them out of every committed file.
The general doc stays the one thing written to disk, so ADR 0016 still covers
every file-based harness. Reading `cwd` from the hook input works for both
harnesses without a per-repository setup step.

## Limits

Claude Code caps `additionalContext` at 10,000 characters. The full doc plus
the filing line was 5,616 characters on 2026-10-19. Past the cap, Claude Code
shows the model only a 2,000-character preview. Conflicts are checked within
one scope, so a general fact and a repository fact on the same key do not
appear under `## Unresolved`.
