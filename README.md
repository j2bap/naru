# Naru

**Shared memory for your coding agents. You decide what stays.**

[![Checks](https://github.com/j2bap/naru/actions/workflows/checks.yml/badge.svg)](https://github.com/j2bap/naru/actions/workflows/checks.yml)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue)](#-get-started)
[![MIT license](https://img.shields.io/badge/License-MIT-green)](LICENSE)

Your agent learns a useful tool quirk. The next session should remember it.
With Naru, agents propose facts, you review them, and approved facts go into
the instruction file your agent already reads.

Works with **Claude Code, Codex, Cursor**, and tools that can run shell commands.
Runs locally with Python's standard library. No API key is needed for the memory commands.

```text
Agent proposes → You review → Approved memory → Your next session
```

- 🧠 **Keep useful facts.** Share approved memory across agent sessions.
- 📬 **Stay in control.** Review claims before they enter the shared document.
- 📁 **Keep your files.** Naru updates its marked block and keeps the rest.

[Get started](#-get-started) · [Connect your agent](docs/usage.md#connect-your-agent) · [Commands](docs/usage.md#commands) · [Research](docs/research.md)

## 🚀 Get started

You need **Python 3.9+**, Git, and SQLite with FTS5. Most Python builds include FTS5.

```bash
git clone https://github.com/j2bap/naru.git
cd naru
python3 ms.py
mkdir -p ~/.local/bin
ln -s "$PWD/naru.py" ~/.local/bin/naru
export PATH="$HOME/.local/bin:$PATH"
naru --help
```

The store check prints `ok` and uses temporary files. If it reports an FTS5
error, use a Python build with SQLite FTS5 support. Add the `PATH` line to your
shell profile to keep the command available in new terminals. If `naru` is
already installed, check its path before replacing the symlink.

### Try it in a temporary store

Run these commands in a new terminal. They keep the example out of your real memory.

```bash
naru_demo=$(mktemp -d)
export NARU_DB="$naru_demo/log.db"
cd "$naru_demo"

naru claim "On macOS, use gdate for GNU date flags" --key shell.date --by you
naru inbox
```

Type `p` to approve the claim. Then write it to a sample instruction file:

```bash
naru inject AGENTS.md
cat AGENTS.md
unset NARU_DB
```

You now have an approved fact between `<!-- naru:begin -->` and
`<!-- naru:end -->`. Close this terminal when you finish the demo.
Your normal store is `~/.naru/log.db`.

## 🔌 Connect your agent

Choose the tool you use. The guide includes the exact setup and configuration.

| Your tool | Setup |
| --- | --- |
| Claude Code | [Install the memory hooks](docs/usage.md#claude-code) |
| Codex | [Install the Codex plugin](docs/usage.md#codex) |
| Cursor or another agent | [Write memory to its instruction file](docs/usage.md#cursor-and-other-tools) |

Hooks load approved memory and tell agents how to propose new facts.
For a file-only setup, add the [claim instructions](docs/usage.md#tell-your-agent-to-file-claims).
If you commit an instruction file, its injected memory is public too.

## 📬 Daily use

```bash
naru inbox                         # review proposed facts
naru search "date flags"           # find a saved entry
naru inject AGENTS.md               # write approved memory to a file
naru stats                         # inspect spill and recovery counts
```

After you register a file with `inject`, later approvals and retirements keep
its memory block current. Use `--repo` for a fact that applies to one repository;
the hooks deliver those facts only in that repository.

The [user guide](docs/usage.md) covers conflicts, stale facts, archive and reject,
large tool output, and the full command list.

## 📖 Learn more

| You want to… | Read |
| --- | --- |
| Set up and use Naru | [User guide](docs/usage.md) |
| See the memory flow | [Visual walkthrough](how-it-works.html) |
| Understand the architecture and limits | [Architecture and research](docs/research.md) |
| Check the measurements | [Benchmark method](docs/benchmark.md) and [published rows](results/published/) |
| Use the optional context gate | [Context policy and Jev](docs/context-policy.md) |
| Contribute a change | [Contributing](CONTRIBUTING.md) |

Naru implements [Context as an Environment](https://arxiv.org/abs/2608.21690)
and adds human review of shared memory. The benchmark measures the research
agent loop, not the value of approved facts in everyday coding. Its judge
differs from LongMemEval's official judge; the scores are not leaderboard results.

The research loop executes model-written Python. Read its
[limits](docs/research.md#limitations) before you run it. Normal memory commands
do not run model-written code. Staleness checks that you attach to claims do run
the specified commands when you review them.

## License

[MIT](LICENSE)
