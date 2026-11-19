# 0020. Replay inbox decisions before automating them

## Context

A person decides every claim in `naru inbox`. Automating that step was
proposed on 2026-11-19. The store that day held 623 decided claims and nothing
else: 61 promoted, 145 archived, 417 rejected. From 2026-10-01 the count was
44 promoted, 77 archived, and 5 rejected, about 2.5 decisions a day.

Rules that need no model were scored against those decisions first:

| Rule | Result |
|---|---|
| Exact duplicate of an earlier claim | 7 of 623, and 4 of the 7 were promoted |
| Word overlap of 0.6 or more with an earlier claim | 5 of 623 |
| Filed with `--repo` | 34 claims: 29 archived, 4 promoted, 1 rejected |
| Filed with `--check` | 5 claims: 4 promoted, 1 archived |

No rule separates a promoted claim from an archived one. That choice is the
promote rule itself: the fact helps in many repositories and an agent would
not guess it. Only a judge that reads the claim can apply it.

The two errors do not cost the same. An archived claim stays searchable
(ADR 0019). A promoted claim enters every session.

## Decision

`triage_probe.py` replays decided claims through a model judge and writes no
verdict. It opens the store read-only. The judge sees the claim kind, the
claim text, and whether it was filed for one repository. It does not see the
verdict or the repository name. It answers `PROMOTE`, `ARCHIVE`, or `REJECT`
with a confidence.

The report gives, for each confidence threshold, how many claims the judge
would decide, how many it would promote, how many of those the person did not
promote, and how many promoted claims it would keep out. A reply that cannot
be read is a lost call and leaves the count, as an errored row leaves the
benchmark pairing.

With `--examples N` the judge also sees the N newest promoted and the N
newest not-promoted decisions made before the claim it is judging. A later
decision is never an example.

`--pending` judges the claims still in the inbox and prints a suggestion for
each. `--pending --apply --threshold T` archives a claim when the judge's
verdict is `ARCHIVE` or `REJECT` at confidence T or more. It never promotes
and never rejects. The threshold has no default: the owner reads it from a
replay with the same model and the same number of examples. Each run that
archives writes one `triage` line to the metrics file with the archived
sequence numbers, the model, and the threshold.

## What the first replay measured

126 decisions filed from 2026-10-01: 44 promoted, 77 archived, 5 rejected.
Always answering archive agrees with 77.

| Judge | Same verdict | Promotes | Wrong promotes | Missed promotes |
|---|---|---|---|---|
| Haiku 4.5, rule only | 74 | 9 | 5 | 40 |
| Haiku 4.5, 12 examples per side | 77 | 21 | 7 | 30 |
| Sonnet 5.5, 12 examples per side | 91 | 36 | 11 | 19 |

At confidence 0.8 or more:

| Judge | Claims kept out | Of those, promoted by the person | 95% interval |
|---|---|---|---|
| Haiku 4.5, rule only | 85 | 23 | 19% to 37% |
| Haiku 4.5, 12 examples per side | 46 | 6 | 6% to 26% |
| Sonnet 5.5, 12 examples per side | 59 | 2 | 1% to 12% |

No judge promoted a claim at confidence 0.8 or more with Sonnet, and about a
third of every judge's promotions were wrong. The runs cost $0.59, $1.25, and
$1.96. Each is one run, and the threshold was read from the same 126 claims,
so the 2 of 59 is an optimistic estimate.

## Why

Past decisions are the only labelled data, and replaying them costs one small
model call per claim and changes nothing. The replay shows that ADR 0004's
rule holds: a person promotes. No judge promoted reliably. The only verdict a
judge made reliably was keeping a claim out, and only the stronger model with
examples did.

Archive is the one automatic verdict because its error is the cheap one. An
archived claim that deserved promotion is still returned by archive search
(ADR 0019). A claim has no path from archived back to pending, so the person
does not see it in the inbox again. That is why the action needs `--apply`
and a threshold on every run and is never a default.

## Limits

Rejections before 2026-10-19 came from one bulk pass, so use `--since` to
score recent decisions. The judge sees the claim alone, not the doc as it
stood when the person decided, so it cannot detect a claim that repeats or
contradicts a promoted fact. Duplicates were about 1% of claims. The judge
model and the claim author can be the same model family, which can inflate
agreement on that model's own claims.
