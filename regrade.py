#!/usr/bin/env python3
"""Re-grade saved runs with the current judge.

Isolates the judge's contribution to run-to-run flips: the answers are frozen,
so any change in verdicts comes from the judge alone. Writes <tag>_regraded.json
next to the input so noise.py can compare regraded replicates.

    python3 regrade.py results/v10_*.json results/v13_*.json
"""

import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backend import HAIKU, get_backend, resolve_backend
from bench import RESULT_FORMAT, _result_status, backend_provenance, judge, load


def main(paths, model=HAIKU, workers=6):
    if not paths:
        sys.exit("usage: regrade.py results/*.json")

    questions = {}
    for split in ("oracle", "s"):
        try:
            for q in load(split):
                questions[q["question_id"]] = q["question"]
        except SystemExit:
            pass

    documents = []
    for path in paths:
        path = pathlib.Path(path)
        data = json.loads(path.read_text())
        rows = data["rows"]
        missing = sorted({row["qid"] for row in rows if not questions.get(row["qid"])})
        if missing:
            sys.exit(f"missing question text for: {', '.join(missing)}")
        if any("gold" not in row for row in rows):
            sys.exit(f"{path} has rows without gold answers")
        old_format = data.get("config", {}).get("result_format", 1)
        if old_format < RESULT_FORMAT and any(
            len(row.get("answer") or "") == 400 for row in rows
        ):
            sys.exit(f"{path} has legacy 400-character answers; exact regrading is impossible")
        documents.append((path, data))

    # Resolved once, for the same reason bench.py does it: every row gets the
    # same judge, and the output can name the judge that produced the verdicts.
    kind, command = resolve_backend(model=model)

    out_paths = []
    for p, d in documents:
        rows = d["rows"]

        def one(r):
            be = get_backend(model, backend=kind, command=command)
            q = {"question": questions[r["qid"]], "answer": r["gold"]}
            new = judge(q, r.get("answer") or "", be)
            return r, new, be

        changed, cost, cost_measured = 0, 0.0, True
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for r, new, be in ex.map(one, rows):
                usage = be.usage
                if getattr(usage, "cost_measured", True):
                    cost += usage.cost_usd
                else:
                    cost_measured = False
                if bool(r["correct"]) != new:
                    changed += 1
                r["correct"] = new
                r["judge_cost"] = (
                    round(usage.cost_usd, 4)
                    if getattr(usage, "cost_measured", True)
                    else None
                )
                r["judge_call_retries"] = usage.call_retries
                r["judge_empty_retries"] = usage.empty_retries
                # The answer is frozen, so its error count stands. Everything
                # the original judge left on the row is replaced: a row that
                # was a judge failure kept `failure_class` and `judge_error`
                # beside the new verdict, and named a judge that did not
                # produce it.
                status = _result_status(
                    r.get("errors") or 0, usage, None, False, correct=new
                )
                if r.get("failure_class") == "harness":
                    # The count alone cannot tell a harness loss from a model
                    # error, and regrading the judge changes neither.
                    status["failure_class"] = "harness"
                r.update(status)
                r.pop("judge_error", None)
                r["judge_backend"] = getattr(be, "label", model)
                base_cost = r.get("cost", 0)
                r["task_cost_usd"] = (
                    round(base_cost + r["judge_cost"], 4)
                    if isinstance(base_cost, (int, float))
                    and not isinstance(base_cost, bool)
                    and isinstance(r["judge_cost"], (int, float))
                    and not isinstance(r["judge_cost"], bool)
                    else None
                )
                if hasattr(usage, "native_usage"):
                    r["judge_provider_usage"] = dict(usage.native_usage)
                if hasattr(usage, "normalized"):
                    r["judge_usage_normalized"] = usage.normalized()

        d.setdefault("config", {})["judge_model"] = model
        d["config"].update(backend_provenance(kind, command, "judge_"))
        d["config"]["result_format"] = RESULT_FORMAT

        outp = p.with_name(p.stem + "_regraded.json")
        outp.write_text(json.dumps(d, indent=1))
        out_paths.append(str(outp))
        for arm in sorted({r["arm"] for r in rows}):
            a = [r for r in rows if r["arm"] == arm]
            print(
                f"  {p.stem:26} {arm:7} {100 * sum(r['correct'] for r in a) / len(a):5.1f}%"
                f"  ({sum(r['correct'] for r in a)}/{len(a)})"
            )
        print(
            f"  {'':26} {changed} verdict(s) changed vs the original grading "
            f"| {'$%.2f' % cost if cost_measured else 'cost unknown'}\n"
        )

    print("regraded files:")
    for o in out_paths:
        print(f"  {o}")
    return 0


