# gitrisk-mcp

**Ask your AI agent where the risk is in a codebase, and who knows the code.**

<!-- mcp-name: io.github.0xmortuex/gitrisk-mcp -->

gitrisk-mcp is a read-only [MCP](https://modelcontextprotocol.io) server that mines a git
repository's **history**. Generic git servers give an agent `status`, `diff` and `commit`.
This one answers the questions you ask before touching old code: which files are the
riskiest, who actually understands them, which files secretly change together, and which
directories would be orphaned if one person left.

**How it differs from the existing git-analytics MCP servers**
(for example [repo-pulse-mcp](https://github.com/hasanyelaldi/repo-pulse-mcp),
[mcp-git-historian](https://github.com/AleBrito124356/mcp-git-historian),
[GitIntel-MCP-Server](https://github.com/hoangsonww/GitIntel-MCP-Server) and
[andriilazor/git-insights-mcp](https://github.com/andriilazor/git-insights-mcp)):

- **Expertise is measured on surviving code, not just commit counts.** `file_experts`
  runs `git blame -w -M` and blends the surviving-line share with recent activity, then
  flags authors who have gone quiet. Commit counting alone rewards whoever reformatted a file.
- **History noise is handled, not ignored.** Mass commits (reformat, vendor drops) are
  filtered, lock files are excluded, `.mailmap` is honoured everywhere (including blame),
  merge commits are not double counted, and renames are followed so a moved file keeps its past.
- **Every number is reproducible.** Each formula is documented, and the test suite builds
  real repositories with scripted commits and asserts the exact scores, confidences and
  shares. Read-only is tested too: the whole repository is hashed byte for byte before and
  after every tool.
- **Safe and honest on awkward repos:** a `--dir` sandbox, shallow clones are detected and
  warned about, bare repositories work, non-UTF-8 path names do not crash anything, and
  huge histories are streamed with explicit limits (a generated 20,000 commit repository
  runs every tool in about a second).
- Zero dependencies beyond `mcp`. It shells out to your real `git`, with argument lists only.

## Tools

| Tool | Answers |
|---|---|
| `repo_summary` | Age, commits, merges, authors, languages by churn, branches, tags. Warns on shallow clones. |
| `hotspots` | Which files are risky: recency-weighted churn times author breadth, over a time window. |
| `file_experts` | Who knows this file or directory (blame + recent commits), the bus factor, and who is inactive. |
| `change_coupling` | Which files change together with this one, with support and confidence. |
| `bus_factor` | Per directory: how many people hold more than half the knowledge. |
| `churn_timeline` | Commits and changed lines per week or month, with an ASCII sparkline. |
| `release_notes` | Markdown notes between two refs, grouped by conventional-commit type or by area. |
| `commit_search` | Find commits by message, author, path, dates, or text added/removed. |
| `file_history` | A file's commits, following it across renames. |

## What it looks like

Real output from the test repository (a scripted three-author history):

```
> hotspots
| # | file | score | commits | lines | authors | last change |
|---|---|---|---|---|---|---|
| 1 | core/engine.py | 40.8 | 6 | 22 | 3 | 2026-02-24 |
| 2 | api/routes.py | 16.0 | 2 | 10 | 2 | 2026-02-16 |
| 3 | core/util.py | 11.6 | 4 | 8 | 2 | 2026-02-23 |

> file_experts core
Experts for core: 30 surviving lines in 2 file(s) (3 authors). Bus factor 1. BUS FACTOR 1: a single person holds most of this code.
| author | email | surviving lines | blame share | commits (180d) | recent share | score | last touch |
| Alice | alice@x.io | 19 | 63% | 3 | 63% | 0.633 | 2026-02-23 |
| Bob   | bob@x.io   | 10 | 33% | 2 | 33% | 0.333 | 2026-02-02 |

> change_coupling core/engine.py
| file | support | confidence | reverse | file commits |
| core/util.py | 4 | 0.67 | 1.00 | 4 |
```

## Install

Needs Python 3.10+ and `git` on your PATH.

**Claude Code**

```
claude mcp add gitrisk -- uvx gitrisk-mcp --dir /path/to/your/projects
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "gitrisk": {
      "command": "uvx",
      "args": ["gitrisk-mcp", "--dir", "C:\\Users\\you\\projects"]
    }
  }
}
```

`--dir` is repeatable. Without it, the server may only read the repository containing its
working directory. Every tool takes `repo=` (a path under an allowed directory; the first
`--dir` is the default).

## Try asking

- "Give me a summary of this repo, then show the 15 riskiest files from the last 6 months."
- "Who should review changes to `src/billing/`? Is anyone the only person who knows it?"
- "Which directories have a bus factor of 1 and at least 10 commits?"
- "What files usually change together with `src/auth/session.py`? Anything surprising?"
- "Draft release notes from the last tag to HEAD, grouped by area."
- "Chart commit activity per week for the `api/` folder over the last year."
- "Find commits that added or removed the string `legacy_flag`, and show who made them."
- "Show the history of `lib/parser.py` including before it was renamed."

## How the numbers work

- **Hotspot score** of file F = `sum over its commits of (lines changed x 0.5^(age/half_life))`
  `x (1 + log2(distinct authors))`. Default half-life 90 days. Binary changes count as 1 line.
  Only files that still exist are ranked; commits touching more than 100 files are skipped.
- **Expert score** = `0.6 x blame share + 0.4 x recent share` (recent = lines changed in the
  last 180 days). **Bus factor** = the fewest authors whose combined share is *more than* 50%.
- **Change coupling**: `confidence = commits touching both / commits touching this file`,
  `reverse = commits touching both / commits touching the other file`. Commits touching more
  than 50 files are ignored.
- Timelines use UTC; weeks start on Monday.

## Limits and honest caveats

- `numstat` makes git read file contents, so very large diffs are slow in git itself. Measured
  on a real 2,200-commit Electron project with big bundled files, one full-history pass took
  about 6 seconds; on a generated 20,000-commit repository it takes under a second. Scans are
  capped (`max_commits`) and say so when they stop early; `since=` narrows them.
- Shallow clones understate everything. The server tells you, in every answer.
- Non-UTF-8 file names are shown with `\xNN` escapes and are skipped by blame-based tools
  (they cannot be passed through argv portably).
- Identity is the (mailmapped) author email. Without a `.mailmap`, one person with two
  addresses counts as two.
- Bus factor from history is a signal, not a verdict: it cannot see pairing, reviews or docs.

## Security

- **Read-only.** Only `log`, `blame`, `rev-parse`, `rev-list`, `ls-tree`, `diff`,
  `for-each-ref`, `describe` and `hash-object` (without `-w`) are ever run. `GIT_OPTIONAL_LOCKS=0`
  stops even stat refreshes from touching the index. Tests hash the repo before and after.
- **No shell.** Commands are argument lists. Refs are resolved to commit ids first and refs
  starting with `-` are rejected, so a ref can not become an option. Search text is passed
  literally, never as a regex. Paths are literal (`GIT_LITERAL_PATHSPECS=1`).
- **Sandbox.** Repositories must live under a `--dir` root; a repository that merely
  contains an allowed directory is refused. Inherited `GIT_*` environment variables are dropped.
- Git still honours the repo's own configuration. Only point `--dir` at repositories you trust.

## Companion tutorial

[gitrisk-mcp-the-hard-way](https://github.com/0xmortuex/gitrisk-mcp-the-hard-way) builds a
minimal version of this server from scratch in seven modules.

## Development

```
uv sync --extra test
uv run pytest tests            # builds real repos; needs git
uv run pytest tests -m slow    # 20,000-commit repo (and local real repos if present)
```

MIT licence.
