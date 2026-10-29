#!/usr/bin/env python3
"""PostToolUse hook: spill oversized tool output into the Naru Event Log.

Claude Code re-sends the whole conversation every turn, so one large tool
result is paid for on every later turn and occupies the context window for the
rest of the session. This hook intercepts the result at the harness boundary,
stores it verbatim in a searchable Event Log, and replaces it in context with a
bounded preview plus a retrieval handle.

Nothing is lost: the full text stays addressable by `seq`.

Enable per-project in .claude/settings.json:

    {"hooks": {"PostToolUse": [{"matcher": "Bash|Read",
      "hooks": [{"type": "command",
                 "command": "python3 /path/to/naru/hook_spill.py"}]}]}}

The replacement MUST match the tool's own output schema or Claude Code discards
it and keeps the original, so we mutate the text field in place inside the
response object rather than returning a bare string.
"""

import json
import os
import pathlib
import shlex
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import metrics

# One owner for the number. `naru stats` imports this value, so the reported
# spill distribution and the hook use the same context-first threshold.
THRESHOLD = int(os.environ.get("NARU_SPILL_THRESHOLD", "2400"))  # chars
KEEP = int(os.environ.get("NARU_SPILL_PREVIEW", "600"))
SIGNPOST_EVERY = 40  # one signpost line per N lines of spilled text
# Resolved lazily. Small results never open a store, and `import ms` costs
# about 8 ms on every hook call. `ms` stays the one owner of the default path
# (see AGENTS.md).
DB = (
    pathlib.Path(os.environ["NARU_SPILL_DB"])
    if os.environ.get("NARU_SPILL_DB")
    else None
)


def db():
    global DB
    if DB is None:
        from ms import DEFAULT_DB

        DB = pathlib.Path(DEFAULT_DB)
    return DB


# Text-bearing fields, in the order tools tend to use them.
TEXT_FIELDS = ("stdout", "content", "output", "text", "result", "stderr")


def find_text(resp):
    """Return (container, key, text) for the largest text field, or None."""
    if isinstance(resp, str):
        return (None, None, resp)
    if not isinstance(resp, dict):
        return None
    best = None
    for k in TEXT_FIELDS:
        v = resp.get(k)
        if isinstance(v, str) and (best is None or len(v) > len(best[2])):
            best = (resp, k, v)
    return best


def outline(text):
    """Line-numbered signposts so the model can see what it can no longer read
    and jump to the part it needs."""
    lines = text.splitlines()
    marks = [
        f"  {i + 1:>6}  {lines[i].strip()[:70]}"
        for i in range(0, len(lines), SIGNPOST_EVERY)
        if lines[i].strip()
    ]
    return "\n".join(marks[:12])


def preview(text, seq, lines, run):
    """Keep both ends of a large result and a recovery pointer."""
    head_size = max(1, KEEP // 2)
    tail_size = max(1, KEEP - head_size)
    omitted = max(0, len(text) - head_size - tail_size)
    recover = f"`naru show {seq} --run {shlex.quote(run)}`"
    return (
        f"{text[:head_size]}\n"
        f"\n[naru: {len(text):,} chars / {lines:,} lines spilled to the Event "
        f"Log as seq {seq}. {omitted:,} middle chars omitted from this preview.]\n"
        f"{text[-tail_size:]}\n"
        f"[signposts]\n{outline(text)}\n"
        f"[recover: {recover} for the full text, or "
        f'`naru search "<terms>"` to find a part of it]'
    )


def spill_run_id(tool, session_id=None):
    prefix = str(session_id)[:16] if session_id else "?"
    return f"{prefix} {tool}"


def record_spill(text, *, harness, tool, **fields):
    metrics.record("hook", harness=harness, tool=tool, chars=len(text), **fields)


def spill_text(
    text,
    *,
    tool="stdin",
    session_id=None,
    harness="cli",
    database=None,
    fail_open=False,
):
    """Return a bounded preview and keep the full text in the Event Log.

    The caller chooses whether a store failure leaves the text inline. Hooks
    stay fail-open so they cannot break a tool call. The explicit CLI path
    fails closed, because its caller can rerun the command after fixing the
    store.
    """
    if not isinstance(text, str):
        raise TypeError("spill text must be a string")
    if len(text) <= THRESHOLD:
        record_spill(text, harness=harness, tool=tool, spilled=False)
        return text, False

    try:
        from ms import MemorySurface

        chosen_db = pathlib.Path(database) if database is not None else db()
        chosen_db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ms = MemorySurface(str(chosen_db))
        run = spill_run_id(tool, session_id)
        seq = ms.append(
            "tool",
            text,
            kind="tool_result",
            session_id=run,
            created_at=datetime.now().isoformat(timespec="seconds"),
            # No payload=: the content column already holds the full text.
            # Passing it would store the same bytes twice and leave the copy in
            # a temp directory macOS may purge, breaking recovery.
        )
        lines = len(text.splitlines())
        replacement = preview(text, seq, lines, run)
        record_spill(
            text,
            harness=harness,
            tool=tool,
            kept=len(replacement),
            spilled=True,
            store=ms.store_id,
            run=run,
            seq=seq,
        )
        return replacement, True
    except Exception as error:
        metrics.record(
            "error",
            harness=harness,
            tool=tool,
            chars=len(text),
            msg=f"{type(error).__name__}: {error}",
        )
        if fail_open:
            print(f"naru spill failed, output left inline: {error}", file=sys.stderr)
            return text, False
        raise


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0  # not our business; pass through untouched

    # NARU_SPILL_DEBUG=<path> records the real event shape. Claude Code's
    # per-tool response schemas are not documented, and a replacement that does
    # not match the tool's schema is discarded silently, so the shape has to be
    # observed rather than assumed.
    dbg = os.environ.get("NARU_SPILL_DEBUG")
    if dbg:
        try:
            with open(dbg, "a") as fh:
                fh.write(raw[:20000] + "\n---\n")
        except OSError:
            pass

    resp = event.get("tool_response")
    found = find_text(resp)
    if not found:
        return 0
    container, key, text = found
    replacement, spilled = spill_text(
        text,
        tool=event.get("tool_name"),
        session_id=event.get("session_id"),
        harness="claude-code",
        database=DB,
        fail_open=True,
    )
    if not spilled:
        return 0

    if container is None:
        updated = replacement
    else:
        updated = dict(container)
        updated[key] = replacement

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": updated,
            }
        },
        sys.stdout,
    )
    return 0


