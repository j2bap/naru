# Measure Naru

This page explains how to run the benchmark, how to read its numbers, and how
to check whether approved context helps real tasks. For a short summary, see
the [README](../README.md#does-it-work).

## The benchmark

`bench.py` runs [LongMemEval](https://arxiv.org/abs/2410.10813) (ICLR 2025)
over four arms on identical history:

| Arm    | What fills the prompt                                               |
| ------ | ------------------------------------------------------------------- |
| `full` | The whole history in one prompt.                                    |
| `rag`  | The top 8 BM25 hits, answered in one call.                          |
| `naru` | Nothing. The history stays in the log and the model reaches it with code. |
| `rsm`  | Grouped dense-retrieval chunks, answered in one call.               |

The default arms are `full,rag,naru`. `rag` is the control. It holds the
kernel fixed and changes only what fills the prompt, so it separates
"programmatic access wins" from "any retrieval beats stuffing". Do not drop it.

```bash
mkdir -p data && cd data
curl -LO https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
cd ..
python3 bench.py --split oracle -n 12
```

All arms report estimated total and peak prompt tokens, including the system
text and retries. Naru also reports its bounded dynamic-view peak separately.

### What the benchmark is for

It checks whether this implementation behaves like the one in the paper. It is
not a contribution to the field, and the numbers are not leaderboard-comparable.
The judge is not LongMemEval's official prompt. The paper already reports 94.8
on LongMemEval-S, 73.1 on BEAM-10M and 86.7 on LOCA-256K against ten named
baselines. Nothing here improves on that. The benchmark tells you whether a
change to this repo broke something.

### The `rsm` arm

The `rsm` arm needs `NARU_EMBED`, so it stays out of the default command. The
command reads one JSON object on stdin with a `texts` array and writes one JSON
object with a same-length `vectors` array. Naru validates and normalizes every
vector. It records the command's program name only, not its arguments.

```bash
NARU_EMBED='./embed-bge-large' python3 bench.py --arms rag,rsm --split oracle -n 12
```

The `rsm` arm defaults to six atoms, five turns per chunk, a 0.85 merge
threshold, and a 4,000-token source budget. The 0.85 value is the paper's BGE
setting. You must calibrate it for another embedding space. The report records
embedding input tokens, atoms, selected members, timing, source tokens, and
estimated prompt tokens. Model cost does not include embedding-provider cost,
because the harness cannot know it.

## The reference run

The accuracy and cost table for the reference run is in the
[README](../README.md#does-it-work). It lives there only, so the two pages
cannot drift apart. This section has the statistics behind it.

"Largest context" is the largest view the model ever held, as measured by the
harness. The cost column is the arm's own cost. It excludes the judge, so it
measures the arm and not the grader.

```
separability — paired McNemar, Bonferroni for 3 pairs
    dropped from the pairing (run errored): full 5
    full  vs rag     9.9 pts to full   full only 14, rag only  5   p=0.064   not separable
    full  vs naru    3.3 pts to naru   full only 11, naru only 14  p=0.690   not separable
    rag   vs naru   13.5 pts to naru   rag  only  9, naru only 22  p=0.029   not separable
```

Three arms mean three tests, so the threshold is Bonferroni-corrected to
0.0167. `rag` vs `naru` clears a plain 0.05 but not the corrected threshold, so
the harness correctly calls it not separable.

### Where the difference sits

| Category                   | `full` | `rag` | `naru` | naru vs rag |
| -------------------------- | ------ | ----- | ------ | ----------- |
| knowledge-update           | 13/16  | 13/16 | 13/16  | p=1.000     |
| multi-session              | 10/16  | 7/16  | 8/16   | p=1.000     |
| single-session-assistant   | 13/15  | 15/16 | 15/16  | p=1.000     |
| single-session-user        | 14/14  | 15/16 | 15/16  | p=1.000     |
| single-session-preference  | 4/16   | 2/16  | 7/16   | p=0.125     |
| **temporal-reasoning**     | 10/14  | 7/16  | 14/16  | **p=0.039** |

Four categories are a tie. The whole difference is in temporal reasoning and
preference following. In those two, the model must compute an answer over the
log instead of quoting one turn. A top-8 keyword window can retrieve a turn,
but it cannot do arithmetic across sessions.

The paper predicts this shape, which makes this a reproduction check and not a
result. The paper's own ablation says the eviction index
affects preference following most (89.1 vs 74.9), so that category was
predicted before it was measured. Temporal reasoning was not. These are
post-hoc tests on six categories, so treat the p-values as descriptive.

### Read the numbers honestly

- **Do not quote `billed_input` or `net-of-harness` ratios.** The `claude`
  CLI's usage data is not consistent across runs. The same `full` prompt
  reported 201,288 billed input tokens at n=12 and 47,151 at n=96, for the same
  view size and nearly the same cost. Cost comes from the CLI's own
  `total_cost_usd` and does reconcile. `View` comes from the harness. Those two
  are safe.
- **A question whose run errored leaves the pairing.** It does not score as a
  wrong answer. McNemar reads only the discordant pairs, so one CLI timeout
  scored as a loss can turn p=0.125 into p=0.031 and invent a significance
  claim. `report()` prints accuracy twice when that happens.
- **Run `noise.py` over two replicate runs** before you read anything into a
  gap. At n=12 the run-to-run band is about 33 points. Two identical Sonnet
  runs disagreed on a third of the questions and still landed on the same
  total. The n=24 Haiku run in `results/published/` shows `rag` ahead of
  `naru`, and the n=96 Sonnet run reverses it. Single-model, small-n orderings
  from this harness do not survive.

## Other models

The benchmark uses one provider-neutral contract for the answerer and the
judge: prompt on stdin, text out. By default, `--backend auto` selects an
installed Codex CLI first, then the Claude CLI. The run config and every row
record the resolved provider, so the benchmark cannot mix runtimes in silence.

```bash
python3 bench.py --backend auto \
  --model gpt-5.6-luna --judge-model gpt-5.6-luna \
  --split oracle -n 12
```

Use an explicit provider when you reproduce a run:

```bash
python3 bench.py --backend codex --model gpt-5.6-luna --judge-model gpt-5.6-luna
python3 bench.py --backend claude --model claude-haiku-4-5-20251001
python3 bench.py --backend command --backend-command 'ollama run llama3.2'
```

The Codex backend parses its JSON event stream, keeps only the final agent
message, and records provider token counts. Each call starts in an empty
temporary workspace with user and project rules ignored, so the coding-agent
CLI cannot read the benchmark checkout as part of an answer. Codex CLI usage
does not give a dollar total, so cost per successful task stays unknown unless
you supply a verified pricing layer.

`NARU_BACKEND` accepts any command that reads a prompt on stdin. Generic
commands do not claim token or dollar measurements unless their adapter reports
them.

## Paired curation check

The benchmark tests the log. `curation_probe.py` tests the curation layer: does
the approved doc help real tasks, and does it leak into tasks where it should
not matter?

It runs each ordinary task twice with the same model and settings. The `plain`
arm gets no Naru doc. The `naru` arm gets the current approved doc through the
same context wrapper as the Codex hook. The Claude adapter uses safe mode with
all tools disabled, so the plain arm cannot read Naru facts from project files.
A custom provider must give the same isolation.

Write JSONL cases with literal checks:

```json
{"id":"port","kind":"fact","prompt":"Which port do I use?","must_include":["port 7443"],"must_exclude":["port 443"]}
{"id":"color","kind":"control","prompt":"Reply with blue.","must_include":["blue"]}
```

Use `fact` when approved context should help. Use `control` when it should not
matter. Keep prompts as ordinary work requests. The model never sees arm names,
scores, or comparison text. [`cases.jsonl`](../cases.jsonl) is an example set.
Its fact cases assume your own store holds matching approved claims.

```bash
python3 curation_probe.py cases.jsonl --out results/curation-live.json
```

The report shows paired wins, losses, exact McNemar p-values, harmful carryover
on controls, prompt size, cost, and elapsed time. Failed model calls stay out of
the paired score and remain visible as errors. The result file contains full
answers. It stays ignored under `results/` unless you publish a redacted copy.
