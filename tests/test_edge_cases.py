"""Renames, binary files, odd path bytes, shallow and bare clones, empty repos."""

import math
import os
import pathlib

import pytest
from gitrepo import ALICE, BOB, CAROL, FI, Builder, fast_import, git

from gitrisk_mcp import analysis as an
from gitrisk_mcp import render as rd
from gitrisk_mcp.git import GitError, Repo, Sandbox


def open_repo(path: str) -> Repo:
    return Sandbox([os.path.dirname(path)]).open(path)


@pytest.fixture
def renamed(tmp_path: pathlib.Path) -> Repo:
    b = Builder(str(tmp_path / "ren"))
    b.commit(ALICE, "2026-01-05T10:00:00+0000", "add", appends={"old/name.py": 20, "other.txt": 1})
    os.makedirs(os.path.join(b.path, "new"))
    git(b.path, "mv", "old/name.py", "new/name.py")
    b.commit(CAROL, "2026-01-10T10:00:00+0000", "refactor: move", appends={"new/name.py": 1})
    b.commit(BOB, "2026-01-15T10:00:00+0000", "tweak", appends={"new/name.py": 2, "other.txt": 1})
    return open_repo(b.path)


def test_file_history_follows_rename(renamed: Repo) -> None:
    r = an.file_history(renamed, "new/name.py")
    assert [x["subject"] for x in r["rows"]] == ["tweak", "refactor: move", "add"]
    assert [(x["added"], x["deleted"]) for x in r["rows"]] == [(2, 0), (1, 0), (20, 0)]
    assert r["rows"][1]["renamed_from"] == "old/name.py"
    assert r["names"] == ["new/name.py", "old/name.py"]
    text = "\n".join(rd.history(r))
    assert "also known as: old/name.py" in text and "renamed from old/name.py" in text


def test_hotspots_and_coupling_follow_renames(renamed: Repo) -> None:
    h = an.hotspots(renamed, since=None, half_life_days=0)
    top = h["rows"][0]
    assert top["path"] == "new/name.py"
    assert (top["commits"], top["lines"], top["authors"]) == (3, 23, 3)
    assert top["score"] == pytest.approx(23 * (1 + math.log2(3)))
    assert not any(x["path"] == "old/name.py" for x in h["rows"])
    c = an.change_coupling(renamed, "new/name.py")
    assert c["target_commits"] == 3
    assert c["rows"][0]["path"] == "other.txt" and c["rows"][0]["support"] == 2
    assert c["rows"][0]["confidence"] == pytest.approx(2 / 3)


def test_file_history_unknown_path(renamed: Repo) -> None:
    with pytest.raises(GitError, match="no history for"):
        an.file_history(renamed, "never/existed.py")


def test_binary_files(tmp_path: pathlib.Path) -> None:
    b = Builder(str(tmp_path / "bin"))
    b.write("assets/logo.png", b"\x89PNG\x00\x01\x02" * 50)
    b.commit(ALICE, "2026-01-05T10:00:00+0000", "add", appends={"src/a.py": 4})
    b.write("assets/logo.png", b"\x89PNG\x00\x09\x09" * 60)
    b.commit(BOB, "2026-01-06T10:00:00+0000", "update logo")
    repo = open_repo(b.path)
    h = an.hotspots(repo, since=None, half_life_days=0)
    by = {x["path"]: x for x in h["rows"]}
    assert by["assets/logo.png"]["binary"] and by["assets/logo.png"]["lines"] == 2  # 1 per change
    assert by["src/a.py"]["lines"] == 4
    e = an.file_experts(repo, "", as_of="2026-01-07")
    assert e["skipped_binary"] == 1 and e["lines"] == 4 and e["files"] == 1
    assert an.repo_summary(repo)["languages"] == [("Python", 4, 1.0)]  # binary adds no churn


