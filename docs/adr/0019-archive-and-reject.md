# 0019. Split a drop into archive and reject, and search the archive

## Context

A dropped claim left the doc and was never shown again unless an agent ran
`naru search`. On 2026-10-19, 16 tasks built from dropped claims that a Haiku
pass labeled general measured the cost (Haiku, tools off, 3 repeats):

| Arm | Approved facts | Dropped facts | Input tokens per call |
|---|---|---|---|
| Naru doc | 100% | 54% | 2,568 |
| OptMem, all 507 claims | 88% | 94% | 12,319 |
| Naru doc plus 5 archive hits per prompt | 100% | 100% | 3,049 |

Pasting archive hits also brought back noise and harm. Unfiltered, 61% of the
hits were repository facts and 14% were labeled wrong. A stale claim, true when
filed, was repeated in 1 of 3 runs.

A drop had no reason attached, so search could not tell "true, but not for
every session" from "wrong".

## Decision

- `naru drop` keeps its meaning: the claim leaves the doc and stays in the
  archive (`promoted=-1`). `naru reject` marks it wrong (`promoted=-2`), and
  archive search never returns it. `drop` on a rejected claim archives it
  again, so a rejection can be undone. `inbox` offers promote, archive,
  reject, and skip.
- `ms.archive(prompt, scope)` returns up to 3 pending or archived claims,
  general or from the current repository. It runs an OR query and keeps a
  claim only if it holds at least two distinct significant words from the
  prompt, or the only one when the prompt has one.
- On `UserPromptSubmit`, the hook adds those claims under "From the Naru
  archive (unreviewed)", with each claim's date, capped at 1,500 characters.
  The section never goes into a file and never into the general doc.

## Why

The doc stays the set of facts a person approved, and the archive becomes a
labeled lead instead of a lost record. The two-word rule keeps ordinary turns
clean: 1 of 10 generic prompts got a hit. The date lets the model weigh age,
which is the remaining guard against a stale claim.

## Limits

The tasks were written from the claims, so they share the claims' words. Real
prompts will match less often. A true-when-filed claim that went stale is not
rejected by anyone until a person finds it. Measure the share of useful hits
in real sessions before raising `k` or the character cap.
