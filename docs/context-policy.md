# Context policy and Jev

[README](../README.md) · [Architecture](research.md) · [Benchmark guide](benchmark.md)

This guide covers an optional policy in the research agent loop. You do not
need it for claims, inbox review, or agent memory hooks. Benchmark examples
make live model calls; complete the
[benchmark setup](benchmark.md#run-a-small-benchmark) first.

## What the policy controls

The agent loop keeps its log, Python kernel, and recovery index. The context
policy selects how much mutable state and recent tool output enters the next
model call. Deterministic selection is the default.

Jev is an optional typed evaluator. At a context-pressure checkpoint, it
receives the question, next action, and metadata for named context blocks.
It does not receive the block bodies. It returns recommendations such as
`needed`, `useful`, `omit`, or `no_match`.

Naru enforces the token budget and retains required state, blocks marked needed,
and recovery handles. Missing or malformed Jev output falls back to
deterministic selection. If the request estimate exceeds **800 tokens**, Naru
skips Jev and uses the deterministic policy.

## Choose a mode

| Mode | Behavior |
| --- | --- |
| `deterministic` | Select context locally. No Jev call. This is the default. |
| `shadow` | Call Jev and record its recommendations without applying them. |
| `jev` | Apply high-confidence retention and omission decisions within Naru's limits. |

For a configured local `jev mcp` command:

```bash
NARU_JEV='jev mcp' python3 bench.py \
  --backend codex --arms rag,naru \
  --context-policy shadow --split oracle -n 12
```

## Connect Jev

Choose the adapter that matches your host:

| Adapter | Use it when… |
| --- | --- |
| `DirectJev` passed to `run_naru` | Your host can call the evaluator directly. No transport process or Jev key in Naru is needed. |
| `host_mcp_evaluator(host_evaluate)` | Your parent process provides an MCP call with keyword arguments. |
| `--jev-host-stdio` | An MCP-aware parent can exchange requests and responses with the benchmark over standard input and output. |
| `CommandJev` | You have a local Jev command or a JSON-lines adapter. |

`CommandJev` keeps a process open by default, with one JSON request and response
per line. `CommandJev(..., persistent=False)` starts a process for each call.
It detects `jev mcp` and uses MCP JSON-RPC over standard input and output. A
local Jev command needs its own provider authentication.

### Use an MCP-aware parent

Launch this through a parent that can handle the protocol:

```bash
python3 bench.py --backend codex \
  --arms naru --context-policy jev --jev-host-stdio --split oracle -n 12
```

The benchmark emits `NARU_JEV_REQUEST` records and expects one JSON MCP result
on standard input for each record. The parent owns the MCP key. Model jobs run
in a thread pool; only the shared Jev exchange is serialized. If the parent
does not respond, the call times out and Naru uses deterministic selection.
This mode requires that parent; a terminal alone does not supply the results.

## Inspect metrics and cost

For a direct `run_naru` caller, set `NARU_AGENT_METRICS=1` to append redacted
context decisions to the file named by `NARU_METRICS`. These decision events
exclude prompts, state bodies, credentials, and tool-result payloads.

Benchmark rows keep provider-native usage with normalized input, cache,
output, retries, latency, success, and cost fields. Reports separate Jev wall
latency, provider latency, transport overhead, and adapter startup from the
main model. Jev decisions do not use a text-generating LLM. Cache reads and
writes remain visible when the adapter reports them.

The repository's `typesafe-jev-input-0.042-free-output` pricing profile stores
**$0.042 per million input tokens** and **free output**. It has no cache prices.
Treat this as a named input to the experiment, not a guarantee of current
provider pricing. The comparator defers if it sees nonzero cache usage without
corresponding rates.

Keep general execution metrics outside the searchable memory log. You can
export the JSONL through an
[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) to a
self-hosted [Phoenix](https://github.com/Arize-ai/phoenix),
[Langfuse](https://langfuse.com/docs), or
[OpenLIT](https://github.com/openlit/openlit) deployment. Naru does not bundle
a vendor SDK or those services.

## Compare policies

Compare the `naru` arm with itself. Hold the split, questions, backend, model,
judge, budget, and turn limit fixed. Replace `MODEL_ID` and `JUDGE_ID` in both
commands with supported IDs before running them:

```bash
python3 bench.py --backend codex \
  --model MODEL_ID --judge-model JUDGE_ID \
  --tag naru-deterministic --arms naru \
  --context-policy deterministic --split oracle -n 12

NARU_JEV='jev mcp' python3 bench.py --backend codex \
  --model MODEL_ID --judge-model JUDGE_ID \
  --tag naru-jev --arms naru \
  --context-policy jev --split oracle -n 12

python3 experiment.py \
  --baseline results/naru-deterministic_oracle_n12.json \
  --candidate results/naru-jev_oracle_n12.json \
  --jev-pricing-profile typesafe-jev-input-0.042-free-output
```

The comparator pairs rows by question ID and excludes errored pairs from
McNemar's test. It also checks task success, failures, latency, and cost against
explicit limits. Its report excludes answers, gold text, prompts, and raw
task IDs. Add `--json` for a machine-readable report.

The result is `defer` when required provider costs or Jev prices are unknown.
The named profile supplies Jev input and output rates; supply verified cache
rates if cache usage is nonzero. Rates carry a version. Run
`python3 experiment.py --help` for price and comparison-limit options.
