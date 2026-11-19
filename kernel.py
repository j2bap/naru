"""Persistent Python kernel — the resident namespace of a Session Environment.

Variables survive across model calls, so tool outputs and derived state live
here as objects instead of being serialized into the prompt. Only what the
model's code prints crosses back into its working view.
"""

import contextlib
import io
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import traceback

MAX_OUT = 8000  # chars of printed output allowed into the working view


def _cap(out):
    """Truncate a cell's output to what may enter the model's view.

    One owner, and applied in the CHILD. Capping only in the parent still
    required readline() to pull the whole line in first: a cell printing 200MB
    took the parent from 19MB to 687MB of RSS before the truncation ran, which
    is precisely the memory bomb ADR 0007 claims the child absorbs.
    """
    if len(out) <= MAX_OUT:
        return out
    return out[:MAX_OUT] + f"\n[output truncated at {MAX_OUT} chars]"


def _provenance(v):
    """Which Event Log addresses a resident value derives from.

    Section 2.2 requires resident objects to carry "type, size, and provenance
    metadata identifying the events it derives from". Log rows expose .seq, so
    a container of rows reports its seq range and the model can go straight
    back to the source without re-searching.
    """
    items = v[:200] if isinstance(v, (list, tuple)) else []
    seqs = [i["seq"] for i in items
            if isinstance(i, dict) and isinstance(i.get("seq"), int)]
    if not seqs:
        return ""
    lo, hi = min(seqs), max(seqs)
    return f" from seq {lo}" if lo == hi else f" from seq {lo}-{hi}"


def _shape(v):
    """Compact type+size description for the namespace digest."""
    t = type(v).__name__
    if isinstance(v, (str, bytes)):
        return f"{t}[{len(v)}]"
    if isinstance(v, (list, tuple, set, dict)):
        return f"{t}[{len(v)}]{_provenance(v)}"
    if isinstance(v, (int, float, bool)) or v is None:
        return f"{t}={v!r}"[:40]
    return t


def _real_interrupt(e):
    """Whether `e` can be a real Ctrl-C and not something a cell raised.

    Python delivers SIGINT to the main thread only, so a KeyboardInterrupt in
    any other thread came from model code. In the main thread the two look the
    same, and there stopping the program has to win.
    """
    return (
        isinstance(e, KeyboardInterrupt)
        and threading.current_thread() is threading.main_thread()
    )


def _digest(ns, preloaded):
    """The resident listing: name, type, shape. One owner for both kernels.

    Describing a variable runs model code. `len()` and `v[:200]` call a model
    class's own methods, and repr() of a 5000-digit int raises ValueError.
    Unguarded, one such variable raised out of run_naru in-process and killed
    the child in the sandbox, so each variable is guarded on its own and the
    rest of the listing is still produced.
    """
    items = []
    for k, v in ns.items():
        if k.startswith("_") or k in preloaded or callable(v):
            continue
        try:
            shape = _shape(v)
        except BaseException as e:
            if _real_interrupt(e):
                raise
            shape = f"<unreadable: {type(e).__name__}>"
        items.append(f"{k}: {shape}")
    return "resident: " + (", ".join(items) if items else "(empty)")


class Kernel:
    """A namespace + exec. Model-authored cells run here.

    ponytail: in-process exec, no sandbox. Fine for benchmark runs on local
    data; move to a subprocess or container before running untrusted code.
    """

    def __init__(self, **preload):
        self.ns = {"__name__": "__naru__"}
        self.ns.update(preload)
        self._preloaded = set(preload)

    def run(self, code):
        """Execute a cell. Returns (printed_output, error_or_None)."""
        buf = io.StringIO()
        err = None
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                exec(compile(code, "<cell>", "exec"), self.ns)
        except BaseException as e:
            # BaseException, not Exception: sys.exit(), `raise
            # KeyboardInterrupt` and `raise BaseException()` in a cell each
            # unwound out of run_naru and ended a paid bench run mid-flight.
            # A real Ctrl-C must still reach you.
            if _real_interrupt(e):
                raise
            tb = traceback.format_exc(limit=2).strip().splitlines()
            err = tb[-1] if tb else "error"
        return _cap(buf.getvalue()), err

    def close(self):
        """Nothing to release; present so callers need no isinstance check."""

    def digest(self):
        """Short listing of resident variables: name, type, shape.

        Prepended to every model call so the model knows what state it holds
        without paying to re-print it.
        """
        return _digest(self.ns, self._preloaded)


