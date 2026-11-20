# 2. Measure the harness token floor, never hardcode it

`bench.py --harness-floor` defaults to `None` and calls
`backend.measure_floor()` at startup. Hardcoding it produced a wrong published
conclusion once already.

## Context

The `claude` CLI sends its own system prompt with every call, so each call
bills input tokens before any of our prompt. To compare the `full` and `naru`
arms we must subtract that floor — `naru` uses ~3.4 calls per question and
`full` uses 1, so the floor is multiplied differently per arm.

The floor was measured once at **13,231** and hardcoded. Then ADR 0001's flags
changed (`--max-turns 1 --disallowed-tools …` became `--allowed-tools ""`) and
the real floor moved to **~22,616**. Nobody re-measured.

## Decision

Measure it at the start of every run, with the same model the run uses. Never
store the number in source.

## Why

With the stale floor the `_s` result read as "net input is a wash". With the
measured floor the same saved rows say `naru` uses **17% fewer** net input
tokens:

| floor             | full net-in/q | naru net-in/q |
| ----------------- | ------------- | --------------- |
| 13,231 (stale)    | 126,761       | 128,801         |
| 22,616 (measured) | 117,376       | 96,892          |

Same data, opposite conclusion. A measured constant that lives in source will
go stale silently and the report will keep looking plausible.

## Consequences

One extra cheap model call per benchmark run. `--harness-floor N` still forces a
value for reproducing an old run. Any result printed before this change should
be recomputed from its `results/*.json` rather than trusted.

## Later changes

`--harness-floor N` is checked before any paid call. A negative value is
refused. A value given for a backend that reports no token counts is refused
too, because it was dropped without a word.

`experiment.py` once required the two runs to have the same `harness_floor`.
Because each run measures its own floor, two honest runs differ by a few
tokens, and the only way to pass was to pin the floor by hand, which is what
this record says not to do. The floor is no longer a controlled variable in
that comparison.
