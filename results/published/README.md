# Published results

[README](../../README.md) · [Reference table](../../docs/research.md#reference-results) · [Benchmark method](../../docs/benchmark.md)

These files contain the per-question rows used to check the reported accuracy,
confidence intervals, context measurements, and costs. You can recompute the
results offline from the repository root.

| File | Questions per arm | Answerer | Arms |
| --- | --- | --- | --- |
| [`v17_sonnet_n96.json`](v17_sonnet_n96.json) | 96 | Sonnet 5 | `full`, `rag`, `naru` |
| [`v12_s_n24.json`](v12_s_n24.json) | 24 | Haiku 4.5 | `full`, `scroll` |
| [`v15_rag_s_n24.json`](v15_rag_s_n24.json) | 24 | Haiku 4.5 | `rag` |
| [`v13_sonnet_s_n12.json`](v13_sonnet_s_n12.json) | 12 | Sonnet 5 | `full`, `scroll` |
| [`v16_rag_sonnet_s_n12.json`](v16_rag_sonnet_s_n12.json) | 12 | Sonnet 5 | `rag` |

Model names describe the recorded runs. Each file's `config` stores the exact
model identifiers and run settings.

## Recompute the reference run

This prints the 96-question run behind the
[reference table](../../docs/research.md#reference-results):

```bash
python3 - <<'PY'
import json
from pathlib import Path
from bench import report, separability

data = json.loads(Path("results/published/v17_sonnet_n96.json").read_text())
rows = data["rows"]
for arm in ("full", "rag", "naru"):
    report(
        [row for row in rows if row["arm"] == arm],
        arm,
        data["config"]["harness_floor"],
    )
separability(rows, ["full", "rag", "naru"])
PY
```

The output includes 64/96 correct for `full`, 59/96 for `rag`, and 72/96 for
`naru`. It excludes five failed `full` runs from paired comparisons. No pair
reaches the corrected significance threshold.

The table's view values are means of the per-question peaks. Model cost
excludes judge cost. Read the benchmark's
[measurement caveats](../../docs/benchmark.md#cost-and-run-to-run-variation)
before using the printed billed-input or net-of-harness fields.

## Recompute the smaller Haiku comparison

This combines the historical `full` and `scroll` run with its `rag` control:

```bash
python3 - <<'PY'
import json
from pathlib import Path
from bench import report, separability

rows = []
for filename in ("v12_s_n24.json", "v15_rag_s_n24.json"):
    data = json.loads((Path("results/published") / filename).read_text())
    batch = data["rows"]
    for row in batch:
        if row["arm"] == "scroll":
            row["arm"] = "naru"
    for arm in sorted({row["arm"] for row in batch}):
        report(
            [row for row in batch if row["arm"] == arm],
            arm,
            data["config"]["harness_floor"],
        )
    rows.extend(batch)
separability(rows, ["full", "rag", "naru"])
PY
```

The example reads each run's measured harness floor from its configuration.
Those two runs recorded 22,846 and 18,718 input tokens per call. Do not replace
them with a shared constant when recomputing historical fields.

## What the files preserve

`scroll` is the older name of the `naru` arm. The data keeps that original
label; the reporting example maps it in memory without changing the files.

The published rows omit `gold` and `answer`. They preserve the fields needed
to check the arithmetic without redistributing the dataset's answer key or
model responses. To run new questions, download the dataset through the
[benchmark setup](../../docs/benchmark.md#run-a-small-benchmark).

The judge differs from LongMemEval's official prompt. Keep that limitation
with any score you quote from these files.
