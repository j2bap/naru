# Benchmark guide

[README](../README.md) · [Architecture and results](research.md) · [Published rows](../results/published/README.md)

Use this guide to run the research benchmark, inspect its controls, or test
whether approved memory helps your tasks. For ordinary memory setup, start
with the [user guides](../README.md#guides).

The benchmark makes live model calls and can cost money. Its `naru` arm also
runs model-written Python. Read the
[execution limits](research.md#execution-limits) before you run it. You can
[recompute the published results](../results/published/README.md) offline.

## What you measure

`bench.py` runs [LongMemEval](https://arxiv.org/abs/2410.10813) questions with the
same source history across these arms:

| Arm | What the model receives |
| --- | --- |
| `full` | The whole history in one prompt. |
| `rag` | The top eight BM25 keyword matches, answered in one call. |
| `naru` | A bounded working view, with Python access to history in the log. |
| `rsm` | Grouped chunks selected through embeddings, answered in one call. |

The default is `full,rag,naru`. Keep `rag` in comparisons of retrieval methods:
it controls for the benefit of retrieving a small part of the history. The
optional `rsm` arm needs an embedding adapter.

This benchmark checks the implementation and catches regressions. It uses a
different judge prompt from official LongMemEval, so its scores are not
leaderboard-comparable. The implemented paper reports 94.8 on LongMemEval-S,
73.1 on BEAM-10M, and 86.7 on LOCA-256K against ten named baselines. The runs
here do not establish an improvement on those results.

## Run a small benchmark

From the repository root, download the oracle split:

```bash
mkdir -p data
curl --fail -L \
  -o data/longmemeval_oracle.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
```

Use a configured, authenticated model backend. For Codex:

```bash
python3 bench.py --backend codex --split oracle -n 12 --tag first-run
```

This writes `results/first-run_oracle_n12.json`. The oracle split and 12
questions provide a small check; they do not reproduce the 96-question
LongMemEval-S reference run.

For a reproducible comparison, set the backend, answerer, and judge explicitly.
Replace `MODEL_ID` and `JUDGE_ID` with IDs supported by that backend:

```bash
python3 bench.py --backend codex \
  --model MODEL_ID --judge-model JUDGE_ID \
  --split oracle -n 12 --tag explicit-run
```

Use `--backend claude` for the Claude CLI. With no override,
`--backend auto` selects an installed Codex CLI first, then Claude. The run
configuration and rows record the resolved provider.

### Use a command backend

The provider contract is a prompt on standard input and answer text on
standard output. Replace `your-provider-command` with your command:

```bash
python3 bench.py --backend command \
  --backend-command 'your-provider-command' \
  --split oracle -n 12
```

`NARU_BACKEND` also accepts a provider command. Generic commands do not supply
token or dollar measurements unless their adapter reports them.

The Codex adapter parses its JSON event stream and keeps the final agent
message. Each call starts in an empty temporary workspace with user and project
rules ignored. This keeps benchmark answers from reading the checkout as task
context. The adapter records provider token counts. Codex CLI usage does not
provide a dollar total, so dollar-based comparisons need a verified pricing
layer.

### Add the embedding arm

Set `NARU_EMBED` to a command that accepts one JSON object with a `texts` array
and returns one JSON object with a same-length `vectors` array. Naru validates
and normalizes the vectors. It records the program name, without arguments.

If your adapter is `./embed-bge-large`, run:

```bash
NARU_EMBED='./embed-bge-large' python3 bench.py \
  --arms rag,rsm --split oracle -n 12
```

The defaults are six retrieved atoms, five turns per chunk, a 0.85 merge
threshold, and a 4,000-token source budget. The threshold comes from the
paper's BGE setting; calibrate it for another embedding space.

The report includes embedding input tokens, atom and member counts, timing,
source tokens, and estimated prompt tokens. Model cost excludes the embedding
provider's cost.

## Read the reference run

The [reference table](research.md#reference-results) is the single summary of
the 96-question Sonnet run. The
[published-results guide](../results/published/README.md) reproduces its
accuracy, confidence intervals, mean peak views, mean calls, and costs.

Current benchmark rows estimate total and peak prompt tokens, including system
text and retries. The `naru` arm also records its dynamic-view peak. Historical
rows can have fewer fields; do not treat an absent measurement as zero.

### Paired comparisons

The reference run reports:

```text
paired McNemar, Bonferroni correction for 3 pairs
excluded failed runs: full 5

full vs rag    full only 14, rag only  5    p=0.064    not separable
full vs naru   full only 11, naru only 14   p=0.690    not separable
rag  vs naru   rag  only  9, naru only 22   p=0.029    not separable
```

McNemar's test uses questions where the two arms disagree. Three planned
comparisons give a corrected threshold of **0.0167**. None reaches it.
The `rag` versus `naru` result crosses 0.05 but does not cross this corrected
threshold.

Failed model or judge calls stay out of paired tests. They are still visible
as failures. The raw accuracy table includes all 96 questions; `report()` also
prints accuracy excluding failed runs when needed. Counting a timeout as a
paired loss can change a p-value from 0.125 to 0.031 without evidence of better
answers.

### Category results

These counts exclude failed runs. The p-values compare `naru` with `rag`:

| Category | `full` | `rag` | `naru` | Paired p-value |
| --- | --- | --- | --- | --- |
| knowledge-update | 13/16 | 13/16 | 13/16 | 1.000 |
| multi-session | 10/16 | 7/16 | 8/16 | 1.000 |
| single-session-assistant | 13/15 | 15/16 | 15/16 | 1.000 |
| single-session-user | 14/14 | 15/16 | 15/16 | 1.000 |
| single-session-preference | 4/16 | 2/16 | 7/16 | 0.125 |
| temporal-reasoning | 10/14 | 7/16 | 14/16 | 0.039 |

The largest observed gaps sit in temporal reasoning and preference following.
Those tasks can require combining evidence across turns. That is a useful
hypothesis for further tests; this run does not isolate the cause.

The paper's eviction-index ablation reports 89.1 versus 74.9 on preference
following, so that category has a prior prediction. Temporal reasoning does
not. These six category tests are post-hoc. Treat their p-values as descriptive.

### Cost and run-to-run variation

Use the recorded model cost and harness view measurements for these historical
runs. Do not quote `billed_input` or `net-of-harness` ratios. The Claude CLI
reported 201,288 billed input tokens for a `full` prompt at n=12 and 47,151 at
n=96, despite similar view size and cost. Its `total_cost_usd` values do
reconcile across those runs.

Run the same configuration at least twice, with distinct tags, then compare
the result files:

```bash
python3 noise.py results/repeat-a_oracle_n12.json results/repeat-b_oracle_n12.json
```

Replace those paths with the files from your repeated runs. In the recorded
n=12 checks, the run-to-run band is about 33 percentage points. Two identical
Sonnet configurations disagree on a third of the questions while reaching
the same total score. The published n=24 Haiku comparison ranks `rag` above
`naru`; the n=96 Sonnet comparison reverses that order. Small runs and one
model are insufficient to establish a stable ranking.

## Paired curation check

Use `curation_probe.py` to test the approved document on your own tasks. It
runs each task twice with the same model and settings:

| Arm | Context |
| --- | --- |
| `plain` | No Naru document. |
| `naru` | Your approved document through the same wrapper as the Codex hook. |

The Claude adapter disables all tools, so the plain arm cannot read approved
facts from project files. A custom provider must provide the same isolation.
The model sees ordinary task prompts, without arm names, scores, or comparison
instructions.

Write JSONL cases with literal checks. These examples assume your approved
memory contains the relevant port fact:

```json
{"id":"port","kind":"fact","prompt":"Which port do I use?","must_include":["port 7443"],"must_exclude":["port 443"]}
{"id":"color","kind":"control","prompt":"Reply with blue.","must_include":["blue"]}
```

Use `fact` when approved context should help and `control` when it should not
affect the answer. The bundled [`cases.jsonl`](../cases.jsonl) has examples;
its fact cases require matching claims in your store.

```bash
python3 curation_probe.py cases.jsonl --out results/curation-live.json
```

The report shows paired wins and losses, exact McNemar p-values, harmful
carryover on controls, prompt size, cost, and elapsed time. Failed calls remain
visible as errors and stay out of paired scores. The result file contains full
answers and stays ignored under `results/` unless you publish a redacted copy.

For a separate comparison of context-selection policies in the research loop,
see [Context policy and Jev](context-policy.md#compare-policies).
