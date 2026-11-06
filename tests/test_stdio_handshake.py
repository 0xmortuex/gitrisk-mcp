"""MCP stdio smoke test: spawns the real server, lists tools, calls them over the wire,
and checks that errors reach the agent as readable text and that --dir is enforced."""

import os
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "repo_summary", "hotspots", "file_experts", "change_coupling", "bus_factor",
    "churn_timeline", "release_notes", "commit_search", "file_history",
}


def params(*roots: str) -> StdioServerParameters:
    args = ["-m", "gitrisk_mcp"]
    for r in roots:
        args += ["--dir", r]
    return StdioServerParameters(command=sys.executable, args=args)


def text(res) -> str:  # type: ignore[no-untyped-def]
    return str(res.content[0].text)


@pytest.mark.anyio
async def test_tools_and_calls_over_stdio(acme_dir: str, acme) -> None:  # type: ignore[no-untyped-def]
    async with stdio_client(params(os.path.dirname(acme_dir))) as (rd, wr), \
            ClientSession(rd, wr) as session:
        init = await session.initialize()
        assert init.server_info.name == "gitrisk"
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(t.description and len(t.description) > 80 for t in tools)

        res = await session.call_tool("repo_summary", {"repo": acme_dir})
        assert not res.is_error and "Commits: 9" in text(res)
        res = await session.call_tool("hotspots", {"repo": acme_dir, "since": "2000-01-01"})
        assert not res.is_error and "core/engine.py" in text(res)
        res = await session.call_tool("file_experts", {"repo": acme_dir, "path": "team"})
        assert "Bus factor 2" in text(res)
        res = await session.call_tool("release_notes", {"repo": acme_dir})
        assert "### Features" in text(res) and "**team:** add b" in text(res)
        res = await session.call_tool("file_history", {"repo": acme_dir, "path": "api/routes.py"})
        assert "explain routes" in text(res)


@pytest.mark.anyio
async def test_errors_reach_the_agent(acme_dir: str, acme, tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with stdio_client(params(os.path.dirname(acme_dir))) as (rd, wr), \
            ClientSession(rd, wr) as session:
        await session.initialize()
        res = await session.call_tool("hotspots", {"repo": acme_dir, "since": "2000-01-01",
                                                   "ref": "nope"})
        assert res.is_error and "unknown ref 'nope'" in text(res)
        res = await session.call_tool("file_experts", {"repo": acme_dir, "path": "missing/dir"})
        assert res.is_error and "no files at 'missing/dir'" in text(res)
        res = await session.call_tool("commit_search", {"repo": acme_dir})
        assert res.is_error and "at least one filter" in text(res)
        res = await session.call_tool("repo_summary", {"repo": str(tmp_path)})
        assert res.is_error and "outside the allowed directories" in text(res)
        res = await session.call_tool("repo_summary", {"repo": acme_dir, "ref": "--output=x"})
        assert res.is_error and "invalid ref" in text(res)


@pytest.mark.anyio
async def test_default_directory_is_the_working_directory(acme_dir: str, acme) -> None:  # type: ignore[no-untyped-def]
    p = StdioServerParameters(command=sys.executable, args=["-m", "gitrisk_mcp"], cwd=acme_dir)
    async with stdio_client(p) as (rd, wr), ClientSession(rd, wr) as session:
        await session.initialize()
        res = await session.call_tool("repo_summary", {})
        assert not res.is_error and "Commits: 9" in text(res)
