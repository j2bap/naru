#!/usr/bin/env python3
"""naru — one curated doc, shared across sessions, models and harnesses.

Agents propose claims and skills. You decide. Only what you promote reaches the doc, which is
why the doc stays small while the log grows without bound.

    naru claim "<text>" [--key k] [--by agent]  # an agent proposes a fact
    naru skill "<procedure>" [--key k] [--by agent]
    naru inbox                                  # decide: promote / archive / reject / skip
    naru inject [path]                          # render the doc: stdout or file
    naru promote SEQ --yes | naru drop SEQ --yes    # decide without the prompt

`promote` and `drop` refuse to run without a terminal unless you pass --yes.
That is a speed bump so a tool loop cannot promote by accident, not a security
boundary — anything that can run this CLI can also pass --yes.

To revise a promoted item: `naru drop <old> --yes`, then propose and promote the
new one. Superseding without retiring leaves both promoted and parks the key
under ## Unresolved.

`inject <path>` splices the doc between `<!-- naru:begin -->` and
`<!-- naru:end -->` markers. It never overwrites the rest of the file, so
pointing it at an AGENTS.md you maintain by hand is safe.

    naru add "sprint planning" < notes.md   # or: naru add "topic" "text"
    naru search "context budget"
    naru outline
    naru show 12 18 [--run ID] [--at CHAR]   # prints 20,000 chars per call
    naru check 12 "codex --version" --yes   # inbox flags the fact if this changes
    naru reject 12 --yes                    # wrong: archive search skips it
    naru spill [--tool NAME] [--session ID] < output.txt
    naru prune [days] [--dry-run]   # default 30; deletes older rows + index
    naru gc                          # remove this store's orphaned blobs
    naru stats [days]                # spill/recovery observability

Harness-agnostic by construction: anything that can run a shell command can
both write proposals and read the doc. `inject` targets the context file the
harness already reads. The Codex plugin calls `naru codex-hook` to load the
same approved doc through Codex lifecycle hooks.

    naru inject AGENTS.md        # Claude Code, Codex, DeepSeek Harness
    naru inject .cursorrules     # Cursor

Notes, proposals and hook-spilled tool output share ~/.naru/log.db (override with
NARU_DB). Recall prints matching lines only, never the whole store, so pulling
a fact back into a session costs a few hundred tokens instead of everything you
ever wrote.
"""

import contextlib
import hashlib
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import metrics
from ms import (
    BEGIN,
    DEFAULT_DB,
    END,
    MemorySurface,
    context_delivery,
    remember_context_delivery,
)

DB = pathlib.Path(os.environ.get("NARU_NOTES", DEFAULT_DB))
CODEX_STATE_SCHEMA = "naru.codex.context.v1"
CODEX_EVENTS = {"SessionStart", "UserPromptSubmit", "SubagentStart"}
CODEX_SESSION_LIMIT = 512
# One show call prints at most this much. It sits under Claude Code's
# 30,000-character cut and Codex's 10,000-token cap.
SHOW_CHARS = int(os.environ.get("NARU_SHOW_CHARS", "20000"))
# Checks run inside the interactive inbox, so a hung one would block the
# review. Ten seconds covers `codex --version` and `git log` with room.
CHECK_SECONDS = 10
# Every promoted line costs tokens in every session. No promoted claim was
# over 400 bytes on 2026-10-19; 8% of dropped claims were.
CLAIM_BYTES = 400


def store():
    # The store holds whatever a tool printed, which can include secrets
    # the agent happened to display. 0o700 so it is not world-readable.
    DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return MemorySurface(str(DB))


def _opt(args, name, default=None):
    """Pull `--name value` out of `args` in place. The rest of this CLI parses
    by hand; argparse for three flags would be the bigger change."""
    if name not in args:
        return default
    i = args.index(name)
    if i + 1 == len(args) or not args[i + 1].strip() or args[i + 1].startswith("--"):
        del args[i]
        raise ValueError(f"{name} needs a value")
    val = args[i + 1]
    del args[i : i + 2]
    return val


def _splice(path, text):
    """Write the doc into `path` between markers, never over the whole file.

    `naru inject AGENTS.md` names a file the user wrote and maintains. A plain
    write_text() silently destroys it — the documented happy path was a data
    loss bug. Existing markers are replaced in place; a file without them keeps
    everything it had and gains a marked block at the end.
    """
    block = f"{BEGIN}\n{text.rstrip()}\n{END}\n"
    p = pathlib.Path(path)
    try:
        old = p.read_text()
    except FileNotFoundError:
        old = ""
    if BEGIN in old and END in old:
        head, rest = old.split(BEGIN, 1)
        new = head + block + rest.split(END, 1)[1].lstrip("\n")
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + block
    p.write_text(new)
    return len(block), len(old)


def _j_dumps(events):
    """Serialize metric events exactly as record() writes them."""
    import json

    return "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events)


def _apply(ms, seq, answer):
    """Act on one inbox answer. Returns the line to print.

    Split out of the input() loop so promote / archive / reject / skip can be asserted
    without a terminal, and so the rowcount is not thrown away: another writer
    can decide the same claim while the human is still reading it.
    """
    if answer.startswith("p"):
        return "promoted" if _decide(ms, seq, True) else "already decided elsewhere"
    if answer.startswith(("d", "a")):
        return "archived" if _decide(ms, seq, False) else "already decided elsewhere"
    if answer.startswith("r"):
        return "rejected" if _reject(ms, seq) else "already decided elsewhere"
    return "skipped"


def _reject(ms, seq):
    """Mark a claim wrong so archive search never surfaces it."""
    changed = ms.reject(seq)
    if changed:
        _refresh(ms)  # a rejected fact may have been in the doc
    return changed


def _decide(ms, seq, keep):
    """Record one verdict, then rewrite every file `inject` wrote.

    A file-based harness reads its context file and nothing else, so it never
    sees a promotion until that file changes. Claude Code ran on a doc missing
    15 promotions before this existed.
    """
    changed = ms.decide(seq, keep)
    if changed:
        if keep:
            row = ms.sql_query(
                "SELECT check_cmd FROM conversation_history WHERE seq=?", (seq,))
            if row and row[0].check_cmd:
                ms.set_check(seq, row[0].check_cmd, _run_check(row[0].check_cmd))
        _refresh(ms)
    return changed


def _run_check(command):
    """Run a fact's check command and return its output as the evidence.

    A fact goes stale when what it was checked against changes: a tool
    version, or the files it describes. MemTrace (arXiv 2610.04838) checks
    recalled evidence against the current repository for the same reason.
    """
    try:
        argv = [os.path.expanduser(a) for a in shlex.split(command)]
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=CHECK_SECONDS, cwd=os.path.expanduser("~"))
    except subprocess.TimeoutExpired:
        return f"timed out after {CHECK_SECONDS}s"
    except (OSError, ValueError) as error:
        return f"failed: {error}"
    out = p.stdout.strip() if p.returncode == 0 else f"exit {p.returncode}: {p.stderr.strip()}"
    return out[:1000]


def _stale(ms):
    """Promoted facts whose check output no longer matches, with the new output."""
    return [(r, now) for r in ms.checked()
            if (now := _run_check(r.check_cmd)) != r.check_out]


def _apply_stale(ms, row, now, answer):
    """Act on one stale fact: keep records the new output as confirmed."""
    if answer.startswith("k"):
        ms.set_check(row.seq, row.check_cmd, now)
        return "kept; check updated"
    if answer.startswith("d"):
        return "dropped" if _decide(ms, row.seq, False) else "already decided elsewhere"
    return "skipped"


