"""Exact numbers from the scripted acme history (see gitrepo.build_acme)."""

import math
import os

import pytest
from gitrepo import AS_OF

from gitrisk_mcp import analysis as an
from gitrisk_mcp.git import GitError, Repo

LOG2_3 = math.log2(3)


def test_hotspots_ranking_no_decay(acme_repo: Repo) -> None:
    r = an.hotspots(acme_repo, since=None, half_life_days=0, as_of=AS_OF, limit=100)
    rows = r["rows"]
    paths = [x["path"] for x in rows]
    assert paths[:6] == ["core/engine.py", "api/routes.py", "core/util.py",
                         "team/a.py", "team/b.py", "docs/readme.md"]
    by = {x["path"]: x for x in rows}
    e = by["core/engine.py"]
    assert (e["commits"], e["lines"], e["authors"]) == (6, 22, 3)  # Bobby merged into Bob
    assert e["score"] == pytest.approx(22 * (1 + LOG2_3))
    assert by["api/routes.py"]["score"] == pytest.approx(10 * 2)
    assert by["core/util.py"]["score"] == pytest.approx(8 * 2)
    assert by["team/a.py"]["score"] == pytest.approx(5)
    assert "uv.lock" not in by  # lock files are excluded by default
    # engine util routes readme a b .mailmap + 12 vendor files
    assert r["files_ranked"] == 7 + 12
    assert r["scan"].commits == 9


def test_hotspots_mass_commit_is_skipped(acme_repo: Repo) -> None:
    r = an.hotspots(acme_repo, since=None, half_life_days=0, as_of=AS_OF,
                    max_files_per_commit=10, limit=100)
    by = {x["path"]: x for x in r["rows"]}
    assert r["scan"].skipped_mass == 1
    assert not any(p.startswith("vendor/") for p in by)
    e = by["core/engine.py"]
    assert (e["commits"], e["lines"], e["authors"]) == (5, 21, 2)
    assert e["score"] == pytest.approx(21 * 2)


def test_hotspots_recency_decay(acme_repo: Repo) -> None:
    r = an.hotspots(acme_repo, since=None, half_life_days=30, as_of=AS_OF, path="team", limit=10)
    age = 3 + 14 / 24  # 2026-02-25T10:00 -> 2026-03-01T00:00 (Feb has 28 days)
    by = {x["path"]: x for x in r["rows"]}
    assert by["team/a.py"]["score"] == pytest.approx(5 * 0.5 ** (age / 30))
    older = 2 + 14 / 24  # team/b.py is one day newer
    assert by["team/b.py"]["score"] == pytest.approx(5 * 0.5 ** (older / 30))


def test_hotspots_since_window_and_exclude(acme_repo: Repo) -> None:
    r = an.hotspots(acme_repo, since="2026-02-01", half_life_days=0, as_of=AS_OF,
                    exclude=["vendor/*"], limit=100)
    by = {x["path"]: x for x in r["rows"]}
    assert by["core/engine.py"]["commits"] == 3  # c4, c6, c7
    assert not any(p.startswith("vendor/") for p in by)


def test_experts_file(acme_repo: Repo) -> None:
    r = an.file_experts(acme_repo, "core/engine.py", recent_days=30, as_of=AS_OF)
    rows = {x["name"]: x for x in r["rows"]}
    assert r["lines"] == 22 and r["bus_factor"] == 1
    assert [x["name"] for x in r["rows"]] == ["Alice", "Bob", "Carol"]
    assert (rows["Alice"]["lines"], rows["Bob"]["lines"], rows["Carol"]["lines"]) == (14, 7, 1)
    assert rows["Alice"]["blame_share"] == pytest.approx(14 / 22)
    # recent window = last 30 days before 2026-03-01: c4 (Bobby +2), c6 (Alice +1), c7 (Carol +1)
    assert rows["Bob"]["recent_share"] == pytest.approx(2 / 4)
    assert rows["Alice"]["score"] == pytest.approx(0.6 * 14 / 22 + 0.4 * 1 / 4)
    assert rows["Bob"]["score"] == pytest.approx(0.6 * 7 / 22 + 0.4 * 2 / 4)
    assert not any(x["inactive"] for x in r["rows"])


def test_experts_directory_and_inactive(acme_repo: Repo) -> None:
    r = an.file_experts(acme_repo, "core", recent_days=30, inactive_days=60, as_of="2026-06-01")
    rows = {x["name"]: x for x in r["rows"]}
    assert r["files"] == 2 and r["lines"] == 30
    assert (rows["Alice"]["lines"], rows["Bob"]["lines"], rows["Carol"]["lines"]) == (19, 10, 1)
    assert r["bus_factor"] == 1
    assert all(x["inactive"] for x in r["rows"])  # nobody touched it for 60+ days at 2026-06-01
    assert r["inactive_share"] == pytest.approx(1.0)
    # nobody changed anything recently, so the score is the blame share alone
    assert rows["Alice"]["score"] == pytest.approx(19 / 30)


