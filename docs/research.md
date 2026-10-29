# Architecture and research

[README](../README.md) · [Benchmark guide](benchmark.md) · [Published results](../results/published/README.md)

Use the [setup](setup.md) and [daily-use](usage.md) guides to share memory with
your agents. This page explains the underlying design, the research loop, and
what the measurements support.

## How it works

Naru combines a local log with a document you curate.

| Layer | What it holds | How you use it |
| --- | --- | --- |
| Log | Notes, proposals, tool output, and research history, addressed by sequence number. | Search or read only the entries you need. |
| Approved document | Claims and procedures you promote. | Load them into agent sessions through hooks or instruction files. |

The log implements
[Context as an Environment: Programmatic Context Management for Long-Horizon Agents](https://arxiv.org/abs/2608.21690).
In the research loop, the model writes Python to inspect history. Only the
text that code prints enters the next model call. `ms.py` owns the SQLite log,
`kernel.py` holds the persistent Python namespace, and `eviction.py` implements
the paper's Algorithm 1 for managing the context budget.

The curation layer adds human review. Four fields track each proposal:

| Field | Purpose |
| --- | --- |
| `promoted` | Records whether the item is pending, approved, archived, or rejected. |
| `topic_key` | Groups items about one topic. Multiple approved items of the same kind and scope create an unresolved group. |
| `base_seq` | Records the document version used when the item was proposed. |
| `scope` | Identifies a repository, or leaves the fact general. |

The CLI and memory hooks use the log and curation layer. The research agent
and benchmark use the log and Python kernel. The
[visual walkthrough](../how-it-works.html) shows the flow.

## Reference results

The reference run uses LongMemEval-S with **96 questions**, Sonnet 5 as the
answerer, and Haiku 4.5 as the judge. These are historical model labels from
the [published run](../results/published/v17_sonnet_n96.json).

| Arm | Correct | 95% confidence interval | Mean peak view | Mean calls | Model cost per question |
| --- | --- | --- | --- | --- | --- |
| `naru` | 72/96 | 65–83% | 2,174 tokens | 3.7 | $0.3547 |
| `full` | 64/96 | 57–75% | 124,073 tokens | 1.0 | $0.8036 |
| `rag` | 59/96 | 51–71% | 2,142 tokens | 1.0 | $0.0753 |

`full` puts the whole history in the prompt. `rag` supplies the top eight
keyword matches. `naru` lets the model query the log with code.

**Mean peak view** averages each question's largest recorded view. It is not
the run's maximum or a provider-billed token count. Model cost excludes the
judge, which adds about $0.027–$0.029 per question in this run.

Keep these limits with the numbers:

- No pair of arms reaches the significance threshold after correction for
  three comparisons. A smaller Haiku run ranks `rag` above `naru`.
- The mean peak view for `naru` is about 57 times smaller than `full`.
  The model cost for `rag` is about 4.7 times lower than `naru`.
- The judge differs from LongMemEval's official prompt. These scores are not
  leaderboard-comparable.
- This benchmark measures the research loop. It does not establish whether
  approved facts improve everyday coding tasks.

The [benchmark guide](benchmark.md) explains the controls, errors, paired
statistics, and category results. The
[published-results guide](../results/published/README.md) lets you recompute the
table without an API call. The curation layer has a
[separate paired check](benchmark.md#paired-curation-check), with no published
production result yet.

## Limitations

- Conflict detection uses exact keys within a kind and scope. Contradictory
  facts under different keys can both remain approved.
- Naru uses one local database. It does not merge independent stores or sync
  them between machines; sequence numbers belong to their store.
- Runtime search uses BM25 keyword matching. The optional `rsm` benchmark arm
  uses embeddings but does not change CLI search.
- You control document size by choosing what to promote. The 400-byte item
  limit does not impose a total document budget.
- Human review is a workflow rule. An agent with CLI access can pass `--yes`
  to a decision command.

### Execution limits

The research loop in `agent.py` and the benchmark executes model-written
Python in your process by default. It has your process's access to files and
the network. Normal memory commands and memory hooks do not run model-written
code. Stale checks run the command attached to the claim.

`NARU_KERNEL=sandbox` moves the research kernel into a child process with
timeouts and requested CPU, memory, and file-size limits. Despite the setting's
name, this alone does not provide a security sandbox. The child still runs as
your user with your filesystem and network access.

Set `NARU_KERNEL_JAIL` to wrap the child with an external tool such as
`sandbox-exec`, `bwrap`, or a container. Choose and configure that isolation
for your environment. macOS refuses `RLIMIT_AS`, so that path cannot enforce
the memory cap there; `limits()` reports the refusal.

For the optional policy that selects context within a budget, see
[Context policy and Jev](context-policy.md).