# ---------------------------------------------------------------------------
# The child program. Reads one JSON request per line on stdin, writes one JSON
# reply per line on the duplicated stdout, and keeps its namespace between
# requests so state survives across cells exactly as the in-process Kernel does.



class _Done(Exception):
    """Raised by the submit_answer stub to stop the rest of the cell."""


def _limit_table():
    """(report name, resource, wanted ceiling) for each limit the child sets."""
    import resource  # not at module level: absent on Windows

    return (
        ("cpu_s", resource.RLIMIT_CPU, int(os.environ.get("NARU_KERNEL_CPU", "30"))),
        ("mem_b", resource.RLIMIT_AS,
         int(os.environ.get("NARU_KERNEL_MEM_MB", "1024")) * 1024 * 1024),
        ("fsize_b", resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
    )


def _limits():
    """OS-enforced ceilings, and an honest report of which ones took.

    macOS refuses RLIMIT_AS outright. Swallowing that leaves a caller believing
    memory is capped when it is not, which is the failure this whole repo is
    built to avoid, so the result is returned rather than discarded.
    """
    import resource

    applied = {}
    for name, what, want in _limit_table():
        try:
            hard = resource.getrlimit(what)[1]
            cap = want if hard < 0 else min(want, hard)
            # Hard as well as soft. With the hard limit left where it was, a
            # cell raised its own soft limit back up to it and the ceiling was
            # a suggestion. A process cannot raise its hard limit again.
            resource.setrlimit(what, (cap, cap))
            applied[name] = resource.getrlimit(what)[0]
        except (ValueError, OSError, AttributeError) as e:
            applied[name] = f"NOT APPLIED: {type(e).__name__}"
    return applied


def _limits_in_force(applied):
    """What holds now, not what held at startup.

    A cell can still lower a limit, and a report cached at startup then names
    a ceiling that is no longer the one in force. A refusal stays a refusal;
    every limit that took is read again.
    """
    import resource

    return {
        name: applied[name]
        if isinstance(applied[name], str)
        else resource.getrlimit(what)[0]
        for name, what, _ in _limit_table()
    }


def _child_main():
    """The child program. One JSON request per line in, one reply per line out.

    A real function, not a string: embedded in one it was invisible to the
    tokenizer, so a typo was not a SyntaxError at import but a dead child at
    run time. It is lintable and greppable here, and `_shape`, `_cap` and
    MAX_OUT have one owner instead of a copy on each side of the pipe.
    """
    # Frame replies down a private copy of stdout, then point stdout at
    # devnull so a stray print at import time cannot corrupt the stream.
    _reply = os.fdopen(os.dup(1), "w")
    os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    limits = _limits()
    calls = []
    ns = {"__name__": "__naru__"}
    db = os.environ.pop("NARU_KERNEL_DB", None)
    if db:
        from ms import MemorySurface

        # open_readonly, not MemorySurface(db): the plain constructor opens a
        # writable connection and runs migrations against the operator's live
        # log, and ReadOnly only withholds an attribute name.
        ns["ms"] = MemorySurface.open_readonly(db).readonly()
    for name in json.loads(os.environ.get("NARU_KERNEL_CALLBACKS", "[]")):

        def stub(*a, _n=name, **k):
            # Batched, not RPC: the parent applies them with the reply, which
            # keeps this a plain request/response loop.
            if _n == "submit_answer":
                # The in-process path keeps str(text), so the same str() runs
                # here, where the object still exists.
                a, k = [str(x) for x in a], {key: str(v) for key, v in k.items()}
            # Encoded at the call, not with the reply. An argument that is not
            # JSON then fails this one call as the cell's own error, where the
            # parent's validator would have stopped the cell in-process. Left
            # to the reply, it replaced the whole reply and the answer, the
            # headlines and the printed output of that cell were all lost.
            # The fourth item is how much the cell had printed at this call.
            # If the parent rejects the call, the cell would have stopped
            # here in-process, so the parent drops what was printed after it.
            printed = cell_out[0].tell() if cell_out[0] is not None else None
            calls.append(json.loads(json.dumps([_n, a, k])) + [printed])
            if _n == "submit_answer":
                raise _Done()

        ns[name] = stub
    preloaded = set(ns)
    cell_out = [None]  # the running cell's output buffer; None during a digest

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        calls.clear()
        if msg.get("op") == "limits":
            reply = {"limits": _limits_in_force(limits)}
        elif msg.get("op") == "digest":
            # Describing a resident object can run a model method that calls a
            # callback, so the digest carries its calls back like a cell does.
            reply = {"digest": _digest(ns, preloaded), "calls": list(calls)}
        else:
            buf, err = io.StringIO(), None
            cell_out[0] = buf
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    exec(compile(msg["code"], "<cell>", "exec"), ns)
            except _Done:
                pass
            except BaseException:
                # BaseException, not Exception: sys.exit() and `raise
                # KeyboardInterrupt` in a cell must be reported as that cell's
                # error, not escape and kill the loop silently. No Ctrl-C is
                # lost by this: the child has its own session. RLIMIT_CPU is
                # not caught here. It delivers SIGXCPU, whose default action
                # ends the child, and the parent names the signal.
                tb = traceback.format_exc(limit=2).strip().splitlines()
                err = tb[-1] if tb else "error"
            cell_out[0] = None
            # capped here, before it crosses the pipe
            reply = {"out": _cap(buf.getvalue()), "err": err, "calls": list(calls)}
        # Always encodable: the stub already encoded every callback argument.
        _reply.write(json.dumps(reply) + "\n")
        _reply.flush()


# The child is `kernel._child_main()`, reached by a three-line bootstrap so a
# jail command in NARU_KERNEL_JAIL still wraps a plain `python -I -c`.
_CHILD = (
    "import os, sys\n"
    "sys.path.insert(0, os.environ['NARU_KERNEL_PATH'])\n"
    "import kernel; kernel._child_main()\n"
)


def _exit_reason(rc):
    """Name a child's exit. A negative code is a signal, and the signal is the
    diagnosis: RLIMIT_CPU does not raise, it delivers SIGXCPU, and "exit -24"
    told nobody that the CPU ceiling had done its job."""
    if rc is None:
        return "no exit status"
    if rc >= 0:
        return f"exit {rc}"
    try:
        name = signal.Signals(-rc).name
    except ValueError:
        return f"signal {-rc}"
    hint = {
        "SIGXCPU": " — the CPU limit",
        "SIGXFSZ": " — the file size limit",
        "SIGKILL": " — killed, out of memory or by the OS",
        "SIGSEGV": " — segmentation fault",
    }.get(name, "")
    return f"{name}{hint}"


def _kill_process_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()


class SandboxedKernel:
    """Kernel.run/digest, executed in a child process.

    What this buys, precisely: a runaway loop, a memory bomb or a hard crash in
    model-authored code takes down the child and not the harness, and the
    ceilings are enforced by the OS rather than by hoping the model behaves.
    A dead child is replaced, losing resident state — which is the honest cost
    of the isolation — and the first run() after any death says so.

    What it does NOT buy: the child runs as the same user, with the same
    filesystem and the same network. `rm -rf`, reading a key out of the
    environment, and an outbound request all still work. Real capability
    isolation needs seccomp or a container, neither of which is in the standard
    library, so `NARU_KERNEL_JAIL` wraps the child in one you supply:

        NARU_KERNEL_JAIL='sandbox-exec -f jail.sb'             # macOS
        NARU_KERNEL_JAIL='bwrap --ro-bind / / --unshare-net'   # Linux

    A container also needs the repo, the log, the environment and its own
    interpreter, because none of the host's paths exist inside it:

        NARU_KERNEL_JAIL="docker run --rm -i --network none \\
            -v $PWD:/naru:ro -v $(dirname $LOG):/log:ro \\
            -e NARU_KERNEL_PATH=/naru -e NARU_KERNEL_DB=/log/log.db \\
            -e NARU_KERNEL_CALLBACKS python:3.13"
        NARU_KERNEL_PYTHON=python

    Without one of those this is process isolation, not a sandbox, and calling
    it a sandbox in a README is how people get hurt.

    The Event Log is reopened by path in the child, so `db` cannot be
    `:memory:` — an in-memory database belongs to one process. That is why the
    in-process Kernel remains the default for benchmark runs.
    """

    def __init__(self, db=None, callbacks=None, timeout=60):
        if db == ":memory:":
            raise ValueError(
                "an in-memory Event Log cannot be shared with a child process; "
                "use a file path or the in-process Kernel"
            )
        self.db = db
        self.callbacks = dict(callbacks or {})
        self.timeout = timeout
        self._proc = None
        self._stderr = None
        self._why = "not started"
        # True from the moment resident state is lost until a run() has said
        # so. digest() and limits() respawn the child but cannot report.
        self._lost = False

    # -- child lifecycle ----------------------------------------------------
    def _spawn(self):
        self._stop()
        env = dict(os.environ)
        env["NARU_KERNEL_PATH"] = str(pathlib.Path(__file__).resolve().parent)
        env["NARU_KERNEL_CALLBACKS"] = json.dumps(sorted(self.callbacks))
        if self.db:
            env["NARU_KERNEL_DB"] = str(self.db)
        # NARU_KERNEL_PYTHON: a jail that supplies its own filesystem has no
        # copy of the host interpreter at sys.executable, so a container jail
        # could not work without this. sandbox-exec and bwrap share the host
        # root and need nothing.
        argv = shlex.split(os.environ.get("NARU_KERNEL_JAIL", "")) + [
            os.environ.get("NARU_KERNEL_PYTHON", sys.executable),
            # -I, isolated: without it `python -c` puts the CWD at sys.path[0],
            # which model code shares. A cell writing ./resource.py and forcing
            # a respawn gave every later child no ceilings at all while
            # limits() reported the fabricated numbers as applied — the
            # enforcement mechanism supplied by the thing it constrains.
            "-I",
            "-c",
            _CHILD,
        ]
        self._stderr = tempfile.TemporaryFile(mode="w+t")
        try:
            self._proc = subprocess.Popen(
                argv,
                # Its own process group. Under NARU_KERNEL_JAIL self._proc is the
                # jail command, and killing it leaves the python child it wrapped
                # running. The group kills both.
                start_new_session=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # A file keeps crash diagnostics without waiting for a
                # descendant that inherited stderr to close a pipe.
                stderr=self._stderr,
                env=env,
                text=True,
            )
        except Exception:
            self._stderr.close()
            self._stderr = None
            raise

    def _stderr_tail(self):
        if not self._stderr:
            return ""
        try:
            self._stderr.flush()
            self._stderr.seek(0)
            return (self._stderr.read() or "").strip()[-300:]
        except (OSError, ValueError):
            return ""

    def _stop(self):
        proc = self._proc
        if proc is None:
            return None, ""
        _kill_process_group(proc)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rc = proc.poll()
        cause = self._stderr_tail()
        if self._stderr:
            self._stderr.close()
        self._stderr = None
        self._proc = None
        return rc, cause

    def close(self):
        self._stop()

    def _reap(self):
        """Notice a child that exited between requests.

        Respawning it quietly gave the next cell an empty namespace and a
        normal reply, so the model saw plain NameErrors and no word that its
        state was gone.
        """
        if self._proc is not None and self._proc.poll() is not None:
            rc, cause = self._stop()
            self._why = (
                f"died between cells ({_exit_reason(rc)})"
                f"{': ' + cause if cause else ''}"
            )
            self._lost = True

    def _ask(self, msg):
        """One request, one reply. Returns None if the child died or hung."""
        self._reap()
        if self._proc is None:
            try:
                self._spawn()
            except (OSError, ValueError) as e:
                # A misspelled NARU_KERNEL_JAIL is an operator error, not a
                # reason to take the harness down mid-run. ValueError is the
                # same error in another form: shlex refusing an unbalanced
                # quote.
                self._why = f"cannot start the kernel: {e}"
                return None
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            # _why has to be set on EVERY path out of here, or run() prints
            # whatever the last failure left behind — "not started", typically.
            self._why = f"pipe closed ({type(e).__name__})"
            self._lost = True
            self.close()
            return None
        result = {}

        def read():
            line = self._proc.stdout.readline()
            if not line:
                return                      # EOF: the child died
            try:
                result["reply"] = json.loads(line)
            except ValueError as e:
                # Unhandled, this killed the thread and the caller read it as a
                # wall-clock hang, waiting out the full timeout for nothing.
                result["reply"] = {"out": "", "err": f"unparseable reply: {e}"}

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(self.timeout)
        if "reply" in result:
            return result["reply"]
        # Died or hung, and the two need different words: a segfault reported
        # as a timeout sends someone hunting a slow cell that never existed.
        #
        # The reader thread is the reliable signal, not poll(). On a crash the
        # thread returns immediately on EOF, often before the OS has reaped the
        # child, so poll() still says None and the death reads as a timeout.
        # The thread is the reliable signal. Alive means it is still blocked
        # on readline, so the child is hung; finished with no reply means
        # stdout hit EOF and the child is on its way out.
        hung = t.is_alive()
        rc, cause = self._stop()
        self._why = (
            f"exceeded {self.timeout}s wall clock"
            if hung
            else f"died ({_exit_reason(rc)}){': ' + cause if cause else ''}"
        )
        self._lost = True
        return None

    # -- Kernel interface ---------------------------------------------------
    def _replay(self, reply):
        """Apply the child's batched callback calls.

        Returns (first error, characters the cell had printed at that call),
        or (None, None) when every call was accepted.

        The callbacks own their validation, so the child's stubs never raise
        and the cell there ran on past a call the parent rejects. In-process
        that call would have stopped the cell, so nothing the cell did after
        it may take effect: the replay stops at the first call that raises,
        and its error is the cell's error. Replayed to the end, a cell with an
        invalid headline still recorded its answer and hid the error.
        """
        for name, args, kwargs, printed in reply.get("calls", []):
            fn = self.callbacks.get(name)
            if not fn:
                continue
            try:
                fn(*args, **kwargs)
            except Exception as e:
                # The arguments come from model-authored code. Dispatching them
                # unguarded let `headline(1,2,3,4,5)` raise a live TypeError out
                # of run() and into the harness — the one thing a child process
                # is here to prevent.
                return f"{type(e).__name__}: {e}", printed
        return None, None

    def run(self, code):
        self._reap()
        # A loss nobody has been told about is reported before anything else
        # runs. The cell is not executed: it was written against state that
        # is gone.
        reply = None if self._lost else self._ask({"op": "run", "code": code})
        if reply is None:
            self._lost = False
            return "", f"kernel {self._why}; resident state was lost"
        # The callback error first: in the cell it came before anything the
        # child reports after it.
        rejected, printed = self._replay(reply)
        out = reply.get("out", "")
        if rejected and printed is not None and printed <= len(out):
            # In-process the rejected call stops the cell, so nothing after
            # it is printed. The child ran on; cut its output at the call.
            out = out[:printed]
        # already capped in the child; belt and braces for a hand-built reply
        return _cap(out), rejected or reply.get("err")

    def digest(self):
        reply = self._ask({"op": "digest"}) or {}
        self._replay(reply)
        return reply.get("digest", "resident: (unavailable)")

    def limits(self):
        """Which ceilings are actually in force in the child.

        A value of "NOT APPLIED: ..." means the platform refused it. macOS
        refuses RLIMIT_AS, so memory is uncapped there and the caller has to
        know rather than assume.
        """
        return (self._ask({"op": "limits"}) or {}).get("limits", {})


def demo():
    k = Kernel(helper=lambda x: x * 2)

    # state persists across separate cells
    out, err = k.run("xs = [1, 2, 3]\nprint(sum(xs))")
    assert (out.strip(), err) == ("6", None), (out, err)
    out, err = k.run("xs.append(10)\nprint(len(xs))")
    assert out.strip() == "4", out

    # nothing printed => nothing enters context, even though state changed
    out, err = k.run("big = 'x' * 100000")
    assert out == "" and err is None
    assert len(k.ns["big"]) == 100000

    # digest sees the resident vars, hides preloaded callables
    d = k.digest()
    assert "xs: list[4]" in d and "big: str[100000]" in d, d
    assert "helper" not in d, d

    class SliceOnly(list):
        def __iter__(self):
            raise AssertionError("digest copied the full resident list")

    rows = SliceOnly([{"seq": 7}] * 1000)
    assert _provenance(rows) == " from seq 7"

    # SystemExit subclasses BaseException, so `except Exception` let a cell
    # calling sys.exit() unwind out of the harness mid-run.
    out, err = k.run("import sys; sys.exit(3)")
    assert err and "SystemExit" in err, (out, err)
    out, _ = k.run("print('alive')")
    assert out.strip() == "alive", "kernel did not survive sys.exit()"

    # Any exception a cell raises is that cell's error. BaseException and
    # KeyboardInterrupt each escaped run_naru and bench's `except Exception`.
    _, err = k.run("raise BaseException('stop the bench')")
    assert err and "BaseException" in err, err
    # Only the main thread can receive a real Ctrl-C, so in any other thread a
    # KeyboardInterrupt is the cell's own, and in the main thread it must
    # still stop the program.
    threaded = []
    worker = threading.Thread(
        target=lambda: threaded.append(k.run("raise KeyboardInterrupt")))
    worker.start()
    worker.join()
    assert threaded and "KeyboardInterrupt" in (threaded[0][1] or ""), threaded
    try:
        k.run("raise KeyboardInterrupt")
        raise AssertionError("Ctrl-C in the main thread was swallowed")
    except KeyboardInterrupt:
        pass

    # Describing a variable runs model code and must never raise: repr() of a
    # 5000-digit int hits the digit limit, and a model class can raise from
    # __len__. The rest of the digest is still produced.
    unreadable = (
        "n = 10**5000\n"
        "class Broken(list):\n"
        "    def __len__(self):\n"
        "        raise RuntimeError('no length')\n"
        "broken = Broken()\n"
        "after = 1"
    )
    assert k.run(unreadable) == ("", None)
    d = k.digest()
    assert "n: <unreadable: ValueError>" in d, d
    assert "broken: <unreadable: RuntimeError>" in d and "after: int=1" in d, d

    # errors are captured, not raised, and the kernel survives
    out, err = k.run("1/0")
    assert err and "ZeroDivisionError" in err, err
    out, err = k.run("print('alive')")
    assert out.strip() == "alive"

    # runaway output is capped
    out, _ = k.run("print('z' * 50000)")
    assert len(out) < MAX_OUT + 200 and "truncated" in out

    # ---- SandboxedKernel: same interface, child process ------------------
    import tempfile
    from time import monotonic, sleep

    from ms import MemorySurface

    tmp = tempfile.TemporaryDirectory()
    d = pathlib.Path(tmp.name)
    db = str(d / "log.db")
    MemorySurface(db).append(
        "user", "the kayak leaks", kind="context_msg",
        session_id="s1", created_at="2023-01-01T00:00",
    )
    got = []
    # 2s: every operation here takes under 0.2s, and one of them is a runaway
    # loop the check has to sit through.
    sk = SandboxedKernel(db=db, callbacks={"submit_answer": got.append}, timeout=2)
    try:
        # an in-memory log belongs to one process and must be refused loudly
        try:
            SandboxedKernel(db=":memory:")
            raise AssertionError("accepted an in-memory DB for a child process")
        except ValueError:
            pass

        out, err = sk.run("xs = [1, 2, 3]\nprint(sum(xs))")
        assert (out.strip(), err) == ("6", None), (out, err)
        out, _ = sk.run("xs.append(9)\nprint(len(xs))")
        assert out.strip() == "4", out          # state survives between cells
        assert "xs: list[4]" in sk.digest(), sk.digest()

        out, _ = sk.run("print(ms.search('kayak')[0]['content'][:15])")
        assert "kayak" in out, out               # the log reopened by path
        _, err = sk.run("ms.db = 1")
        assert err and "read-only" in err, err
        # The facade withholds a name; SQLite withholds the write. A cell
        # walked ms._ms.db and emptied a live log, so the child's connection
        # is opened mode=ro and the refusal comes from the database.
        _, err = sk.run("ms._ms.db.execute('DELETE FROM conversation_history')")
        assert err and "readonly database" in err, err
        out, _ = sk.run("print(len(ms.search('kayak')))")
        assert out.strip() == "1", out           # and reads still work

        # -I keeps the CWD off the child's sys.path. Without it a cell writes
        # ./resource.py, forces a respawn, and every later child reports
        # fabricated ceilings as applied — the limits supplied by the thing
        # they constrain. Run from a scratch directory, because the child adds
        # the repo to sys.path itself and the repo is often the CWD.
        here = os.getcwd()
        os.chdir(d)
        try:
            poison = SandboxedKernel(timeout=2)
            honest = poison.limits()
            poison.run(
                "open('resource.py','w').write("
                "'def setrlimit(*a):\\n    pass\\n"
                "def getrlimit(w):\\n    return (1, 1)\\n"
                "RLIMIT_CPU=0\\nRLIMIT_AS=1\\nRLIMIT_FSIZE=2\\n')"
            )
            poison.run("while True: pass")      # force a respawn
            assert poison.limits() == honest, (honest, poison.limits())
            poison.close()
        finally:
            os.chdir(here)

        out, _ = sk.run("submit_answer('42')\nprint('NOT REACHED')")
        assert got == ["42"], got                # callback crossed back
        assert "NOT REACHED" not in out, out     # and stopped the rest of it

        # Callback arguments come from model code. Dispatched unguarded in the
        # parent, one bad call raised a live TypeError out of run().
        bad = SandboxedKernel(callbacks={"headline": lambda task=None: task}, timeout=2)
        try:
            _, err = bad.run("headline(1,2,3,4,5)")
            assert err and "TypeError" in err, err
            out, _ = bad.run("print('alive')")
            assert out.strip() == "alive", "a bad callback arg took the parent"
        finally:
            bad.close()

        # The callbacks own their validation, so the child's stub cannot stop
        # the cell where the in-process callback would have. The parent stops
        # replaying at the first call that raises: nothing after it takes
        # effect, and its error is the cell's error.
        seen = []

        def strict(task=None):
            if not isinstance(task, str):
                raise ValueError("headline needs task")
            seen.append(task)

        pair = SandboxedKernel(
            callbacks={"headline": strict, "submit_answer": seen.append}, timeout=2
        )
        try:
            _, err = pair.run("headline(task=1)\nsubmit_answer('A')")
            assert seen == [] and err == "ValueError: headline needs task", (seen, err)
            # In-process the cell stops at the rejected call, so what the
            # child printed after it must not reach the observation.
            out, err = pair.run("print('before')\nheadline(task=1)\nprint('after')")
            assert out == "before\n" and err == "ValueError: headline needs task", (out, err)
            out, err = pair.run("print('a')\nheadline(task='ok')\nprint('b')")
            assert out == "a\nb\n" and err is None, (out, err)
            seen.clear()
            # The rejected call came first in the cell, so it is the error,
            # not whatever the child ran into after it.
            _, err = pair.run("headline(task=1)\n1/0")
            assert err == "ValueError: headline needs task", err
            # A callback argument that is not JSON used to replace the whole
            # reply. submit_answer keeps str(), as the in-process path does,
            # and the printed output survives.
            out, err = pair.run("print('kept')\nsubmit_answer(object())")
            assert out.strip() == "kept" and err is None, (out, err)
            assert len(seen) == 1 and seen[0].startswith("<object object"), seen
            # Any other non-JSON argument fails its own call and stops the
            # cell there, as the parent's validator would have in-process.
            out, err = pair.run(
                "print('kept')\nheadline(task=object())\nsubmit_answer('B')")
            assert out.strip() == "kept" and "TypeError" in (err or ""), (out, err)
            assert "B" not in seen, seen
            # A resident object can call a callback while the digest describes
            # it. The call crosses back like one made by a cell.
            pair.run(
                "class Loud(list):\n"
                "    def __getitem__(self, i):\n"
                "        submit_answer('from digest')\n"
                "loud = Loud([1])"
            )
            assert "loud: <unreadable: _Done>" in pair.digest()
            assert seen[-1] == "from digest", seen
        finally:
            pair.close()

        # A jail command shlex cannot split is an operator error like a
        # misspelled one, and must not raise out of run() or digest().
        os.environ["NARU_KERNEL_JAIL"] = "sandbox-exec -f 'unbalanced"
        try:
            jailed = SandboxedKernel(timeout=2)
            _, err = jailed.run("print(1)")
            assert err and "cannot start the kernel" in err, err
            assert jailed.digest() == "resident: (unavailable)"
        finally:
            os.environ.pop("NARU_KERNEL_JAIL", None)

        # The same guards hold in the child: neither cell may kill it.
        _, err = sk.run("raise KeyboardInterrupt")
        assert err and "KeyboardInterrupt" in err, err
        assert sk.run(unreadable) == ("", None)
        d = sk.digest()
        assert "n: <unreadable: ValueError>" in d and "after: int=1" in d, d
        assert "xs: list[4]" in d, f"the child lost its state: {d}"

        # The log path must not stay in the child's environment: model code
        # reading it can reopen the same file writable and undo mode=ro.
        out, _ = sk.run("import os; print(os.environ.get('NARU_KERNEL_DB'))")
        assert out.strip() == "None", out

        # Containment. Each of these used to take the harness down with it.
        _, err = sk.run("while True: pass")
        assert err and "wall clock" in err, err
        out, _ = sk.run("print('alive')")
        assert out.strip() == "alive", "parent did not survive a runaway loop"
        # Capped in the CHILD: the parent must never pull the whole payload
        # through the pipe just to truncate it afterwards.
        out, _ = sk.run("print('z' * 5_000_000)")
        assert len(out) < MAX_OUT + 200 and "truncated" in out, len(out)

        _, err = sk.run("import os; os._exit(86)")
        assert err and "died" in err, err
        out, _ = sk.run("print('alive')")
        assert out.strip() == "alive", "parent did not survive a child exit"

        # A child that exits BETWEEN cells was respawned in silence and the
        # next cell got plain NameErrors. The first run() after the death must
        # report the loss, and only the one after it uses the fresh child.
        dies_later = (
            "import os, threading\nz = 5\n"
            "threading.Timer(0.2, os._exit, [1]).start()"
        )
        for between in (lambda: None, sk.digest):
            assert sk.run(dies_later) == ("", None)
            sleep(0.6)
            between()        # a digest respawns the child; it must not hide it
            out, err = sk.run("print(z)")
            assert err and "died between cells" in err and "lost" in err, (out, err)
            _, err = sk.run("print(z)")
            assert err and "NameError" in err, err   # reported once, then fresh

        # A ceiling the platform refused must say so. macOS rejects RLIMIT_AS,
        # and reporting it as applied is the failure this repo is built around.
        lim = sk.limits()
        assert set(lim) == {"cpu_s", "mem_b", "fsize_b"}, lim
        assert lim["cpu_s"] == 30, lim
        # A cell must not be able to lift a ceiling: the hard limit is set
        # with the soft one, so raising it back is refused.
        _, err = sk.run(
            "import resource\n"
            "hard = resource.getrlimit(resource.RLIMIT_CPU)[1]\n"
            "resource.setrlimit(resource.RLIMIT_CPU, (60, hard))"
        )
        assert err and sk.limits()["cpu_s"] == 30, (err, sk.limits())
        # It can still lower one, and limits() must read what is in force now
        # and not repeat what it measured at startup.
        _, err = sk.run("resource.setrlimit(resource.RLIMIT_CPU, (20, 20))")
        assert err is None and sk.limits()["cpu_s"] == 20, (err, sk.limits())
    finally:
        sk.close()

    descendant = SandboxedKernel(timeout=1)
    try:
        descendant._spawn()
        assert descendant._proc.stderr is None and descendant._stderr.seekable(), (
            "sandbox stderr must use a regular temporary file"
        )
        descendant.close()
        started = monotonic()
        _, err = descendant.run(
            "import os, subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(3)'], stdout=subprocess.DEVNULL)\n"
            "os._exit(0)"
        )
        elapsed = monotonic() - started
        assert err and "died" in err, err
        assert elapsed < 2.5, f"descendant stderr held the parent for {elapsed:.2f}s"
    finally:
        descendant.close()

    # Whether the report is TRUE, not merely well-formed. "isinstance(x, int)
    # or 'NOT APPLIED' in str(x)" accepts both branches and is a tautology, so
    # it passed while the child reported a refused limit as applied. Ask the
    # child to exceed the cap it claims to have instead.
    os.environ["NARU_KERNEL_MEM_MB"] = "128"
    probe = SandboxedKernel(timeout=10)
    try:
        mem = probe.limits()["mem_b"]
        _, err = probe.run("b = bytearray(160_000_000); print(len(b))")
        if isinstance(mem, int):
            assert err, f"mem_b reported as {mem} but 160MB allocated anyway"
        else:
            assert "NOT APPLIED" in str(mem), mem
            assert not err, f"mem_b unenforced yet the allocation failed: {err}"
    finally:
        probe.close()
        os.environ.pop("NARU_KERNEL_MEM_MB", None)

    print("ok — kernel checks passed (in-process and sandboxed)")


if __name__ == "__main__":
    demo()
