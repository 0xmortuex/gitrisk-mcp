"""The sandbox, and the promise that nothing ever modifies the repository."""

import os
import pathlib

import pytest
from gitrepo import ALICE, Builder, git, snapshot

from gitrisk_mcp import server
from gitrisk_mcp.git import GitError, Sandbox


def test_sandbox_allows_inside_and_refuses_outside(acme: Builder, tmp_path: pathlib.Path) -> None:
    root = os.path.dirname(acme.path)
    assert Sandbox([root]).open(acme.path).path
    assert Sandbox([acme.path]).open(None).path  # default repo is the first root
    assert Sandbox([acme.path]).open(os.path.join(acme.path, "core")).path  # subdirectory
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(GitError, match="outside the allowed directories"):
        Sandbox([str(other)]).open(acme.path)
    with pytest.raises(GitError, match="outside the allowed directories"):
        Sandbox([acme.path]).open(os.path.join(acme.path, "..", ".."))


def test_sandbox_refuses_repo_that_only_contains_the_allowed_dir(tmp_path: pathlib.Path) -> None:
    b = Builder(str(tmp_path / "outer"))
    b.commit(ALICE, "2026-01-05T10:00:00+0000", "add", appends={"sub/a.py": 1})
    with pytest.raises(GitError, match="belongs to the repository"):
        Sandbox([os.path.join(b.path, "sub")]).open(None)


def test_sandbox_not_a_repo_and_missing(tmp_path: pathlib.Path) -> None:
    with pytest.raises(GitError, match="not inside a git repository"):
        Sandbox([str(tmp_path)]).open(str(tmp_path))
    with pytest.raises(GitError, match="not a directory"):
        Sandbox([str(tmp_path)]).open(str(tmp_path / "missing"))


def test_environment_cannot_redirect_git(acme: Builder, tmp_path: pathlib.Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    other = Builder(str(tmp_path / "decoy"))
    other.commit(ALICE, "2026-01-05T10:00:00+0000", "decoy", appends={"x.py": 1})
    monkeypatch.setenv("GIT_DIR", os.path.join(other.path, ".git"))
    from gitrisk_mcp import analysis as an
    repo = Sandbox([os.path.dirname(acme.path)]).open(acme.path)
    assert an.repo_summary(repo)["commits"] == 9


def test_every_tool_leaves_the_repository_byte_identical(acme_copy: str) -> None:
    status_before = git(acme_copy, "status", "--porcelain=v1")
    # status itself may refresh the index; take the snapshot AFTER it, then run the tools.
    before = snapshot(acme_copy)
    server.set_sandbox([os.path.dirname(acme_copy)])
    r = acme_copy
    calls = [
        lambda: server.repo_summary(repo=r),
        lambda: server.hotspots(repo=r, since=None),
        lambda: server.file_experts("core", repo=r),
        lambda: server.file_experts("core/engine.py", repo=r),
        lambda: server.change_coupling("core/engine.py", repo=r),
        lambda: server.bus_factor(repo=r),
        lambda: server.churn_timeline(repo=r, period="week"),
        lambda: server.release_notes(repo=r),
        lambda: server.release_notes(repo=r, group_by="area"),
        lambda: server.commit_search(repo=r, query="feat", content="engine"),
        lambda: server.file_history("core/engine.py", repo=r),
    ]
    for call in calls:
        assert isinstance(call(), str)
        assert snapshot(acme_copy) == before
    assert git(acme_copy, "status", "--porcelain=v1") == status_before
    assert not os.path.exists(os.path.join(acme_copy, ".git", "index.lock"))


def test_dirty_working_tree_is_not_touched(acme_copy: str) -> None:
    with open(os.path.join(acme_copy, "core", "engine.py"), "ab") as fh:
        fh.write(b"uncommitted\n")
    with open(os.path.join(acme_copy, "untracked.txt"), "wb") as fh:
        fh.write(b"x\n")
    before = snapshot(acme_copy)
    server.set_sandbox([os.path.dirname(acme_copy)])
    out = server.file_experts("core/engine.py", repo=acme_copy)
    assert "22 surviving lines" in out  # blame reads HEAD, not the dirty file
    assert snapshot(acme_copy) == before
