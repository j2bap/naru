# Context policy and Jev

This page covers the optional context policy in the `naru` agent loop. You do
not need it to use the curation commands in the [README](../README.md). See
[ADR 0014](adr/0014-jev-context-gate.md) for the design decision.

## What the context policy does

Naru stays the memory layer. The agent loop keeps its Event Log, kernel, and
recovery index. A separate context policy limits the mutable state and the
latest tool observation that the model sees. Deterministic selection is the
default.

Jev is a low-overhead typed evaluator. At a pressure checkpoint it receives the
question, the next action, and metadata for named context blocks, but not their
bodies. It can recommend `needed`, `useful`, `omit`, or `no_match`. Naru still
enforces the token budget and keeps required state, Jev-needed blocks, and
recovery handles. If Jev output is missing or malformed, Naru falls back to
deterministic selection.

The request estimate is capped at 800 tokens. If the metadata or question map
is larger, Naru skips Jev and uses the deterministic fitter. This keeps the
main agent's required context instead of overflowing Jev's smaller window.

## Modes

The benchmark exposes two Jev modes:

- `shadow` calls Jev and records what it would have selected.
- `jev` applies only high-confidence retention and omission decisions.

```bash
NARU_JEV='jev mcp' python3 bench.py \
  --arms rag,naru --context-policy shadow --split oracle -n 12
```

For a parent process with the native MCP binding:

```bash
python3 bench.py --backend codex \
  --model gpt-5.6-luna --judge-model gpt-5.6-luna \
  --arms naru --context-policy jev --jev-host-stdio --split oracle -n 12
```

## Connect Jev

Pick the path that matches your host:

- **Direct call.** When the host can make the MCP call itself, pass a
  `DirectJev` evaluator to `run_naru`. This avoids a transport process and does
  not need a Jev key in Naru.
- **Keyword-shaped host binding.** Use `host_mcp_evaluator(host_evaluate)`,
  where the MCP-aware parent process supplies `host_evaluate`.
- **Host MCP over stdio.** For a host that already exposes
  `mcp__jev__evaluate`, use `--jev-host-stdio`. The benchmark runs model jobs
  in a parent-owned thread pool, emits `NARU_JEV_REQUEST` records, and waits
  for one JSON MCP result per record. Only the shared host Jev exchange is
  serialized. The host owns the MCP key and returns the result over stdin. If
  the host does not respond, the call times out and Naru falls back to
  deterministic selection.
- **Standalone command.** `CommandJev` is persistent by default. It reads one
  JSON request per line and returns one typed JSON response per line, so you
  pay startup once per task. `CommandJev(..., persistent=False)` gives a
  one-shot adapter. `CommandJev` detects `jev mcp` and speaks MCP JSON-RPC over
  stdio. It also accepts a custom JSON-lines adapter for test doubles and other
  hosts. The local Jev command needs provider authentication in its own
  environment.

## Metrics and cost

Set `NARU_AGENT_METRICS=1` with a direct `run_naru` caller to append redacted
context decisions to `NARU_METRICS`. Naru records no prompt, state body,
credential, or tool-result payload.

Benchmark rows also keep provider-native usage beside normalized input, cache,
output, retry, latency, task-success, and cost fields. The report shows Jev
wall latency, provider latency, transport overhead, and one-time adapter
startup apart from the main model. Jev decisions do not go through a
text-generating LLM. The report keeps Jev cache reads and writes when the
adapter reports them.

The built-in `typesafe-jev-input-0.042-free-output` profile records a Jev price
of $0.042 per million input tokens and free output. Cache prices are unknown.
The comparator defers if nonzero cache usage appears without rates.

For general execution observability, keep those events outside the recallable
Event Log. You can feed the dependency-free JSONL to the
[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) and view it
in a self-hosted [Phoenix](https://github.com/Arize-ai/phoenix),
[Langfuse](https://langfuse.com/docs), or
[OpenLIT](https://github.com/openlit/openlit) deployment. Naru does not add a
vendor SDK.

## Paired context proof

Run the same split, question count, model, judge, budget, and turn limit once
with each policy. The comparator pairs rows by question ID, excludes errored
pairs from McNemar's test, and applies explicit task-success, failure,
latency, and cost guardrails. It never prints answers, gold text, prompts, or
raw task IDs.

```bash
python3 bench.py --tag naru-deterministic --arms naru \
  --context-policy deterministic --split oracle -n 12
NARU_JEV='jev mcp' python3 bench.py --tag naru-jev --arms naru \
  --context-policy jev --split oracle -n 12
python3 experiment.py \
  --baseline results/naru-deterministic_oracle_n12.json \
  --candidate results/naru-jev_oracle_n12.json \
  --jev-pricing-profile typesafe-jev-input-0.042-free-output
```

The default decision is `defer` when provider costs or Jev pricing are not
measured. The built-in profile covers Jev's input and output rates. Supply
cache rates too when Jev reports nonzero cache categories. Rates carry a
version and are never hidden assumptions. Use `experiment.py --json` for a
machine-readable, privacy-safe report.
