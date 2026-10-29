# Contributing

[README](README.md) · [Architecture](docs/research.md) · [Benchmark guide](docs/benchmark.md)

Keep each change focused. Explain the behavior you change, update the relevant
guide, and include the commands you ran with their results in your pull
request.

## Run the offline checks

From the repository root, run:

```bash
(
  set -e
  python3 ms.py
  python3 kernel.py
  python3 eviction.py
  python3 agent.py
  python3 naru.py --selfcheck
  python3 hook_spill.py --selfcheck
  python3 noise.py --selfcheck
  python3 metrics.py --selfcheck
  python3 backend.py --selfcheck
  python3 bench.py --selfcheck
  python3 beam.py --selfcheck
  python3 regrade.py --selfcheck
  python3 curation_probe.py --selfcheck
  python3 test_mutations.py
)
```

The block stops at the first failure. These checks run offline. The
[CI workflow](.github/workflows/checks.yml) runs the same set on Python 3.9 and
3.13, on Ubuntu and macOS, and checks that SQLite has FTS5 support.

When you change the context policy, Jev adapter, experiment comparator, or
their use in the agent loop, also run these offline checks. They are separate
from the CI check set above:

```bash
python3 context_policy.py
python3 jev.py
python3 experiment.py --selfcheck
```

`python3 backend.py` and `python3 test_judge.py` without `--selfcheck` make live
model calls and can cost money. They are outside this offline check set.

For a docs change, also check local links and run the commands you changed in
a temporary store. Keep live calls separate from offline validation.

## Keep installation simple

The runtime uses Python's standard library. Preserve the clone-and-symlink
installation path. If a change needs a dependency, explain the requirement and
the alternatives in the pull request before adding it.

Use the shared shell-command interface for agent integrations. Add an
abstraction or setting when current callers need it. Keep test-only behavior
out of production code.

## Test behavior through the real path

Call the function that production code calls. A test that repeats the same
expression can pass even when the application path breaks.

```python
# Check the function used by main().
assert unknown_arms(["full", "rag", "nauru"]) == ["nauru"]
```

`test_mutations.py` deliberately changes production behavior and checks that
the relevant self-check fails. When you add an invariant, add a mutation that
shows the check detects its violation. Avoid assertions that compare a value
with itself or share output buffers between independent scenarios.

## Change a benchmark carefully

Read the [benchmark method](docs/benchmark.md) before changing measurement or
reporting code.

- Measure the harness overhead for each run. Do not hardcode a measured
  constant into a result.
- Keep `rag` as the control when comparing retrieval methods.
- Report repeated-run variation and paired McNemar results. Correct across
  the planned arm pairs. Confidence-interval overlap does not replace a paired
  test.
- Exclude failed model or judge calls from paired accuracy tests and report
  those failures separately.
- Do not quote historical `billed_input` or `net-of-harness` ratios. The
  recorded Claude CLI usage does not reconcile across those runs; model cost
  and harness view measurements do.
- Keep the judge caveat with the scores. This repository's results are not
  comparable with the official LongMemEval leaderboard.

## Document and commit the change

Explain the context, decision, and reason for a design change in the pull
request. Update public docs with the behavior they describe. Keep private
development records out of this repository.

Use Conventional Commits, such as `docs: clarify agent setup`. Keep the
imperative subject within 50 characters, with no final period. Put a blank
line before a body wrapped at 72 characters. Explain what changes and why.
Do not add `Co-Authored-By` lines.

## Report security issues

The research kernel executes model-written Python. Read its
[execution limits](docs/research.md#execution-limits) before running it.
If you report a vulnerability in an issue, describe the class of problem
without publishing a working exploit.

## Prepare a public release

Use the [release checklist](docs/public-release.md) when bringing changes into
the public repository. Review both the content and the files included in the
release.