def _refresh(ms):
    """Splice the current doc into each recorded file that still has markers.

    Removing the markers is how a user stops Naru writing a file, so such a
    file, or one that is gone or unreadable, is forgotten and left alone.
    """
    text = ms.doc()
    for path in ms.inject_targets():
        try:
            old = pathlib.Path(path).read_text()
        except (OSError, UnicodeDecodeError):
            old = ""
        if BEGIN not in old or END not in old:
            ms.forget_target(path)
            continue
        _splice(path, text)


def _show(ms, rows, run, at):
    """Print rows in parts that survive a harness's output limit.

    Claude Code cuts the middle of a long result and Codex caps one at about
    10,000 tokens, so an unbounded print could lose rows while the receipt
    said they were read. Whole rows print up to SHOW_CHARS and a receipt
    covers only those. A single larger row prints one slice per call from
    --at, and a slice writes no receipt: it does not prove the row was read.
    """
    run_flag = f" --run {shlex.quote(run)}" if run else ""
    shown, used = [], 0
    for i, r in enumerate(rows):
        head = f"--- [{r.seq}] {r.created_at} | {r.session_id}"
        size = len(head) + len(r.content) + 2
        if not shown and size > SHOW_CHARS:
            part = r.content[at:at + SHOW_CHARS]
            end = at + len(part)
            print(f"{head} | chars {at:,}-{end:,} of {len(r.content):,}")
            print(part)
            if end < len(r.content):
                print(f"[naru: not done. Run: naru show {r.seq} --at {end}{run_flag}]")
            elif i + 1 < len(rows):
                print(f"[naru: not done. Run: naru show {rows[i + 1].seq} "
                      f"{rows[-1].seq}{run_flag}]")
            return
        if shown and used + size > SHOW_CHARS:
            print(f"[naru: not done. Run: naru show {r.seq} {rows[-1].seq}{run_flag}]")
            break
        print(head)
        print(r.content)
        shown.append(r)
        used += size
    if shown:
        metrics.record_show(ms.store_id, run, shown[0].seq, shown[-1].seq)


def _hook_session(event, harness="codex"):
    session_id = event.get("session_id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > CODEX_SESSION_LIMIT
        or "\0" in session_id
    ):
        return None
    return f"{harness}:{session_id}"


def _hook_seen(ms, session_id, harness="codex"):
    state = context_delivery(ms, harness, session_id)
    if state is not None:
        return (
            state.doc_hash
            if state.schema == CODEX_STATE_SCHEMA and isinstance(state.doc_hash, str)
            else None
        )
    if harness != "codex":
        return None  # only Codex wrote legacy agent_state rows
    rows = ms.sql_query(
        "SELECT content, created_at FROM conversation_history"
        " WHERE kind='agent_state' AND agent_id='codex' AND session_id=?"
        " ORDER BY seq DESC LIMIT 1",
        (session_id,),
    )
    if not rows:
        return None
    try:
        legacy = json.loads(rows[0].content)
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        legacy.get("schema") != CODEX_STATE_SCHEMA
        or type(legacy.get("doc_seq")) is not int
        or not isinstance(legacy.get("doc_hash"), str)
    ):
        return None
    remember_context_delivery(
        ms,
        "codex",
        session_id,
        {
            "schema": CODEX_STATE_SCHEMA,
            "doc_seq": legacy["doc_seq"],
            "doc_hash": legacy["doc_hash"],
            "delivered_at": rows[0].created_at,
        },
    )
    return legacy["doc_hash"]