def test_experts_bus_factor_two(acme_repo: Repo) -> None:
    r = an.file_experts(acme_repo, "team", as_of=AS_OF)
    assert r["bus_factor"] == 2  # 50/50 split: neither holds MORE than half
    assert {x["name"]: x["lines"] for x in r["rows"]} == {"Bob": 5, "Carol": 5}


def test_experts_errors(acme_repo: Repo) -> None:
    with pytest.raises(GitError, match="no files at 'nope'"):
        an.file_experts(acme_repo, "nope")


def test_coupling_confidence(acme_repo: Repo) -> None:
    r = an.change_coupling(acme_repo, "core/engine.py")
    assert r["target_commits"] == 6
    assert [x["path"] for x in r["rows"]] == ["core/util.py"]
    u = r["rows"][0]
    assert u["support"] == 4 and u["confidence"] == pytest.approx(4 / 6)
    assert u["reverse_confidence"] == pytest.approx(1.0)


def test_coupling_filters_mass_commits(acme_repo: Repo) -> None:
    r = an.change_coupling(acme_repo, "core/engine.py", max_files_per_commit=10)
    assert r["target_commits"] == 5 and r["scan"].skipped_mass == 1
    assert r["rows"][0]["confidence"] == pytest.approx(4 / 5)
    loose = an.change_coupling(acme_repo, "api/routes.py", min_support=1, min_confidence=0.0)
    by = {x["path"]: x for x in loose["rows"]}
    assert by["core/engine.py"]["confidence"] == pytest.approx(1 / 2)
    assert by["core/engine.py"]["reverse_confidence"] == pytest.approx(1 / 6)


def test_coupling_errors(acme_repo: Repo) -> None:
    with pytest.raises(GitError, match="not a file"):
        an.change_coupling(acme_repo, "core")


def test_bus_factor_per_directory(acme_repo: Repo) -> None:
    r = an.bus_factor(acme_repo, half_life_days=0, as_of=AS_OF)
    by = {x["dir"]: x for x in r["rows"]}
    assert by["team"]["bus_factor"] == 2 and not by["team"]["at_risk"]
    assert by["core"]["bus_factor"] == 1 and by["core"]["at_risk"]
    assert by["core"]["top_author"] == "Alice" and by["core"]["top_share"] == pytest.approx(19 / 30)
    assert by["api"]["bus_factor"] == 1 and not by["api"]["at_risk"]  # only 2 commits
    assert by["."]["top_author"] == "Alice"
    assert r["rows"][-1]["dir"] == "team"  # least risky last


def test_bus_factor_depth_two(acme_repo: Repo) -> None:
    r = an.bus_factor(acme_repo, depth=2, half_life_days=0, path="core")
    assert {x["dir"] for x in r["rows"]} == {"core"}


def test_timeline_month_and_week(acme_repo: Repo) -> None:
    m = an.churn_timeline(acme_repo, period="month")
    assert [(str(d), c, a, x) for d, c, a, x in m["series"]] == [
        ("2026-01-01", 3, 33, 0), ("2026-02-01", 6, 132, 0)]
    w = an.churn_timeline(acme_repo, period="week")
    assert [s[1] for s in w["series"]] == [1, 1, 1, 0, 1, 0, 1, 4]
    assert w["commit_spark"] == "---_-_-@"
    c = an.churn_timeline(acme_repo, period="month", author="carol")
    assert [s[1] for s in c["series"]] == [3]
    p = an.churn_timeline(acme_repo, period="month", path="team")
    assert [s[1] for s in p["series"]] == [2]


def test_timeline_no_match(acme_repo: Repo) -> None:
    with pytest.raises(GitError, match="no commits match"):
        an.churn_timeline(acme_repo, author="nobody-here")


def test_sparkline_levels() -> None:
    assert an.sparkline([0, 0]) == "__"
    assert an.sparkline([1, 9]) == ".@"
    assert an.sparkline([]) == ""


def test_summary(acme_repo: Repo) -> None:
    r = an.repo_summary(acme_repo)
    assert r["commits"] == 9 and r["merges"] == 0 and r["author_count"] == 3
    assert {n: c for n, _, c in r["authors"]} == {"Alice": 3, "Bob": 3, "Carol": 3}
    langs = {k: v for k, v, _ in r["languages"]}
    assert langs["Python"] == 50 and langs["Markdown"] == 2 and langs["Other"] == 13  # uv.lock is ignored
    assert [t for t, _ in r["tags"]] == ["v0.1"]
    assert r["branches"][0][0] == "main"
    assert an.iso_day(r["first_ts"]) == "2026-01-05" and an.iso_day(r["last_ts"]) == "2026-02-26"
    assert not r["shallow"] and not r["bare"]