def demo():
    class Usage:
        cost_usd = 0.25
        errors = 0
        call_retries = 0
        empty_retries = 0

    class Backend:
        label = "new-judge"

        def __init__(self):
            self.usage = Usage()

        def __call__(self, prompt, system=None, nudge=None):
            return "CORRECT" if "TAIL FACT" in prompt else "WRONG"

    def questions(_):
        return [
            {"question_id": "q1", "question": "where is the fact?"},
            {"question_id": "q2", "question": "what else?"},
        ]

    import tempfile
    from unittest import mock

    from backend import backend_fingerprint

    # Arguments can carry a credential; only the label and a fingerprint of
    # the regrade judge may reach the output.
    new_judge = ("command", "new-judge --key sk-secret")

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        current = root / "current.json"
        answer = "x" * 450 + " TAIL FACT"
        current.write_text(json.dumps({
            "config": {
                "result_format": RESULT_FORMAT, "judge_backend": "old-judge",
                "judge_resolved_backend": "codex",
                "judge_backend_fingerprint": "old",
            },
            "rows": [{
                "qid": "q1", "arm": "full", "gold": "TAIL FACT",
                "answer": answer, "correct": False, "judge_errors": 9,
                "judge_cost": 9.0, "failure_class": "judge",
                "judge_error": "HARNESS: judge unavailable",
                "judge_backend": "old-judge",
            }, {
                "qid": "q2", "arm": "full", "gold": "another fact",
                "answer": "HARNESS: worker died", "correct": False,
                "errors": 1, "failure_class": "harness",
            }],
        }))
        with mock.patch(f"{__name__}.load", questions), mock.patch(
            f"{__name__}.resolve_backend", lambda model=None: new_judge
        ), mock.patch(
            f"{__name__}.get_backend", lambda *_args, **_kwargs: Backend()
        ):
            assert main([current], workers=1) == 0
        text = (root / "current_regraded.json").read_text()
        result = json.loads(text)
        row, lost = result["rows"]
        assert row["correct"] and row["answer"] == answer
        assert row["judge_errors"] == 0 and row["judge_cost"] == 0.25
        assert row["task_success"]
        assert row["task_cost_usd"] == 0.25
        # Nothing the original judge wrote may sit beside the new verdict.
        assert row["failure_class"] is None and "judge_error" not in row, row
        assert row["judge_backend"] == "new-judge", row["judge_backend"]
        # The answer side is not regraded: a harness loss stays one.
        assert lost["failure_class"] == "harness" and lost["errors"] == 1, lost
        assert not lost["correct"] and not lost["task_success"]
        config = result["config"]
        assert config["result_format"] == RESULT_FORMAT
        assert config["judge_model"] == HAIKU
        assert config["judge_resolved_backend"] == "command", config
        assert config["judge_backend"] == "new-judge", config
        assert config["judge_backend_fingerprint"] == backend_fingerprint(*new_judge)
        assert "sk-secret" not in text

        legacy = root / "legacy.json"
        legacy.write_text(json.dumps({
            "config": {},
            "rows": [{
                "qid": "q1", "arm": "full", "gold": "TAIL FACT",
                "answer": "x" * 400, "correct": False,
            }],
        }))
        calls = []
        with mock.patch(f"{__name__}.load", questions), mock.patch(
            f"{__name__}.get_backend",
            lambda *_args, **_kwargs: calls.append(1) or Backend(),
        ):
            try:
                main([legacy], workers=1)
            except SystemExit as error:
                assert "exact regrading is impossible" in str(error), error
            else:
                raise AssertionError("legacy truncated answers were regraded")
        assert calls == [], "regrade called the judge before validating input"
    print("ok — regrade checks passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        demo()
    else:
        sys.exit(main(sys.argv[1:]))
