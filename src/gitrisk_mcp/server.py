"""gitrisk-mcp: where is the risk in this repository, and who knows this code?"""

from __future__ import annotations

import argparse
import functools
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import analysis as an
from . import render as rd
from .git import GitError, Repo, Sandbox

mcp = MCPServer(
    "gitrisk",
    instructions=(
        "Read-only analytics over a git repository's HISTORY, for questions generic git tools "
        "cannot answer: where is the risk and who knows this code. repo_summary first for the "
        "lay of the land; hotspots (churn x recency x authors) for risky files; file_experts "
        "(blame + recent commits) for who to ask; bus_factor per directory; change_coupling "
        "for files that change together; churn_timeline for activity over time; release_notes "
        "between two refs; commit_search and file_history (follows renames) to dig in. Every "
        "tool takes repo= (a path inside an allowed directory; defaults to the first one). "
        "Paths are relative to the repo root with forward slashes. Nothing here modifies the repo."
    ),
)

_sandbox: Sandbox | None = None
F = TypeVar("F", bound=Callable[..., Any])


def set_sandbox(roots: list[str]) -> None:
    global _sandbox
    _sandbox = Sandbox(roots)


def _open(repo: str | None) -> Repo:
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox([])
    return _sandbox.open(repo)


def tool(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except GitError as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn


@tool
def repo_summary(repo: str | None = None, ref: str = "HEAD", max_commits: int = 10000) -> str:
    """Overview of a repository: age, commit and merge counts, top authors (mailmap applied),
    languages by churn, branches, tags. Warns if the clone is shallow. Start here."""
    r = _open(repo)
    return rd.finish(rd.summary(an.repo_summary(r, ref, max_commits)), r.warnings())


@tool
def hotspots(
    repo: str | None = None, since: str = "12 months ago", path: str = "", limit: int = 20,
    half_life_days: float = 90.0, max_files_per_commit: int = 100, exclude: list[str] | None = None,
    ref: str = "HEAD", until: str | None = None, as_of: str | None = None,
    max_commits: int = 20000,
) -> str:
    """Rank files by risk: recency-weighted churn (lines added+deleted, halving every
    half_life_days) multiplied by (1 + log2 of distinct authors). Only files that still exist
    are listed. Commits touching more than max_files_per_commit files (reformats, imports) and
    lock files are ignored; exclude takes extra globs like '*.min.js'. since/until accept git
    dates ('6 months ago', '2026-01-01'); path limits to a directory; as_of pins 'now'."""
    r = _open(repo)
    res = an.hotspots(r, ref, since, until, path, limit, half_life_days, max_files_per_commit,
                      exclude, as_of, max_commits)
    return rd.finish(rd.hotspots(res), r.warnings())


@tool
def file_experts(
    path: str, repo: str | None = None, ref: str = "HEAD", recent_days: int = 180,
    inactive_days: int = 365, max_authors: int = 10, max_files: int = 200,
    as_of: str | None = None,
) -> str:
    """Who knows this file or directory? Weights each author by surviving lines (git blame
    ignoring whitespace and moved lines) and by lines changed in the last recent_days
    (score = 0.6 blame share + 0.4 recent share). Reports the bus factor (fewest authors
    holding more than 50% of the code) and flags authors with no activity for inactive_days.
    For a directory, the max_files biggest text files are blamed."""
    r = _open(repo)
    res = an.file_experts(r, path, ref, recent_days, inactive_days, as_of, max_files, max_authors)
    return rd.finish(rd.experts(res), r.warnings())


@tool
def change_coupling(
    path: str, repo: str | None = None, since: str | None = None, min_support: int = 2,
    min_confidence: float = 0.2, max_files_per_commit: int = 50, limit: int = 20,
    ref: str = "HEAD", max_commits: int = 20000,
) -> str:
    """Which files change together with this file? For each other file: support (commits
    touching both), confidence (support / commits touching this file) and reverse confidence.
    Mass commits (more than max_files_per_commit files) are ignored and renames are followed.
    High confidence between files with no import relation is a hidden dependency."""
    r = _open(repo)
    res = an.change_coupling(r, path, ref, since, min_support, min_confidence,
                             max_files_per_commit, limit, max_commits)
    return rd.finish(rd.coupling(res), r.warnings())


@tool
def bus_factor(
    repo: str | None = None, path: str = "", depth: int = 1, since: str | None = None,
    half_life_days: float = 365.0, min_commits: int = 3, limit: int = 30, ref: str = "HEAD",
    as_of: str | None = None, max_commits: int = 20000,
) -> str:
    """Bus factor per directory (first `depth` path components), from history: the fewest
    authors whose recency-weighted changed lines exceed 50%. Directories with factor 1 and at
    least min_commits commits are marked AT RISK. Use file_experts for blame-based detail."""
    r = _open(repo)
    res = an.bus_factor(r, ref, path, depth, since, half_life_days, min_commits,
                        100, limit, as_of, max_commits)
    return rd.finish(rd.bus_factor(res), r.warnings())


@tool
def churn_timeline(
    repo: str | None = None, period: str = "month", since: str | None = None, path: str = "",
    author: str | None = None, ref: str = "HEAD", max_commits: int = 20000,
) -> str:
    """Commits and changed lines per week or month (UTC), with ASCII sparklines
    ('_' = none, '@' = peak). Filter by path or author substring. Merges are excluded."""
    r = _open(repo)
    return rd.finish(rd.timeline(an.churn_timeline(r, ref, period, since, path, author,
                                                   max_commits)), r.warnings())


@tool
def release_notes(
    repo: str | None = None, from_ref: str | None = None, to_ref: str = "HEAD",
    group_by: str = "type", max_commits: int = 500,
) -> str:
    """Markdown release notes for from_ref..to_ref (default: latest tag before to_ref up to
    HEAD). group_by='type' groups by conventional-commit type (feat, fix, ...) with breaking
    changes first; group_by='area' groups by the top-level directory each commit mostly
    touched. Merge commits and PR numbers ((#123), 'Merge pull request #123') are listed."""
    r = _open(repo)
    return rd.finish(rd.release(an.release_notes(r, to_ref, from_ref, group_by, max_commits)),
                     r.warnings())


@tool
def commit_search(
    repo: str | None = None, query: str | None = None, author: str | None = None,
    path: str = "", since: str | None = None, until: str | None = None,
    content: str | None = None, include_merges: bool = False, limit: int = 20, ref: str = "HEAD",
) -> str:
    """Find commits. All filters are case-insensitive literals and combine with AND: query
    (in the message), author (name or email substring), path (file or directory), since/until
    (git dates), content (text added or removed by the commit, git's pickaxe -S). At least
    one filter is required. limit is capped at 200."""
    r = _open(repo)
    res = an.commit_search(r, ref, query, author, path, since, until, content,
                           include_merges, limit)
    return rd.finish(rd.search(res), r.warnings())


@tool
def file_history(path: str, repo: str | None = None, limit: int = 30, ref: str = "HEAD") -> str:
    """Commits that touched a file, newest first, following it across renames (reports the
    earlier names). Shows lines added/deleted per commit."""
    r = _open(repo)
    return rd.finish(rd.history(an.file_history(r, path, ref, limit)), r.warnings())


def main() -> None:
    ap = argparse.ArgumentParser(prog="gitrisk-mcp", description=__doc__)
    ap.add_argument("--dir", action="append", default=[], metavar="PATH",
                    help="allow reading repositories under PATH (repeatable; default: the "
                         "current directory)")
    ns = ap.parse_args()
    set_sandbox(ns.dir)
    mcp.run()


if __name__ == "__main__":
    main()
