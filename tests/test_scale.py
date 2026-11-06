"""A generated repository with 20,000 commits, and (when present) this machine's real repos."""

import os
import pathlib
import time

import pytest
from gitrepo import big_history, fast_import, git

from gitrisk_mcp import analysis as an
from gitrisk_mcp.git import Repo, Sandbox

N = 20000


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    root = tmp_path_factory.mktemp("big")
    path = str(root / "big.git")
    fast_import(path, big_history(N))
    return Sandbox([str(root)]).open(path)


@pytest.mark.slow
def test_twenty_thousand_commits(big: Repo) -> None:
    timings = {}

    def timed(name: str, fn):  # type: ignore[no-untyped-def]
        t = time.perf_counter()
        out = fn()
        timings[name] = time.perf_counter() - t
        return out

    s = timed("summary", lambda: an.repo_summary(big, max_commits=N))
    assert s["commits"] == N and s["author_count"] == 12 and not s["scan"].truncated
    h = timed("hotspots", lambda: an.hotspots(big, since=None, as_of="2025-06-01", max_commits=N,
                                           max_files_per_commit=50))
    assert h["scan"].commits == N and h["scan"].skipped_mass == 8  # the 60-file commits
    assert len(h["rows"]) == 20
    c = timed("coupling", lambda: an.change_coupling(big, "mod3/file3.py", min_support=1,
                                                     min_confidence=0.0, max_commits=N))
    assert c["target_commits"] > 50
    b = timed("bus_factor", lambda: an.bus_factor(big, max_commits=N, as_of="2025-06-01"))
    assert {r["dir"] for r in b["rows"]} == {f"mod{i}" for i in range(15)}
    t = timed("timeline", lambda: an.churn_timeline(big, period="month"))
    assert sum(x[1] for x in t["series"]) == N
    e = timed("experts", lambda: an.file_experts(big, "mod3", as_of="2025-06-01", max_files=20))
    assert e["files"] == 20 and e["lines"] > 0
    r = timed("release", lambda: an.release_notes(big, max_commits=500))
    assert r["truncated"] and r["commits"] == 500
    q = timed("search", lambda: an.commit_search(big, query="change 1999", limit=5))
    assert len(q["rows"]) == 5 and q["more"]
    fh = timed("history", lambda: an.file_history(big, "mod3/file3.py", limit=10))
    assert len(fh["rows"]) == 10
    print("\nTIMINGS (20000 commits):", {k: round(v, 2) for k, v in timings.items()})
    assert max(timings.values()) < 60  # generous: CI runners are slow, real runs take seconds


@pytest.mark.slow
def test_streaming_stops_early(big: Repo) -> None:
    t = time.perf_counter()
    h = an.hotspots(big, since=None, max_commits=100)
    assert h["scan"].truncated and h["scan"].commits == 100
    assert time.perf_counter() - t < 15


def _ref_state(path: str) -> str:
    refs = git(path, "for-each-ref", "--format=%(refname) %(objectname)")
    return refs + git(path, "rev-parse", "HEAD")


REAL = [r"C:\Claude code free\Mort", r"C:\Claude code free\vex"]


@pytest.mark.parametrize("path", [p for p in REAL if os.path.isdir(os.path.join(p, ".git"))])
@pytest.mark.slow
def test_real_repositories_read_only(path: str) -> None:
    """Runs on the author's machine only (skipped elsewhere): real history, read-only."""
    index = pathlib.Path(path, ".git", "index")
    index_before = index.read_bytes() if index.exists() else b""
    refs_before = _ref_state(path)
    repo = Sandbox([os.path.dirname(path)]).open(path)
    t = time.perf_counter()
    s = an.repo_summary(repo)
    h = an.hotspots(repo, since="12 months ago")
    top = h["rows"][0]["path"] if h["rows"] else None
    if top:
        an.file_experts(repo, top)
        an.change_coupling(repo, top)
    an.bus_factor(repo)
    an.churn_timeline(repo)
    an.release_notes(repo)
    print(f"\n{os.path.basename(path)}: {s['commits']} commits, all tools in "
          f"{time.perf_counter() - t:.1f}s")
    assert _ref_state(path) == refs_before
    assert (index.read_bytes() if index.exists() else b"") == index_before