def test_release_notes_by_type_default_from_latest_tag(acme_repo: Repo) -> None:
    r = an.release_notes(acme_repo)
    assert r["from"] == "v0.1" and r["commits"] == 6
    assert list(r["groups"]) == ["Features", "Refactoring", "Documentation", "Chores", "Style"]
    assert [e["desc"] for e in r["groups"]["Features"]] == ["add b", "add a"]
    assert r["groups"]["Features"][0]["scope"] == "team"
    assert {n: c for n, c in r["contributors"]} == {"Alice": 1, "Bob": 2, "Carol": 3}


def test_release_notes_by_area(acme_repo: Repo) -> None:
    r = an.release_notes(acme_repo, group_by="area")
    assert list(r["groups"]) == ["(repo root)", "api", "core", "team", "vendor"]
    assert [e["desc"] for e in r["groups"]["team"]] == ["add b", "add a"]
    assert [e["desc"] for e in r["groups"]["(repo root)"]] == ["bump deps"]


def test_release_notes_merges_prs_breaking(rel_repo: Repo) -> None:
    r = an.release_notes(rel_repo)
    assert r["from"] == "v1.0.0" and r["commits"] == 5
    assert list(r["groups"]) == ["Features", "Bug fixes", "Performance", "Other changes"]
    assert r["breaking"][0]["note"] == "the legacy flag was removed"
    assert r["merges"][0]["pr"] == 21 and r["merges"][0]["title"] == "Add x endpoint"
    assert r["merges"][0]["branch"] == "alice/feat-x"
    perf = r["groups"]["Performance"][0]
    assert perf["pr"] == 22 and perf["scope"] == "app"
    assert {n: c for n, c in r["contributors"]} == {"Alice": 1, "Bob": 2, "Carol": 1}
    by_area = an.release_notes(rel_repo, group_by="area")
    assert list(by_area["groups"]) == ["(repo root)", "api", "app"]
    assert len(by_area["groups"]["app"]) == 2


def test_release_notes_bad_args(acme_repo: Repo) -> None:
    with pytest.raises(GitError, match="unknown ref"):
        an.release_notes(acme_repo, from_ref="nope")
    with pytest.raises(GitError, match="group_by"):
        an.release_notes(acme_repo, group_by="x")


def test_commit_search(acme_repo: Repo) -> None:
    def subj(**kw: object) -> list[str]:
        return [c.subject for c in an.commit_search(acme_repo, **kw)["rows"]]  # type: ignore[arg-type]

    assert subj(query="ROUTES") == ["docs: explain routes", "feat(api): add routes (#13)"]
    assert len(subj(author="carol")) == 3
    assert len(subj(author="bob@x.io")) == 3  # mailmap applied: includes the Bobby commit
    assert subj(author="bob@old.example") == []
    assert subj(path="api") == ["docs: explain routes", "feat(api): add routes (#13)"]
    assert subj(content="engine.py:Carol:") == ["style: reformat everything"]
    assert len(subj(since="2026-02-20", until="2026-02-28")) == 4
    assert subj(query="docs", author="carol") == ["docs: explain routes"]
    r = an.commit_search(acme_repo, author="carol", limit=2)
    assert len(r["rows"]) == 2 and r["more"]
    with pytest.raises(GitError, match="at least one filter"):
        an.commit_search(acme_repo)
    with pytest.raises(GitError, match="limit"):
        an.commit_search(acme_repo, query="x", limit=1000)


def test_commit_search_literal_not_regex(acme_repo: Repo) -> None:
    assert an.commit_search(acme_repo, query="feat(team)")["rows"]  # parens are literal
    assert not an.commit_search(acme_repo, query="feat.*team")["rows"]


def test_bad_ref_and_option_injection(acme_repo: Repo, acme_dir: str) -> None:
    for bad in ("--output=pwned.txt", "-x", ""):
        with pytest.raises(GitError, match="invalid ref"):
            acme_repo.resolve(bad)
    with pytest.raises(GitError, match="unknown ref"):
        acme_repo.resolve("no-such-branch")
    assert not os.path.exists(os.path.join(acme_dir, "pwned.txt"))
    with pytest.raises(GitError, match="must not contain"):
        an.file_history(acme_repo, "../etc/passwd")


def test_rendered_text(acme_repo: Repo) -> None:
    from gitrisk_mcp import server
    server.set_sandbox([os.path.dirname(acme_repo.path)])
    out = server.hotspots(repo=acme_repo.path, since=None, half_life_days=0, limit=3, as_of=AS_OF)
    assert "| 1 | core/engine.py | 56.9 | 6 | 22 | 3 | 2026-02-24 |" in out
    out = server.churn_timeline(repo=acme_repo.path, period="week")
    assert "commits ---_-_-@" in out and "| 2026-02-23 | 4 | 125 | 0 |" in out
    out = server.release_notes(repo=acme_repo.path)
    assert "### Features\n- **team:** add b (" in out and "Carol (3), Bob (2), Alice (1)" in out
    out = server.file_experts("core/engine.py", repo=acme_repo.path, as_of=AS_OF)
    assert "Bus factor 1. BUS FACTOR 1" in out and "| Alice | alice@x.io | 14 | 64% |" in out
