# Architecture and research

## How it works

Naru has two layers.

**The log.** Naru implements [Context as an Environment: Programmatic Context
Management for Long-Horizon Agents](https://arxiv.org/abs/2608.21690). History
lives in a SQLite log addressed by sequence number. In the agent loop, the
model reaches that history by writing Python, and only what the code prints
enters the next model call. `ms.py` is the log, `kernel.py` is the persistent
Python namespace, and `eviction.py` is the paper's Algorithm 1.

**The curation layer.** This part is not in the paper. Agents propose, you
decide, and only approved items reach the doc. The doc stays small while the
log grows. Four columns carry it:

| Column      | Meaning                                                                       |
| ----------- | ----------------------------------------------------------------------------- |
| `promoted`  | Your decision. A rejected row stays readable at its `seq`.                    |
| `topic_key` | Groups claims about one thing. Two approved claims on one key are a conflict. |
| `base_seq`  | The doc version when the claim was written. `inbox` shows how far it moved.   |
| `scope`     | The repository a fact holds in. Empty for a general fact.                     |

The `naru` command and its hooks use the curation layer and the log. The agent
loop and the benchmark use the log and the kernel.

Read [How it works](../how-it-works.html) for a visual walkthrough.

## Reference results

The benchmark checks that this code behaves like the paper's system. It does
not claim a new result. This is the reference run: LongMemEval-S, 96
questions, Sonnet 5, judged by Haiku 4.5. The rows are in
[`results/published/`](../results/published/), stripped of gold answers.

| Arm    | Correct | 95% CI | Largest context | Calls | Model cost per question |
| ------ | ------- | ------ | --------------- | ----- | ----------------------- |
| `naru` | 72/96   | 65-83% | 2,174 tokens    | 3.7   | $0.3547                 |
| `full` | 64/96   | 57-75% | 124,073 tokens  | 1.0   | $0.8036                 |
| `rag`  | 59/96   | 51-71% | 2,142 tokens    | 1.0   | $0.0753                 |

`full` puts the whole history in the prompt. `rag` pastes the top 8 keyword
hits. `naru` keeps the history in the log and lets the model query it with
code. The cost column excludes the judge, which costs about $0.027 per question
for every arm.

Read this table with care:

- No arm beats another at the corrected significance threshold. A smaller
  Haiku run puts `rag` ahead of `naru`.
- `naru` holds about 57 times less context than `full`, but `rag` is about 4.7
  times cheaper than `naru`.
- The judge is not LongMemEval's official prompt, so you cannot compare these
  numbers with published scores.

[Measure Naru](benchmark.md) has the full method, per-category results,
and how to run the benchmark with other models. [Context policy and
Jev](context-policy.md) covers the optional context gate in the agent
loop.

## Limitations

- **Conflict checks use exact keys.** Two contradictory claims under different
  `--key` values both appear as approved.
- **One database on one machine.** Two databases would break `seq` as a total
  order.
- **Search is BM25 keyword search**, as in the paper. The optional `rsm`
  benchmark arm uses embeddings but does not change runtime search.
- **The agent loop runs model-written code in your process** by default. This
  applies to `agent.py` and the benchmark. The `naru` command and its hooks do
  not run model-written code. `NARU_KERNEL=sandbox` moves the kernel to a child
  process with CPU, memory, and file-size limits and a timeout. That is process
  isolation, not a sandbox: the child has your user, filesystem, and network.
  Set `NARU_KERNEL_JAIL` to wrap it in `sandbox-exec`, `bwrap`, or a container.
  macOS refuses `RLIMIT_AS`, so memory is not capped there, and `limits()` says
  so.
- **Curation has no published production result yet.** It has self-checks and
  a [paired live-check harness](benchmark.md#paired-curation-check).

