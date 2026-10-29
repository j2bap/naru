# Naru

**Useful memory for your coding agents. You decide what stays.**

Save the tool quirks and working rules you want your next agent session to
remember. Agents propose facts. You review them. Naru puts the facts you
approve into a shared document that your agents can read.

- 🧠 **Share what you learn** across sessions and agents.
- 📬 **Review what stays** with a small, explicit inbox.
- 📁 **Keep it local** in SQLite and the instruction files you already use.

Works with Claude Code, Codex, Cursor, and other tools that can run a shell
command. The memory commands use Python's standard library and need no API key.

[Install](#install) · [Try it](#try-it-in-a-temporary-store) · [Connect your agent](#connect-your-agent) · [Guides](#guides)

## Install

You need **Python 3.9 or later**, Git, and Python's SQLite module with FTS5
search support. These commands use macOS or Linux shell syntax.

```bash
git clone https://github.com/j2bap/naru.git
cd naru
python3 ms.py

mkdir -p "$HOME/.local/bin"
ln -s "$PWD/naru.py" "$HOME/.local/bin/naru"
export PATH="$HOME/.local/bin:$PATH"
naru --help
```

The store check uses temporary files and ends with `ok`. Add the `export PATH`
line to your shell profile to keep `naru` available in new terminals. Keep the
checkout where it is: the symlink points to its `naru.py` file.

If a step fails, see [installation help](docs/setup.md#installation-help).

## Try it in a temporary store

Paste this whole block into your terminal. The parentheses keep the settings
inside the demo. Your normal memory stays unchanged.

```bash
(
  naru_demo=$(mktemp -d)
  export NARU_DB="$naru_demo/log.db"
  unset NARU_NOTES

  naru claim "The Naru demo phrase is: memory works." --key demo.phrase --by you
  naru promote 1 --yes
  naru inject "$naru_demo/AGENTS.md"
  cat "$naru_demo/AGENTS.md"

  printf '\nDemo files: %s\n' "$naru_demo"
)
```

You should see `seq 1 promoted`, then a document with the demo phrase between
`<!-- naru:begin -->` and `<!-- naru:end -->`. The files remain in the printed
temporary directory for you to inspect.

The demo uses `--yes` to approve its one test claim. For everyday review, run
`naru inbox` and choose **promote**, **archive**, **reject**, or **skip**.

## Connect your agent

| You use | Start here |
| --- | --- |
| Claude Code | [Set up the memory hooks](docs/setup.md#claude-code) |
| Codex | [Install the bundled plugin](docs/setup.md#codex) |
| Cursor | [Write memory to your project rules](docs/setup.md#cursor) |
| Another agent | [Use its instruction file](docs/setup.md#other-agents) |

Hooks load your approved memory and tell the agent how to file claims. With a
file setup, Naru updates only its marked block and keeps your other text.
If you commit that file, you also publish the memory in it.

## Use it each day

```bash
naru inbox                  # review proposed facts and stale checks
naru search "tool quirk"     # find a saved entry
naru inject                 # read your approved memory
```

Once you register an instruction file with `naru inject PATH`, later decisions
update its block. Hooks deliver changes at the next prompt or session event.
Facts with `--repo` reach only the matching repository through hooks.

Your default store is `~/.naru/log.db`. Naru keeps the log locally; it does not
sync memory between machines.

## Guides

| You want to… | Read |
| --- | --- |
| Install hooks, connect an agent, or fix setup | [Agent setup](docs/setup.md) |
| Review, find, update, and retire facts | [Daily use](docs/usage.md) |
| Look up a command, option, or setting | [Command reference](docs/commands.md) |
| See the memory flow | [Visual walkthrough](how-it-works.html) |
| Understand the design and its limits | [Architecture and research](docs/research.md) |
| Inspect or repeat the measurements | [Benchmark guide](docs/benchmark.md) and [published results](results/published/README.md) |
| Try the research loop's optional context policy | [Context policy and Jev](docs/context-policy.md) |
| Change the code or docs | [Contributing](CONTRIBUTING.md) |

## Research

Naru also includes an agent loop that implements
[Context as an Environment](https://arxiv.org/abs/2608.21690). Its benchmark
measures how a model retrieves history. It does not measure the value of
approved facts in daily coding, and its judge differs from LongMemEval's
official judge. See the [results and caveats](docs/research.md#reference-results).

The research loop runs model-written Python. Read its
[execution limits](docs/research.md#execution-limits) before you run it.
Normal memory commands do not run model-written code. If you attach a
[stale check](docs/usage.md#check-for-stale-facts), Naru runs the command you
specify.

## License

[MIT](LICENSE)