def _demo_direct_api(big, database, metrics_path):
    small, spilled = spill_text("small", tool="demo", database=database)
    assert small == "small" and not spilled
    cli_preview, spilled = spill_text(big, tool="demo", harness="cli", database=database)
    assert spilled and "naru show 2" in cli_preview and "line 399" in cli_preview
    observed = [json.loads(line) for line in pathlib.Path(metrics_path).read_text().splitlines()]
    assert any(e.get("harness") == "cli" and e.get("spilled") for e in observed)


def demo():
    """Self-check: exercises the hook contract without Claude Code."""
    import subprocess
    import tempfile

    global DB
    tmp = tempfile.TemporaryDirectory()
    DB = pathlib.Path(tmp.name) / "s.db"
    me = [sys.executable, str(pathlib.Path(__file__).resolve())]
    # Isolate metrics too: a self-check must not write into the user's
    # real observability store.
    env = {
        **os.environ,
        "NARU_SPILL_DB": str(DB),
        "NARU_METRICS": str(DB.parent / "m.jsonl"),
    }
    metrics.PATH = pathlib.Path(env["NARU_METRICS"])

    def run(payload):
        p = subprocess.run(
            me, input=json.dumps(payload), capture_output=True, text=True, env=env
        )
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else None

    # small output passes through untouched (no stdout at all)
    assert run({"tool_name": "Bash", "tool_response": {"stdout": "hi"}}) is None

    # large output is replaced, and the response SHAPE is preserved
    big = "\n".join(f"line {i} " + "x" * 60 for i in range(400))
    out = run(
        {
            "tool_name": "Bash",
            "session_id": "abc",
            "tool_response": {"stdout": big, "stderr": "", "interrupted": False},
        }
    )
    upd = out["hookSpecificOutput"]["updatedToolOutput"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert set(upd) == {"stdout", "stderr", "interrupted"}, upd.keys()
    assert upd["interrupted"] is False, "non-text fields must survive"
    assert len(upd["stdout"]) < len(big) / 4, "replacement should be much smaller"
    assert "seq 1" in upd["stdout"] and "naru show 1" in upd["stdout"]
    assert "line 0" in upd["stdout"], "preview should show the head"
    assert "line 200" in upd["stdout"], "signposts should reach the middle"
    assert "line 399" in upd["stdout"], "preview should show the tail"

    # the full text really is recoverable, verbatim
    from ms import MemorySurface

    ms = MemorySurface(str(db()))
    assert ms.expand(1)[0].content == big, "spilled text not recovered verbatim"
    assert ms.search("line", k=3), "spilled text not searchable"
    observed = [
        json.loads(line)
        for line in pathlib.Path(env["NARU_METRICS"]).read_text().splitlines()
    ]
    spill = next(e for e in observed if e.get("spilled"))
    assert spill["harness"] == "claude-code"
    assert spill["store"] == ms.store_id and spill["run"] == "abc Bash"
    assert all(e.get("harness") == "claude-code" for e in observed)

    _demo_direct_api(big, DB, env["NARU_METRICS"])

    # a bare-string response is handled too
    out2 = run({"tool_name": "X", "tool_response": big})
    assert isinstance(out2["hookSpecificOutput"]["updatedToolOutput"], str)

    # malformed stdin must never break the tool call
    p = subprocess.run(me, input="not json", capture_output=True, text=True, env=env)
    assert p.returncode == 0 and not p.stdout.strip()

    saved = len(big) - len(upd["stdout"])
    print(
        f"ok — hook checks passed ({len(big):,} chars -> "
        f"{len(upd['stdout']):,}, {saved / len(big) * 100:.0f}% removed from "
        f"context, full text recoverable at seq 1)"
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        demo()
    else:
        sys.exit(main())