def _hook_remember(ms, session_id, doc_seq, doc_hash, harness="codex"):
    if _hook_seen(ms, session_id, harness) == doc_hash:
        return
    remember_context_delivery(
        ms,
        harness,
        session_id,
        {
            "schema": CODEX_STATE_SCHEMA,
            "doc_seq": doc_seq,
            "doc_hash": doc_hash,
            "delivered_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


PROMOTE_RULE = (
    "Promote only a fact that helps in many repositories and that an agent "
    "would not guess, such as a non-standard rule or a tool quirk. Never "
    "promote a repository overview."
)


def _hook_context(doc, harness="codex", subagent=False, in_file=False, repo=""):
    # A subagent cannot judge what is already known, so its claims arrive as
    # duplicates. OptMem gives its subagents the same rule.
    filing = (
        "You are a subagent. Do not run `naru claim`. Report a lasting fact "
        "to your parent agent, which decides whether to file it."
        if subagent else
        f"File a lasting fact with `naru claim \"<text>\" --key <topic> --by "
        f"{harness}`; add `--repo` when it holds only in this repository. A "
        "person decides it with `naru inbox`. File only a fact an agent would "
        "not guess, such as a non-standard rule or a tool quirk."
    )
    tools = f"{filing} Use `naru search \"<terms>\"` to recall archived facts."
    tail = f"\n\n{repo}" if repo else ""
    if in_file:
        return f"The approved Naru doc is already in your instructions.\n{tools}{tail}"
    return (
        "Naru live context follows. It replaces any older Naru block from "
        f"your instruction files.\n{tools}\n\n{doc}{tail}"
    )


def _instruction_files(harness, cwd):
    """The files a harness loads at session start that can hold the doc.

    Codex reads $CODEX_HOME/AGENTS.md. Claude Code reads ~/.claude/CLAUDE.md,
    then CLAUDE.md, .claude/CLAUDE.md, and CLAUDE.local.md in the working
    directory and each parent. It reads AGENTS.md there only when none of
    those CLAUDE files exist. Repository files never hold the doc, so a Codex
    project AGENTS.md is not listed.
    """
    if harness == "codex":
        home = pathlib.Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
        return [home / "AGENTS.md"]
    here = pathlib.Path(cwd).resolve()
    dirs = [here, *here.parents]
    claude = [d / name for d in dirs
              for name in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")]
    found = [f for f in claude if f.is_file()]
    agents = [] if found else [d / "AGENTS.md" for d in dirs]
    return [pathlib.Path("~/.claude/CLAUDE.md").expanduser(), *found, *agents]


def _hook_file_has(doc, harness="codex", cwd="."):
    """True when a file the harness loads already holds this exact doc."""
    block = f"{BEGIN}\n{doc.rstrip()}\n{END}"
    for path in _instruction_files(harness, cwd):
        try:
            if block in path.read_text():
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def _repo_id(path):
    """Name the repository at `path` the same way on every workstation.

    The origin URL gives owner/name, which survives a different checkout
    path. A repository without a remote falls back to its folder name.
    """
    def git(*args):
        p = subprocess.run(["git", "-C", str(path), *args],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else ""

    url = git("remote", "get-url", "origin")
    if url:
        tail = url.rstrip("/").removesuffix(".git").replace(":", "/").split("/")
        if len(tail) >= 2 and tail[-2] and tail[-1]:
            return f"{tail[-2]}/{tail[-1]}"
    top = git("rev-parse", "--show-toplevel")
    return pathlib.Path(top).name if top else None


def _hook(event, ms, harness="codex"):
    if not isinstance(event, dict) or event.get("hook_event_name") not in CODEX_EVENTS:
        return None
    session_id = _hook_session(event, harness)
    if session_id is None:
        return None
    event_name = event["hook_event_name"]
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    repo = _repo_id(cwd)
    repo_doc = ms.repo_doc(repo) if repo else ""
    doc = ms.doc()
    doc_seq = ms.doc_version()
    doc_hash = hashlib.sha256((doc + repo_doc).encode()).hexdigest()
    archive = ""
    if event_name == "UserPromptSubmit" and isinstance(event.get("prompt"), str):
        archive = _archive_section(ms.archive(event["prompt"], repo))
    if (event_name == "UserPromptSubmit"
            and _hook_seen(ms, session_id, harness) == doc_hash):
        if not archive:
            return None
        metrics.record("archive_delivery", harness=harness, chars=len(archive))
        return {"hookSpecificOutput": {"hookEventName": event_name,
                                       "additionalContext": archive.lstrip()}}
    if event_name != "SubagentStart":
        _hook_remember(ms, session_id, doc_seq, doc_hash, harness)
    # A new session has just loaded its instruction files, and _decide keeps
    # an injected one current, so sending the doc again would double it.
    # Later prompts still get the doc: the session holds the file as it was
    # at start. Repository facts never live in a file, so they always go.
    in_file = event_name == "SessionStart" and _hook_file_has(doc, harness, cwd)
    context = _hook_context(
        doc, harness, subagent=event_name == "SubagentStart",
        in_file=in_file, repo=repo_doc,
    ) + archive
    metrics.record(
        "context_delivery",
        harness=harness,
        hook=event_name,
        doc_seq=doc_seq,
        chars=len(context),
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


ARCHIVE_CHARS = 1500  # three short claims; real prompts rarely need more


def _archive_section(hits):
    """Unreviewed archive claims for one prompt, dated so age can be weighed.

    Archive search matched OptMem's recall on dropped facts at about a
    quarter of its tokens (E4-E6, 2026-10-19), but it also repeated a stale
    claim once in three runs. The label and the date are the guard.
    """
    lines, used = [], 0
    for h in hits:
        text = " ".join(h.content.split())
        line = f"- [{(h.created_at or '')[:10]} · seq {h.seq}] {text[:300]}"
        if used + len(line) > ARCHIVE_CHARS:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return ("\n\n## From the Naru archive (unreviewed)\n\nNo person approved these."
            " Prefer approved facts, weigh the date, and verify one before you rely"
            " on it.\n" + "\n".join(lines))


def hook_main(raw=None, harness="codex"):
    """Hook boundary for Codex and Claude Code. A Naru failure must not stop
    the agent."""
    try:
        event = json.loads(sys.stdin.read() if raw is None else raw)
        output = _hook(event, store(), harness)
    except Exception:
        return 0
    if output:
        json.dump(output, sys.stdout)
    return 0


def _spill_command(args):
    args = list(args)
    try:
        tool = _opt(args, "--tool", "stdin")
        session_id = _opt(args, "--session")
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    if args:
        print(f"unknown spill option {args[0]!r}", file=sys.stderr)
        return 2
    from hook_spill import spill_text

    try:
        replacement, _ = spill_text(
            sys.stdin.read(),
            tool=tool,
            session_id=session_id,
            harness="cli",
            database=DB,
            fail_open=False,
        )
    except Exception as error:
        print(f"naru spill failed: {error}", file=sys.stderr)
        return 1
    print(replacement, end="")
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd in ("codex-hook", "claude-hook"):
        return hook_main(harness=cmd.split("-")[0])
    ms = store()

    if cmd == "spill":
        return _spill_command(args)

    elif cmd == "add":
        if not args:
            print('need a topic: naru add "sprint planning" [text]', file=sys.stderr)
            return 2
        topic = args[0]
        body = " ".join(args[1:]) if len(args) > 1 else sys.stdin.read()
        if not body.strip():
            print("nothing to save (no text and empty stdin)", file=sys.stderr)
            return 2
        now = datetime.now().isoformat(timespec="minutes")
        # One row per paragraph: search returns the relevant chunk, not the
        # whole meeting.
        chunks = [c.strip() for c in body.split("\n\n") if c.strip()] or [body.strip()]
        seqs = [
            ms.append(
                "note", c, kind="note", session_id=f"{now[:10]} {topic}", created_at=now
            )
            for c in chunks
        ]
        print(f"saved {len(seqs)} chunk(s) as seq {seqs[0]}-{seqs[-1]} -> {DB}")

    elif cmd in ("claim", "skill"):
        try:
            key, by, base = _opt(args, "--key"), _opt(args, "--by"), _opt(args, "--base")
            run, source = _opt(args, "--run"), _opt(args, "--source")
            check = _opt(args, "--check")
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        scope = None
        if "--repo" in args:
            args.remove("--repo")
            scope = _repo_id(os.getcwd())
            if scope is None:
                print("--repo needs a git repository in the current folder", file=sys.stderr)
                return 2
        text = " ".join(args).strip() or sys.stdin.read().strip()
        if not text:
            print(f'need text: naru {cmd} "<text>" [--key k]', file=sys.stderr)
            return 2
        size = len(text.encode())
        if size > CLAIM_BYTES:
            print(
                f"too long: {size} bytes, limit {CLAIM_BYTES}. State one lasting"
                " fact and compress it. Accented characters cost 2 bytes.",
                file=sys.stderr,
            )
            return 2
        if bool(run) != bool(source):
            print("--run and --source LO:HI must be used together", file=sys.stderr)
            return 2
        if source:
            try:
                lo, hi = (int(x) for x in source.split(":", 1))
                if lo > hi:
                    raise ValueError
            except ValueError:
                print("--source must be LO:HI with LO <= HI", file=sys.stderr)
                return 2
        else:
            lo = hi = None
        if source and not metrics.opened(ms.store_id, run, lo, hi):
            cited = f"seq {lo}" if lo == hi else f"seq {lo}-{hi}"
            print(
                f"refused: nothing opened {cited}. A claim may cite only evidence"
                f' its author read: `naru show {lo} {hi} --run "{run}"` first.',
                file=sys.stderr,
            )
            return 2
        if base:
            try:
                base_seq = int(base)
            except ValueError:
                print("--base must be an integer", file=sys.stderr)
                return 2
        else:
            base_seq = ms.doc_version()
        try:
            seq = ms.append(
                "agent", text, kind=cmd, agent_id=by, topic_key=key,
                base_seq=base_seq, created_at=datetime.now().isoformat(timespec="seconds"),
                source_run_id=run, source_seq_lo=lo, source_seq_hi=hi, scope=scope,
                check_cmd=check,
            )
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        print(f"{cmd} {seq} pending" + (f" for {scope}" if scope else "")
              + " — `naru inbox` to decide")

    elif cmd in ("promote", "drop", "reject"):
        args = list(args)
        yes = "--yes" in args
        if yes:
            args.remove("--yes")
        if not args or not args[0].isdigit():
            print(f"need a seq: naru {cmd} 12", file=sys.stderr)
            return 2
        # A speed bump, NOT a security boundary. An agent that can run this CLI
        # can also pass --yes, so do not read this as "only a human can
        # promote". It exists so that promotion is never something a tool loop
        # does by accident, which is a different and much weaker claim.
        if not yes and not sys.stdin.isatty():
            print(
                f"refusing to {cmd} without a terminal; pass --yes if you mean it",
                file=sys.stderr,
            )
            return 2
        changed = (_reject(ms, int(args[0])) if cmd == "reject"
                   else _decide(ms, int(args[0]), cmd == "promote"))
        if not changed:
            print(
                f"seq {args[0]} is not a pending claim or skill this can {cmd}"
                " (already decided, or not a curation item)",
                file=sys.stderr,
            )
            return 1
        print(f"seq {args[0]} " + {"promote": "promoted", "drop": "archived",
                                    "reject": "rejected"}[cmd])
        if cmd == "promote":
            row = ms.sql_query("SELECT check_cmd, check_out FROM conversation_history"
                               " WHERE seq=?", (int(args[0]),))[0]
            if row.check_cmd:
                print(f"  check: {row.check_cmd} -> {row.check_out[:120]}")

    elif cmd == "inbox":
        pend, stale = ms.pending(), _stale(ms)
        if not pend and not stale:
            print("inbox clear")
            return 0
        print(PROMOTE_RULE + "\n")
        version = ms.doc_version()
        for i, c in enumerate(pend, 1):
            print(
                f"[{i}/{len(pend)}] {c.kind} seq {c.seq} · {c.agent_id or '?'} · "
                f"{(c.created_at or '')[:16]}"
                + (f" · key: {c.topic_key}" if c.topic_key else "")
                + (f" · repo: {c.scope}" if c.scope else "")
            )
            # Staleness cannot be prevented — the author was not wrong when
            # they started. Show it and let the human weigh it.
            if c.base_seq is not None and version > c.base_seq:
                print(
                    f"  ⚠ written against doc version {c.base_seq} — "
                    f"the doc is now at {version}"
                )
            print(f"  + {c.content.strip()}")
            if c.source_run_id:
                print(
                    f"  source: naru show {c.source_seq_lo} {c.source_seq_hi} "
                    f"--run {c.source_run_id}"
                )
            if c.check_cmd:
                print(f"  check: {c.check_cmd}   (runs when you promote)")
            try:
                ans = input("  promote / archive / reject / skip ? ").strip().lower()
            except EOFError:
                print("\n(no input — nothing decided)")
                return 0
            print("  " + _apply(ms, c.seq, ans) + "\n")
        for i, (c, now) in enumerate(stale, 1):
            print(f"[stale {i}/{len(stale)}] {c.kind} seq {c.seq}"
                  + (f" · key: {c.topic_key}" if c.topic_key else "")
                  + (f" · repo: {c.scope}" if c.scope else ""))
            print(f"  + {c.content.strip()}")
            print(f"  check: {c.check_cmd}\n  was: {c.check_out}\n  now: {now}")
            try:
                ans = input("  keep / drop / skip ? ").strip().lower()
            except EOFError:
                print("\n(no input — nothing decided)")
                return 0
            print("  " + _apply_stale(ms, c, now, ans) + "\n")

    elif cmd == "check":
        args = list(args)
        yes = "--yes" in args
        if yes:
            args.remove("--yes")
        if len(args) != 2 or not args[0].isdigit():
            print('need a seq and a command: naru check 12 "codex --version"',
                  file=sys.stderr)
            return 2
        # The command runs again at every inbox, so attaching one gets the
        # same speed bump as a promotion.
        if not yes and not sys.stdin.isatty():
            print("refusing to attach a check without a terminal; pass --yes"
                  " if you mean it", file=sys.stderr)
            return 2
        seq = int(args[0])
        row = ms.sql_query(
            "SELECT promoted FROM conversation_history WHERE seq=?"
            " AND kind IN ('claim','skill')", (seq,))
        if not row or row[0].promoted != 1:
            print(f"seq {seq} is not a promoted claim or skill", file=sys.stderr)
            return 1
        out = _run_check(args[1])
        ms.set_check(seq, args[1], out)
        print(f"seq {seq} check recorded: {out[:120]}")

    elif cmd == "inject":
        # One render for every file-based harness, spliced between markers so
        # it never destroys a file it does not own.
        text = ms.doc()
        if args:
            folder = pathlib.Path(args[0]).expanduser().parent
            if not folder.is_dir():
                # A traceback here told the user nothing. Not creating the
                # folder keeps a mistyped path from leaving a stray one.
                print(f"inject: folder {folder} does not exist; create it or fix the path",
                      file=sys.stderr)
                return 2
            wrote, had = _splice(args[0], text)
            ms.remember_target(os.path.realpath(args[0]))
            print(
                f"wrote {wrote} chars into {args[0]}"
                + (f" (kept the {had} chars already there)" if had else "")
            )
        else:
            print(text, end="")

    elif cmd == "search":
        if not args:
            print("need a query", file=sys.stderr)
            return 2
        k = int(args[-1]) if len(args) > 1 and args[-1].isdigit() else 8
        q = (
            " ".join(args[:-1])
            if len(args) > 1 and args[-1].isdigit()
            else " ".join(args)
        )
        hits = ms.search(q, k=k)
        metrics.record("search", q=q[:60], hits=len(hits))
        if not hits:
            print("no hits. try `naru outline` and `naru show LO HI` to browse.")
            return 1
        for h in hits:
            print(f"[{h.seq}] {h.created_at} | {h.session_id}")
            print(f"     {h.content.strip()[:300]}")

    elif cmd == "outline":
        # Human listing: the topic is the anchor you navigate by, and
        # ms.outline() deliberately omits session labels (they are opaque
        # hashes in the benchmark data it was built for).
        rows = ms.session_ranges()
        if not rows:
            print("(empty — nothing saved yet)")
            return 1
        for r in rows:
            print(
                f"seq {r.lo}-{r.hi} | {str(r.at)[:16]} | {r.n} chunk(s) | "
                f"{r.session_id}"
            )

    elif cmd == "show":
        try:
            run = _opt(args, "--run")
            at = _opt(args, "--at", "0")
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        if not args:
            print("need a seq: naru show 12 [18] [--run ID] [--at CHAR]", file=sys.stderr)
            return 2
        try:
            lo = int(args[0])
            hi = int(args[1]) if len(args) > 1 else None
            at = int(at)
            if at < 0:
                raise ValueError
        except ValueError:
            print("show sequence values and --at must be whole numbers", file=sys.stderr)
            return 2
        rows = ms.expand(lo, hi, session_id=run) if run else ms.expand(lo, hi)
        _show(ms, rows, run, at)

    elif cmd == "prune":
        days = int(args[0]) if args and args[0].isdigit() else 30
        dry = "--dry-run" in args or "-n" in args
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        n_doomed, b_doomed, n_kept = ms.prune_preview(cutoff)
        print(f"cutoff {cutoff[:10]} (older than {days} days)")
        print(f"  would remove : {n_doomed} rows, {b_doomed:,} chars")
        print(f"  would keep   : {n_kept} rows (promoted claims, skills, and evidence are never aged out)")
        if dry:
            print("  --dry-run: nothing deleted")
            return 0
        if not n_doomed:
            return 0
        removed = ms.prune(cutoff)
        print(f"  removed {removed} rows, index cleaned, database vacuumed")
        # Metrics age out with the rows they describe. This is also the ONE
        # place the file is size-capped: record() used to trim in-line, which
        # raced across parallel hook processes and dropped new events.
        kept_ev = [e for e in metrics.read() if e.get("t", "") >= cutoff]
        while len(_j_dumps(kept_ev)) > metrics.MAX_BYTES:
            kept_ev = kept_ev[len(kept_ev) // 4 :]
        try:
            metrics.PATH.write_text(_j_dumps(kept_ev))
            print(f"  metrics trimmed to {len(kept_ev)} event(s)")
        except OSError:
            pass

    elif cmd == "stats":
        days = int(args[0]) if args and args[0].isdigit() else None
        # One owner for the threshold. Re-reading the env with a different
        # default made `stats` advise against a number the hook never used.
        from hook_spill import THRESHOLD

        print("\n".join(metrics.report(days=days, threshold=THRESHOLD)))

    elif cmd == "gc":
        n, freed = ms.gc_blobs()
        print(f"removed {n} orphaned blob item(s), freed {freed / 1024:.0f} KB")

    else:
        print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
        return 2
    return 0


def demo():
    """Self-check against a throwaway store."""
    import tempfile

    global DB
    tmp = tempfile.TemporaryDirectory()
    DB = pathlib.Path(tmp.name) / "t.db"
    metrics.PATH = DB.parent / "m.jsonl"  # never touch the real store
    # The Codex hook reads $CODEX_HOME/AGENTS.md; never read the user's.
    real_codex_home = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = str(DB.parent / "codex-home")
    # Claude Code reads ~/.claude/CLAUDE.md; never read the user's.
    real_home, real_cwd = os.environ.get("HOME"), os.getcwd()
    os.environ["HOME"] = str(DB.parent / "home")
    (DB.parent / "home").mkdir()

    # Close the door rather than hoping it is shut. Several assertions below
    # depend on stdin being at EOF; inheriting the caller's stdin meant
    # `python3 naru.py --selfcheck` — the exact pre-commit command — hung
    # forever on a terminal, at `claim` with no text and again at `inbox`.
    real_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        _demo(real_stdin)
    finally:
        sys.stdin = real_stdin
        os.chdir(real_cwd)
        for name, value in (("CODEX_HOME", real_codex_home), ("HOME", real_home)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _demo(real_stdin):
    assert (
        main(
            [
                "add",
                "naru review",
                "Deferred the eviction index; worth 1.8 pts.\n\n"
                "Slack is the best adapter target.",
            ]
        )
        == 0
    )
    assert main(["add", "sprint planning", "Ship the Email MFE by Friday."]) == 0

    ms = store()
    # paragraphs became separate rows, so recall is chunk-level
    assert len(ms.search("eviction")) == 1, ms.search("eviction")
    assert ms.search("eviction")[0].seq == 1
    assert ms.search("Slack")[0].seq == 2, "second paragraph should be its own row"
    assert ms.search("Friday")[0].seq == 3

    # ms.outline() previews notes even though no row has role='user'
    ol = ms.outline()
    assert "Deferred the eviction" in ol, f"outline preview empty: {ol}"
    assert "Ship the Email MFE" in ol, ol

    # the CLI listing shows the topic, which is what a human navigates by
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["outline"]) == 0
    listing = buf.getvalue()
    assert "naru review" in listing and "sprint planning" in listing, listing

    # verbatim recovery by address
    assert "1.8 pts" in ms.expand(1)[0].content

    # unknown command and empty input are rejected, not silently accepted
    assert main(["bogus"]) == 2
    assert main(["add"]) == 2

    # prune removes old rows AND their FTS entries; recent rows survive
    old = ms.append(
        "tool",
        "ANCIENTMARKER payload",
        kind="tool_result",
        created_at="2000-01-01T00:00:00",
    )
    assert ms.search("ANCIENTMARKER"), "should be findable before prune"
    removed = ms.prune("2001-01-01T00:00:00")
    assert removed == 1, removed
    assert ms.search("ANCIENTMARKER") == [], "FTS index not cleaned by prune"
    assert ms.expand(old) == [], "row survived prune"
    assert ms.search("eviction"), "prune must not touch recent rows"

    # a missing blob must not break recovery — content column is authoritative
    ms2 = MemorySurface(str(DB.parent / "b.db"))
    big = "FULLTEXT " + "z" * 5000
    s2 = ms2.append("tool", big, created_at="2026-01-01T00:00", payload=big)
    row = ms2.sql_query(
        "SELECT payload_path FROM conversation_history WHERE seq=?", (s2,)
    )[0]
    pathlib.Path(row.payload_path).unlink()  # simulate a /var/folders purge
    got = ms2.expand(s2)[0].content
    assert got.startswith("FULLTEXT"), "recovery broke when the blob vanished"

    # ---- curation through the CLI -----------------------------------------
    assert (
        main(
            [
                "claim",
                "Store is SQLite, WAL on.",
                "--key",
                "store.engine",
                "--by",
                "claude-opus",
            ]
        )
        == 0
    )
    assert (
        main(["claim", "Retry budget is 5.", "--key", "bench.retries", "--by", "gpt-5"])
        == 0
    )
    assert main(["claim"]) == 2

    pend = store().pending()
    assert len(pend) == 2, pend
    assert pend[0].agent_id == "claude-opus" and pend[0].topic_key == "store.engine"
    assert pend[0].base_seq is not None, "a claim must record the doc it saw"

    # promotion refuses to run unattended without an explicit --yes
    assert main(["promote", str(pend[0].seq)]) == 2, "unattended promote allowed"
    assert main(["promote", str(pend[0].seq), "--yes"]) == 0
    assert main(["drop", str(pend[1].seq), "--yes"]) == 0
    assert store().pending() == [], "decided claims must leave the inbox"
    # promoting twice is refused, not silently re-applied
    assert main(["promote", str(pend[0].seq), "--yes"]) == 1
    assert main(["promote", "9999", "--yes"]) == 1
    assert main(["promote", "--yes"]) == 2
    # A promoted fact stays revisable: retire the old one, promote the new one.
    # Without this, superseding leaves both promoted and parks the key under
    # ## Unresolved forever, losing the fact the doc used to state.
    assert main(["drop", str(pend[0].seq), "--yes"]) == 0, (
        "promoted claim not retirable"
    )
    assert main(["promote", str(pend[0].seq), "--yes"]) == 1, (
        "a dropped claim came back"
    )
    assert (
        main(
            [
                "claim",
                "Store is SQLite, WAL on.",
                "--key",
                "store.engine",
                "--by",
                "human",
            ]
        )
        == 0
    )
    fresh = store().pending()[-1].seq
    assert main(["promote", str(fresh), "--yes"]) == 0
    assert store().conflicts() == {}, "superseding must not create a conflict"

    # _apply is the inbox's decision logic, reachable without a terminal
    ms_a = store()
    escaped = ms_a.append("agent", f"literal {BEGIN} {END}", kind="claim")
    assert _apply(ms_a, escaped, "p") == "promoted"
    assert BEGIN not in ms_a.doc() and END not in ms_a.doc()
    s = ms_a.append(
        "agent",
        "apply path",
        kind="claim",
        agent_id="t",
        created_at="2026-08-27T12:00:00",
    )
    assert _apply(ms_a, s, "skip") == "skipped"
    assert _apply(ms_a, s, "p") == "promoted"
    assert _apply(ms_a, s, "p") == "already decided elsewhere", (
        "inbox must not claim success when another writer already decided"
    )
    assert main(["drop", str(s), "--yes"]) == 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["inject"]) == 0
    doc = buf.getvalue()
    assert "Store is SQLite" in doc, doc
    assert "Retry budget" not in doc, "a dropped claim reached the doc"

    # inject must NEVER destroy a file it does not own. The documented path is
    # `naru inject AGENTS.md`, a file the user wrote.
    hand = DB.parent / "AGENTS.md"
    hand.write_text("# My rules\n\nAlways run the tests before committing.\n")
    assert main(["inject", str(hand)]) == 0
    body = hand.read_text()
    assert "Always run the tests" in body, "inject destroyed a hand-written file"
    assert "Store is SQLite" in body, "inject did not add the doc"
    assert body.count(BEGIN) == 1 and body.count(END) == 1, body
    assert "naru: begin" in body and "naru: end" in body, body

    # re-injecting replaces the block in place, it does not stack copies
    assert main(["inject", str(hand)]) == 0
    body2 = hand.read_text()
    assert body2.count(BEGIN) == 1, "re-inject appended a second block"
    assert "Always run the tests" in body2, "re-inject lost the user's content"

    # Every decision rewrites the files inject wrote, from the CLI and from
    # the inbox alike, so a file-based harness never reads an old doc.
    assert main(["claim", "Refresh marker fact.", "--key", "refresh.cli"]) == 0
    marker = store().pending()[-1].seq
    assert main(["promote", str(marker), "--yes"]) == 0
    assert "Refresh marker fact." in hand.read_text(), "promote left a file stale"
    assert "Always run the tests" in hand.read_text(), "refresh lost the user's content"
    assert main(["drop", str(marker), "--yes"]) == 0
    assert "Refresh marker fact." not in hand.read_text(), "drop left a file stale"
    assert main(["claim", "Inbox marker fact.", "--key", "refresh.inbox"]) == 0
    marker = store().pending()[-1].seq
    assert _apply(store(), marker, "p") == "promoted"
    assert "Inbox marker fact." in hand.read_text(), "inbox promote left a file stale"
    assert main(["drop", str(marker), "--yes"]) == 0
    # A copy of the store carries the same file list, but its decisions must
    # never rewrite the real files.
    import sqlite3
    copy_db = DB.parent / "copy.db"
    with contextlib.closing(sqlite3.connect(DB)) as src, \
            contextlib.closing(sqlite3.connect(copy_db)) as dst:
        src.backup(dst)  # a file copy would miss rows still in the WAL
    copy = MemorySurface(str(copy_db))
    assert copy.db.execute("SELECT COUNT(*) FROM inject_target").fetchone()[0], (
        "the copy has no file list, so this check proves nothing"
    )
    copied = copy.append("agent", "Copy-only fact.", kind="claim", topic_key="copy.only")
    before = hand.read_text()
    assert _decide(copy, copied, True) == 1
    assert hand.read_text() == before, "a copied store rewrote the real file"
    copy.close()
    # Removing the markers opts a file out: Naru forgets it and stops writing.
    hand.write_text("# My rules only\n")
    assert main(["claim", "Opt-out marker.", "--key", "refresh.optout"]) == 0
    marker = store().pending()[-1].seq
    assert main(["promote", str(marker), "--yes"]) == 0
    assert hand.read_text() == "# My rules only\n", "refresh wrote a file without markers"
    assert os.path.realpath(hand) not in store().inject_targets()
    assert main(["drop", str(marker), "--yes"]) == 0

    missing = DB.parent / "no-such-folder" / "AGENTS.md"
    assert main(["inject", str(missing)]) == 2, "inject into a missing folder must fail cleanly"
    assert not missing.parent.exists(), "inject created a folder"

    unreadable = DB.parent / "invalid-utf8.md"
    original = b"\xff\xfehand-written"
    unreadable.write_bytes(original)
    try:
        main(["inject", str(unreadable)])
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("inject overwrote a file it could not read")
    assert unreadable.read_bytes() == original

    # Codex gets the approved doc at startup, then only after the doc changes.
    codex_id = "session-" + "x" * 80
    codex_run = f"codex:{codex_id}"
    start = _hook(
        {"hook_event_name": "SessionStart", "session_id": codex_id}, store()
    )
    assert start["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Store is SQLite" in start["hookSpecificOutput"]["additionalContext"]
    assert "naru claim \"" in start["hookSpecificOutput"]["additionalContext"]
    state = context_delivery(store(), "codex", codex_run)
    assert state is not None
    assert state.session_id == codex_run, "Codex session ID was truncated"
    assert state.schema == CODEX_STATE_SCHEMA
    assert state.doc_seq == store().doc_version()
    assert state.doc_hash == hashlib.sha256(store().doc().encode()).hexdigest()
    assert not store().sql_query(
        "SELECT seq FROM conversation_history"
        " WHERE kind='agent_state' AND agent_id='codex'"
    ), "Codex delivery metadata reached the Event Log"
    delivery = [e for e in metrics.read() if e.get("e") == "context_delivery"][-1]
    assert delivery["harness"] == "codex" and delivery["hook"] == "SessionStart"

    legacy_store = MemorySurface(str(DB.parent / "legacy-codex.db"))
    legacy_seq = legacy_store.append(
        "agent",
        json.dumps(
            {
                "schema": CODEX_STATE_SCHEMA,
                "doc_seq": 12,
                "doc_hash": "legacy-hash",
            },
            separators=(",", ":"),
        ),
        kind="agent_state",
        session_id="codex:legacy",
        agent_id="codex",
        created_at="2026-09-01T12:00:00",
    )
    assert _hook_seen(legacy_store, "codex:legacy") == "legacy-hash"
    assert context_delivery(legacy_store, "codex", "codex:legacy").doc_seq == 12
    assert legacy_store.sql_query(
        "SELECT seq FROM conversation_history WHERE seq=?", (legacy_seq,)
    ), "legacy delivery migration deleted its audit row"
    legacy_store.close()
    assert _hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": codex_id,
            "prompt": "PRIVATE_CODEX_PROMPT",
        },
        store(),
    ) is None, "unchanged Naru context was sent twice"
    assert not store().sql_query(
        "SELECT seq FROM conversation_history WHERE content LIKE ?",
        ("%PRIVATE_CODEX_PROMPT%",),
    ), "Codex prompt was persisted"

    assert main(["claim", "Codex refresh marker.", "--key", "codex.refresh"]) == 0
    codex_claim = store().pending()[-1].seq
    assert main(["promote", str(codex_claim), "--yes"]) == 0
    assert main(["claim", "Codex high marker.", "--key", "codex.high"]) == 0
    codex_high = store().pending()[-1].seq
    assert main(["promote", str(codex_high), "--yes"]) == 0
    refresh = _hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": codex_id}, store()
    )
    assert "Codex refresh marker" in refresh["hookSpecificOutput"]["additionalContext"]
    assert "Codex high marker" in refresh["hookSpecificOutput"]["additionalContext"]
    assert _hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": codex_id}, store()
    ) is None, "refreshed Naru context was sent twice"

    assert main(["drop", str(codex_claim), "--yes"]) == 0
    retired = _hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": codex_id}, store()
    )
    assert "Codex refresh marker" not in retired["hookSpecificOutput"]["additionalContext"]
    assert "Codex high marker" in retired["hookSpecificOutput"]["additionalContext"]
    assert _hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": codex_id}, store()
    ) is None, "retired Naru context was sent twice"
    assert main(["drop", str(codex_high), "--yes"]) == 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert hook_main(
            json.dumps(
                {"hook_event_name": "SubagentStart", "session_id": codex_id}
            )
        ) == 0
    subagent = json.loads(buf.getvalue())
    assert subagent["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert "Store is SQLite" in subagent["hookSpecificOutput"]["additionalContext"]
    assert "Codex high marker" not in subagent["hookSpecificOutput"]["additionalContext"]
    assert "Do not run `naru claim`" in subagent["hookSpecificOutput"]["additionalContext"]
    assert "naru claim \"" not in subagent["hookSpecificOutput"]["additionalContext"], (
        "a subagent was told to file claims"
    )
    assert _hook(
        {"hook_event_name": "PostToolUse", "session_id": codex_id}, store()
    ) is None, "unsupported Codex event produced output"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert hook_main("not json") == 0
    assert not buf.getvalue(), "malformed Codex input produced output"

    # When $CODEX_HOME/AGENTS.md already holds the doc, a new session gets the
    # filing line only. Later prompts and subagents still get the whole doc.
    codex_home = pathlib.Path(os.environ["CODEX_HOME"])
    codex_home.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["inject", str(codex_home / "AGENTS.md")]) == 0
    filed_id = "session-in-file"
    start = _hook(
        {"hook_event_name": "SessionStart", "session_id": filed_id}, store()
    )["hookSpecificOutput"]["additionalContext"]
    assert "Store is SQLite" not in start, "the doc was sent twice"
    assert "naru claim \"" in start and "already in your instructions" in start
    assert _hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": filed_id}, store()
    ) is None, "the first prompt resent a doc the session already has"
    assert main(["claim", "In-file refresh marker.", "--key", "codex.infile"]) == 0
    marker = store().pending()[-1].seq
    assert main(["promote", str(marker), "--yes"]) == 0
    refresh = _hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": filed_id}, store()
    )
    assert "In-file refresh marker." in (
        refresh or {"hookSpecificOutput": {"additionalContext": ""}}
    )["hookSpecificOutput"]["additionalContext"], (
        "a running session missed a promotion because the file was current"
    )
    sub = _hook(
        {"hook_event_name": "SubagentStart", "session_id": filed_id}, store()
    )["hookSpecificOutput"]["additionalContext"]
    assert "In-file refresh marker." in sub, "a subagent lost the doc"
    assert main(["drop", str(marker), "--yes"]) == 0

    # A repository fact never enters the general doc or a file. The hooks add
    # it only for a session inside that repository.
    widget = DB.parent / "widget"
    (widget / "src").mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-C", str(widget), *a], check=True,
                                    capture_output=True)
    git("init", "-q")
    git("remote", "add", "origin", "git@github.com:acme/widget.git")
    assert _repo_id(widget / "src") == "acme/widget", "an ssh remote lost its owner"
    git("remote", "set-url", "origin", "https://github.com/acme/widget.git")
    assert _repo_id(widget) == "acme/widget"
    here = os.getcwd()
    os.chdir(DB.parent)
    assert main(["claim", "Widget fact.", "--key", "widget", "--repo"]) == 2, (
        "--repo outside a repository was accepted"
    )
    os.chdir(widget / "src")
    assert main(["claim", "Widget fact.", "--key", "widget", "--repo"]) == 0
    os.chdir(here)
    marker = store().pending()[-1]
    assert marker.scope == "acme/widget", "--repo stored no scope"
    assert main(["promote", str(marker.seq), "--yes"]) == 0
    assert "Widget fact." not in store().doc(), "a repository fact reached the general doc"
    assert "Widget fact." not in (codex_home / "AGENTS.md").read_text()
    inside = _hook({"hook_event_name": "SessionStart", "session_id": "in-widget",
                    "cwd": str(widget)}, store())["hookSpecificOutput"]["additionalContext"]
    assert "## This repository (acme/widget)" in inside and "Widget fact." in inside
    assert "Store is SQLite" not in inside, "the in-file shortcut dropped with repo facts"
    outside = _hook({"hook_event_name": "SubagentStart", "session_id": "elsewhere",
                     "cwd": str(DB.parent)}, store())["hookSpecificOutput"]["additionalContext"]
    assert "Widget fact." not in outside, "a repository fact left its repository"
    sub = _hook({"hook_event_name": "SubagentStart", "session_id": "in-widget",
                 "cwd": str(widget)}, store())["hookSpecificOutput"]["additionalContext"]
    assert "Widget fact." in sub and "Store is SQLite" in sub

    # Claude Code reads AGENTS.md only when no CLAUDE file is in the path, so
    # the in-file shortcut must follow the same rule.
    claude_dir = DB.parent / "claude-project"
    claude_dir.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["inject", str(claude_dir / "AGENTS.md")]) == 0
    def claude_start(session):
        return _hook({"hook_event_name": "SessionStart", "session_id": session,
                      "cwd": str(claude_dir)}, store(), "claude")[
            "hookSpecificOutput"]["additionalContext"]
    assert "Store is SQLite" not in claude_start("c1"), "Claude got the doc twice"
    assert "--by claude" in claude_start("c1b")
    (claude_dir / "CLAUDE.md").write_text("# rules\n")
    assert "Store is SQLite" in claude_start("c2"), (
        "Claude ignores AGENTS.md beside a CLAUDE.md, but the hook skipped the doc"
    )
    assert context_delivery(store(), "claude", "claude:c2") is not None
    assert context_delivery(store(), "codex", "claude:c2") is None, (
        "the harnesses share delivery state"
    )
    assert main(["drop", str(marker.seq), "--yes"]) == 0

    # reject hides a claim from archive search; drop archives it, and also
    # undoes a reject. A prompt hook adds matching archive claims, labeled.
    assert main(["claim", "Archive lead: glab api PUT returns HTTP 415 without a JSON header.",
                 "--key", "glab.lead"]) == 0
    lead = store().pending()[-1].seq
    assert main(["drop", str(lead), "--yes"]) == 0
    assert main(["claim", "Wrong lead: glab api PUT never needs a JSON header.",
                 "--key", "glab.bad"]) == 0
    bad = store().pending()[-1].seq
    assert main(["reject", str(bad)]) == 2, "reject skipped the speed bump"
    assert _apply(store(), bad, "r") == "rejected"
    _hook({"hook_event_name": "SessionStart", "session_id": "archive-session",
           "cwd": str(DB.parent)}, store())
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "archive-session",
              "cwd": str(DB.parent), "prompt": "my glab api PUT call returns 415"}
    extra = _hook(prompt, store())["hookSpecificOutput"]["additionalContext"]
    assert extra.startswith("## From the Naru archive (unreviewed)"), extra[:80]
    assert f"seq {lead}]" in extra and f"seq {bad}]" not in extra, "rejected claim surfaced"
    assert "Store is SQLite" not in extra, "an unchanged doc was resent with the archive"
    assert _hook({**prompt, "prompt": "tell me about the weather today"}, store()) is None
    assert main(["claim", "Promoted then rejected.", "--key", "reject.doc"]) == 0
    gone = store().pending()[-1].seq
    assert main(["promote", str(gone), "--yes"]) == 0
    assert "Promoted then rejected." in (codex_home / "AGENTS.md").read_text()
    assert main(["reject", str(gone), "--yes"]) == 0
    assert "Promoted then rejected." not in (codex_home / "AGENTS.md").read_text(), (
        "reject left a retired fact in an injected file"
    )
    assert "Promoted then rejected." not in store().doc(), "reject left a fact in the doc"
    assert main(["drop", str(bad), "--yes"]) == 0, "a reject could not be undone"

    # A check records what a fact was confirmed against. When its output
    # changes, the inbox asks the person to keep or retire the fact.
    version = DB.parent / "tool-version.txt"
    version.write_text("1.0\n")
    assert main(["claim", "Tool 1.0 quirk.", "--key", "tool.quirk",
                 "--check", f"cat {version}"]) == 0
    fact = store().pending()[-1].seq
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["inbox"])
    assert f"check: cat {version}" in buf.getvalue(), "inbox hid the check command"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["promote", str(fact), "--yes"]) == 0
    assert "-> 1.0" in buf.getvalue(), "promote ran a check without showing it"
    assert [r.check_out for r in store().checked()] == ["1.0"], "no baseline at promote"
    assert _stale(store()) == []
    version.write_text("2.0\n")
    stale = _stale(store())
    assert [(r.seq, now) for r, now in stale] == [(fact, "2.0")], "a changed check was missed"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["inbox"])
    assert "[stale 1/1]" in buf.getvalue() and "now: 2.0" in buf.getvalue()
    assert _apply_stale(store(), *stale[0], "k") == "kept; check updated"
    assert _stale(store()) == [], "keep did not record the new output"
    version.write_text("3.0\n")
    assert _apply_stale(store(), *_stale(store())[0], "d") == "dropped"
    assert "Tool 1.0 quirk." not in store().doc() and store().checked() == []
    # Attach a check to a fact promoted before checks existed.
    assert main(["claim", "Old fact.", "--key", "old.fact"]) == 0
    old = store().pending()[-1].seq
    assert main(["check", str(old), f"cat {version}", "--yes"]) == 1, (
        "a check attached to a pending claim"
    )
    assert main(["promote", str(old), "--yes"]) == 0
    assert main(["check", str(old), f"cat {version}"]) == 2, "check skipped the speed bump"
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["check", str(old), f"cat {version}", "--yes"]) == 0
    assert [r.check_out for r in store().checked()] == ["3.0"]
    assert main(["drop", str(old), "--yes"]) == 0
    assert _run_check("definitely-not-a-command-xyz").startswith("failed")
    assert _run_check(f"cat {DB.parent / 'missing'}").startswith("exit 1")
    saved_check_seconds = CHECK_SECONDS
    globals()["CHECK_SECONDS"] = 1
    try:
        assert _run_check("sleep 5") == "timed out after 1s", "a hung check blocked"
    finally:
        globals()["CHECK_SECONDS"] = saved_check_seconds

    # a note is not a claim: `add` must never reach the doc
    assert main(["add", "notes topic", "This line is a note, not a claim."]) == 0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["inject"])
    assert "not a claim" not in buf.getvalue(), "a note leaked into the doc"

    trace_run = "trace-run"
    reply = store().append(
        "agent", "```python\\nprint('evidence')\\n```", kind="agent_reply",
        session_id=trace_run, created_at="2026-08-27T12:00:00",
    )
    observation = store().append(
        "tool", "evidence", kind="agent_observation", session_id=trace_run,
        created_at="2026-08-27T12:00:01",
    )
    # Evidence rule: the evidence exists, but nobody has opened it yet.
    assert main([
        "claim", "unread", "--key", "unread.fact", "--run", trace_run,
        "--source", f"{reply}:{observation}",
    ]) == 2, "a claim cited evidence no reader had opened"
    assert not [c for c in store().pending() if c.content == "unread"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["show", str(reply), str(observation), "--run", "wrong"]) == 0
    assert not buf.getvalue(), "a wrong-run show printed rows"
    assert not metrics.opened(ms.store_id, "wrong", reply, observation)
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["show", str(reply), "--run", trace_run]) == 0
        assert main(["show", str(observation), "--run", trace_run]) == 0
    assert main([
        "claim", "split", "--key", "split.fact", "--run", trace_run,
        "--source", f"{reply}:{observation}",
    ]) == 2, "two endpoint receipts authorized an unopened middle"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["show", str(reply), str(observation), "--run", trace_run]) == 0
    assert "evidence" in buf.getvalue()
    other_db = DB.parent / "other.db"
    other = MemorySurface(str(other_db))
    assert not metrics.opened(other.store_id, trace_run, reply, observation), (
        "a receipt crossed Event Log stores"
    )
    other_seq = other.append("tool", "preview", payload="B" * 5000)
    other_payload = pathlib.Path(other.sql_query(
        "SELECT payload_path FROM conversation_history WHERE seq=?", (other_seq,)
    )[0].payload_path)
    other.close()
    primary = store()
    primary.blobs.mkdir(parents=True, exist_ok=True)
    orphan = primary.blobs / "orphan.txt"
    orphan.write_text("orphan")
    primary.close()
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["gc"]) == 0
    assert not orphan.exists() and other_payload.exists(), "gc crossed Event Log stores"
    other_payload.unlink()
    other_payload.parent.rmdir()
    assert main([
        "claim", "trace fact", "--key", "trace.fact", "--run", trace_run,
        "--source", f"{reply}:{observation}",
    ]) == 0
    trace_claim = store().pending()[-1]
    assert trace_claim.source_run_id == trace_run
    assert (trace_claim.source_seq_lo, trace_claim.source_seq_hi) == (reply, observation)
    assert main(["claim", "partial", "--run", trace_run]) == 2
    assert main(["claim", "partial", "--source", f"{reply}:{observation}"]) == 2
    assert main(["claim", "bad", "--run", trace_run, "--source", "4:nope"]) == 2
    assert main(["claim", "wrong run", "--run", "wrong", "--source", f"{reply}:{observation}"]) == 2
    assert main(["claim", "bad", "--base", "nope"]) == 2
    assert main(["claim", "missing", "--key"]) == 2
    assert main(["claim", "missing", "--key", ""]) == 2
    assert main(["skill", "missing", "--run"]) == 2
    # The limit is in bytes, the unit the doc's token cost follows.
    before = len(store().pending())
    assert main(["claim", "x" * CLAIM_BYTES, "--key", "limit.edge"]) == 0
    assert main(["claim", "x" * (CLAIM_BYTES + 1), "--key", "limit.over"]) == 2
    assert main(["skill", "é" * (CLAIM_BYTES // 2 + 1), "--key", "limit.bytes"]) == 2, (
        "the limit counted characters, not bytes"
    )
    assert len(store().pending()) == before + 1, "an over-limit claim was stored"
    assert main(["show", str(reply), "--run"]) == 2
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["inbox"]) == 0
    assert f"claim seq {trace_claim.seq}" in buf.getvalue()
    assert PROMOTE_RULE in buf.getvalue(), "inbox hid the promotion rule"
    assert f"naru show {reply} {observation} --run {trace_run}" in buf.getvalue()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["show", str(reply), str(observation), "--run", trace_run]) == 0
    assert "evidence" in buf.getvalue()

    # Evidence rule: show prints parts a harness keeps whole, and a receipt covers
    # only the rows it printed whole.
    saved_show_chars = SHOW_CHARS
    globals()["SHOW_CHARS"] = 200
    try:
        page = "page-run"
        first, second, big = (
            store().append("tool", body, kind="agent_observation", session_id=page,
                           created_at="2026-08-27T12:00:02")
            for body in ("A" * 90, "B" * 90, "C" * 150 + "D" * 150)
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main(["show", str(first), str(big), "--run", page]) == 0
        out = buf.getvalue()
        assert "A" * 90 in out and "B" * 90 not in out, "show ignored its budget"
        assert f"naru show {second} {big} --run {page}" in out, "no way to continue"
        assert metrics.opened(ms.store_id, page, first, first)
        assert not metrics.opened(ms.store_id, page, first, second), (
            "a receipt covered a row that was not printed"
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main(["show", str(big), "--run", page]) == 0
        out = buf.getvalue()
        assert "C" * 150 + "D" * 50 in out and "D" * 51 not in out, out
        assert f"naru show {big} --at 200 --run {page}" in out, "no way to continue"
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["show", str(big), "--at", "200", "--run", page]) == 0
        assert not metrics.opened(ms.store_id, page, big, big), (
            "a slice of a row wrote a receipt for the whole row"
        )
        assert main(["show", str(big), "--at", "-1"]) == 2
    finally:
        globals()["SHOW_CHARS"] = saved_show_chars

    assert main(["skill", "Search the Event Log first.", "--key", "lookup"]) == 0
    skill = store().pending()[-1]
    assert skill.kind == "skill" and "Search the Event Log" not in store().doc()
    assert main(["promote", str(skill.seq), "--yes"]) == 0
    assert "## Skills" in store().doc() and "Search the Event Log" in store().doc()

    # inbox at EOF must not hang and must not decide anything on its own
    pre = len(store().pending())
    assert main(["claim", "pending forever", "--by", "codex"]) == 0
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["inbox"]) == 0
    assert len(store().pending()) == pre + 1, "inbox decided something unprompted"

    # `claim` with no text and no stdin is an error, not a block
    assert main(["claim"]) == 2

    print("ok — naru checks passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        demo()
    else:
        sys.exit(main(sys.argv[1:]))