def test_non_utf8_path_names(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "odd.git")
    fast_import(path, [
        FI(ALICE, 1_700_000_000, "add", [(b"caf\xe9.txt", b"one\ntwo\n"), (b"ok.txt", b"a\nb\nc\n")]),
        FI(BOB, 1_700_086_400, "edit", [(b"caf\xe9.txt", b"one\ntwo\nthree\n")]),
    ])
    repo = open_repo(path)
    h = an.hotspots(repo, since=None, half_life_days=0, as_of="2023-12-01")
    by = {x["path"]: x for x in h["rows"]}
    assert "caf\\xe9.txt" in by  # visible, unambiguous, never lost
    assert by["caf\\xe9.txt"]["commits"] == 2 and by["caf\\xe9.txt"]["authors"] == 2
    e = an.file_experts(repo, "", as_of="2023-12-01")
    assert e["skipped_nonutf8"] == 1 and e["lines"] == 3  # ok.txt blamed, odd name skipped
    assert "non-UTF-8" in "\n".join(rd.experts(e))
    assert an.churn_timeline(repo)["series"][0][1] == 2
    assert an.repo_summary(repo)["commits"] == 2


def test_utf8_path_and_unicode_author(tmp_path: pathlib.Path) -> None:
    b = Builder(str(tmp_path / "uni"))
    b.commit(("Zoë Ångström", "zoe@x.io"), "2026-01-05T10:00:00+0000", "add",
             appends={"déjà/vu.py": 3})
    repo = open_repo(b.path)
    e = an.file_experts(repo, "déjà/vu.py", as_of="2026-01-06")
    assert e["rows"][0]["name"] == "Zoë Ångström" and e["lines"] == 3


def test_shallow_clone_warns(acme_dir: str, acme: Builder, tmp_path: pathlib.Path) -> None:
    dst = str(tmp_path / "shallow")
    git(str(tmp_path), "clone", "-q", "--depth", "3", pathlib.Path(acme.path).as_uri(), dst)
    repo = open_repo(dst)
    assert repo.shallow
    assert an.repo_summary(repo)["commits"] == 3
    assert any("shallow clone" in w for w in repo.warnings())
    from gitrisk_mcp import server
    server.set_sandbox([str(tmp_path)])
    out = server.hotspots(repo=dst, since=None)
    assert "WARNING: this is a shallow clone" in out



def test_bare_repository(acme: Builder, tmp_path: pathlib.Path) -> None:
    dst = str(tmp_path / "bare.git")
    git(str(tmp_path), "clone", "-q", "--bare", acme.path, dst)
    repo = open_repo(dst)
    assert repo.bare and not repo.shallow
    full = open_repo(acme.path)
    a = an.hotspots(repo, since=None, half_life_days=0, limit=50)["rows"]
    b = an.hotspots(full, since=None, half_life_days=0, limit=50)["rows"]
    assert [(x["path"], x["score"]) for x in a] == [(x["path"], x["score"]) for x in b]
    e = an.file_experts(repo, "core/engine.py", as_of="2026-03-01")
    assert (e["lines"], e["bus_factor"]) == (22, 1)
    # mailmap is read from HEAD:.mailmap in a bare repository
    assert {n for n, _, _ in an.repo_summary(repo)["authors"]} == {"Alice", "Bob", "Carol"}
    assert an.change_coupling(repo, "core/engine.py")["rows"][0]["path"] == "core/util.py"


def test_empty_repository(tmp_path: pathlib.Path) -> None:
    Builder(str(tmp_path / "empty"))
    repo = open_repo(str(tmp_path / "empty"))
    with pytest.raises(GitError, match="unknown ref 'HEAD'"):
        an.repo_summary(repo)


def test_wrong_ref_for_every_tool(acme_repo: Repo) -> None:
    for fn in (an.repo_summary, an.hotspots, an.churn_timeline, an.bus_factor):
        with pytest.raises(GitError, match="unknown ref 'nope'"):
            fn(acme_repo, "nope")  # type: ignore[operator]


def test_merge_commits_not_counted_as_churn(rel_repo: Repo) -> None:
    h = an.hotspots(rel_repo, since=None, half_life_days=0)
    by = {x["path"]: x for x in h["rows"]}
    assert by["api/x.py"]["commits"] == 1  # the merge itself is not a second touch
    s = an.repo_summary(rel_repo)
    assert s["merges"] == 1 and s["commits"] == 6
    t = an.churn_timeline(rel_repo)
    assert t["series"][0][1] == 5  # six commits minus the merge
