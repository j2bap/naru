# 7. The kernel can run in a child process, and that is not a sandbox

`NARU_KERNEL=sandbox` runs model-authored cells in a child process with
OS-enforced limits. It is opt-in, it is process isolation, and calling it a
sandbox without a jail wrapper would be a lie.

## Context

`kernel.py` executed model-authored Python with `exec` in the harness process.
A runaway loop hung the whole run, a memory bomb took the harness with it, and
a segfault in a C extension killed everything. The README disclosed this and it
was the single thing most likely to stop a stranger running the project.

## Decision

`SandboxedKernel` keeps `Kernel`'s interface — `run(code)`, `digest()`,
persistent namespace — and executes cells in a child `python3` process speaking
one JSON request and one JSON reply per line.

- `RLIMIT_CPU` (30s), `RLIMIT_AS` (1GB), `RLIMIT_FSIZE` (64MB), each
  configurable, plus a parent-side wall-clock timeout for hangs no CPU limit
  catches: `sleep`, a blocking read, a socket. The child sets the hard limit
  equal to the soft limit. With the hard limit left alone, a cell raised its
  own soft limit back up to it.
- The Event Log is reopened **by path** in the child through
  `MemorySurface.open_readonly()`, a `mode=ro` URI connection. The first
  version used the plain constructor and a `ReadOnly` facade; that facade
  withholds an attribute name and nothing else, and a cell reached the live log
  through `ms._ms.db.execute("DELETE FROM conversation_history")` and emptied
  it. The refusal now comes from SQLite, which no attribute walk gets around.
- The child runs with `-I`. Without it `python -c` puts the working directory
  at `sys.path[0]`, and model code shares that directory: one cell writing
  `./resource.py` and one forcing a respawn left every later child with no
  ceilings at all while `limits()` reported the fabricated numbers as applied.
  The enforcement mechanism was supplied by the thing it constrains.
- Output is capped **in the child**, before it crosses the pipe. Capping only
  in the parent still required `readline()` to pull the whole line in first: a
  cell printing 200MB took the parent from 19MB to 687MB of RSS, which is the
  memory bomb this design claims the child absorbs.
- `submit_answer` and `headline` become stubs that batch their calls into the
  reply; the parent applies them. That keeps the protocol request/response
  rather than duplex RPC. See "Callbacks are replayed, not called" below for
  what that costs.
- A dead child is replaced and resident state is lost. That is the honest cost
  of isolation, and `run()` says which happened — a segfault reported as a
  timeout sends someone hunting a slow cell that never existed. A child that
  dies between cells is reported the same way: the first `run()` after the
  death returns the error and does not execute its cell, even when a
  `digest()` or `limits()` call has already started a new child. Before that,
  the next cell ran in an empty namespace and the model saw plain NameErrors.

## Callbacks are replayed, not called

The parent owns the callbacks and their validation. The child's stub records
the call and returns, so the cell runs on past a call that the parent later
rejects. Three rules keep the two kernels in agreement on the same cell:

- The parent stops replaying at the first callback that raises. That error is
  the cell's error, and no later call from that cell takes effect. Replayed to
  the end, a cell with an invalid `headline` still recorded its answer.
- The stub encodes its arguments as JSON at the call. An argument that is not
  JSON fails that call inside the cell with a `TypeError`. Encoded with the
  reply, one such argument replaced the whole reply and lost the cell's output.
- The `submit_answer` stub applies `str()` to its argument first, because the
  in-process path keeps `str(text)`.

Each batched call also carries how many characters the cell had printed
when it made the call. When the parent rejects a call, it cuts the cell's
output at that point, so the observation matches the in-process kernel. The
cell's other side effects after the rejected call, such as a variable it
assigned, remain in the child.

The digest follows the same path. Describing a resident object can run a model
method that calls a callback, so the digest reply carries its calls and the
parent replays them.

## Why opt-in rather than default

`bench.py` ingests each question into `MemorySurface(":memory:")`. An in-memory
database belongs to one process, so no child can open it. Making the sandbox
the default would mean file-backed databases for every benchmark question, for
a threat model — our own dataset, our own machine — that is empty.

The constructor refuses `":memory:"` loudly rather than failing later.

## What it does not do

The child runs as the same user with the same filesystem and the same network.
`rm -rf`, reading a key out of the environment, and an outbound request all
still work. Real capability isolation needs seccomp or a container, neither of
which is in the standard library, so `NARU_KERNEL_JAIL` wraps the child in one
the operator supplies:

```
NARU_KERNEL_JAIL='sandbox-exec -f jail.sb'              # macOS
NARU_KERNEL_JAIL='bwrap --ro-bind / / --unshare-net'    # Linux
```

Those two share the host filesystem, so the child finds the repo, the log and
the interpreter where the parent left them. A container finds none of them, and
the first version of this ADR offered a `docker run …` line that could not have
worked: the argv ends in the host's `sys.executable`, a path that does not
exist inside the image. `NARU_KERNEL_PYTHON` overrides it, and the jail has to
carry the rest in:

```
NARU_KERNEL_JAIL="docker run --rm -i --network none \
    -v $PWD:/naru:ro -v $(dirname $LOG):/log:ro \
    -e NARU_KERNEL_PATH=/naru -e NARU_KERNEL_DB=/log/log.db \
    -e NARU_KERNEL_CALLBACKS python:3.13"
NARU_KERNEL_PYTHON=python
```

Same seam as `NARU_BACKEND`, and for the same reason: this repo does not ship a
per-platform jail, it ships the place to put one.

## A limit the platform refuses must say so

macOS rejects `RLIMIT_AS` outright with `ValueError`. The first version caught
that and moved on, which left a caller believing memory was capped when it was
not — ADR 0002's mistake in a new place. `limits()` now reports
`"NOT APPLIED: ValueError"` per ceiling. For a limit that took, `limits()` reads
the value in force at the time of the call, because a cell can still lower it.

The self-check for it was worse than useless for one round:

```python
assert isinstance(lim["mem_b"], int) or "NOT APPLIED" in str(lim["mem_b"])
```

That accepts both branches. It passed while the child reported a refused limit
as applied, and `test_mutations.py` caught it. The check now asks the child to
exceed the cap it claims to have.

## What the in-process kernel still cannot do

`ReadOnly` is a convention there, not a boundary, and it cannot become one.
Model code under `Kernel` shares the interpreter and owns every object in it,
so `ms._ms.db` is reachable and writable by construction. `ms.py` says so now
instead of claiming the attribute is withheld for safety.

That is an argument for the sandboxed kernel, not against the facade: the same
expression against a child's connection raises `attempt to write a readonly
database`.

## Consequences

On macOS memory is **not** capped. CPU time and file size are. The wall-clock
timeout and process isolation work everywhere, and those cover the failure that
actually happens: a cell that never returns.
