"""Turn analysis results into compact text for an agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .analysis import Scan, iso_day


def table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "(none)"
    cells = [[str(c) for c in r] for r in rows]
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in cells]
    return "\n".join(out)


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def scan_notes(scan: Scan, max_commits: int, what: str = "commits") -> list[str]:
    notes = [f"Scanned {scan.commits} {what}."]
    if scan.skipped_mass:
        notes[0] += f" Skipped {scan.skipped_mass} mass commit(s) (too many files)."
    if scan.truncated:
        notes.append(f"NOTE: stopped at max_commits={max_commits}; older history was not read. "
                     "Raise max_commits or narrow with since=.")
    return notes


def finish(body: list[str], repo_warnings: list[str]) -> str:
    return "\n".join(body + ([""] + repo_warnings if repo_warnings else []))


def hotspots(r: dict[str, Any]) -> list[str]:
    rows = [[i, x["path"], f"{x['score']:.1f}", x["commits"], x["lines"], x["authors"],
             iso_day(x["last_ts"]), "binary" if x["binary"] else ""]
            for i, x in enumerate(r["rows"], 1)]
    head = [f"Hotspots at {r['sha'][:10]} (score = recency-weighted churn, half-life "
            f"{r['half_life_days']:g} days, x (1 + log2 authors)); {r['files_ranked']} files ranked.", ""]
    return head + [table(["#", "file", "score", "commits", "lines", "authors", "last change", ""], rows),
                   ""] + scan_notes(r["scan"], r["max_commits"])


def experts(r: dict[str, Any]) -> list[str]:
    rows = [[x["name"], x["email"], x["lines"], pct(x["blame_share"]),
             x["recent_commits"], pct(x["recent_share"]), f"{x['score']:.3f}",
             iso_day(x["last_ts"]) if x["last_ts"] else "-", "INACTIVE" if x["inactive"] else ""]
            for x in r["rows"]]
    flag = ""
    if r["bus_factor"] == 1:
        flag = " BUS FACTOR 1: a single person holds most of this code."
    out = [f"Experts for {r['path']}: {r['lines']} surviving lines in {r['files']} file(s) "
           f"({r['authors']} authors). Bus factor {r['bus_factor']}.{flag}", "",
           table(["author", "email", "surviving lines", "blame share",
                  f"commits ({r['recent_days']}d)", "recent share", "score", "last touch", ""], rows)]
    if r["inactive_share"] > 0:
        out.append(f"\n{pct(r['inactive_share'])} of surviving lines belong to authors with no "
                   f"touch in {r['inactive_days']}+ days (INACTIVE).")
    if r["skipped_binary"] or r["skipped_cap"] or r["skipped_nonutf8"]:
        out.append(f"\nBlamed {r['files']} of {r['files_total']} files "
                   f"({r['skipped_binary']} binary, {r['skipped_nonutf8']} non-UTF-8 path name, {r['skipped_cap']} skipped by max_files).")
    return out


def coupling(r: dict[str, Any]) -> list[str]:
    rows = [[x["path"], x["support"], f"{x['confidence']:.2f}", f"{x['reverse_confidence']:.2f}",
             x["commits"]] for x in r["rows"]]
    head = [f"Change coupling for {r['target']}: {r['target_commits']} commits touched it "
            f"({r['matches']} coupled files; commits over {r['max_files_per_commit']} files ignored).",
            "confidence = P(other changes | this changes); reverse = P(this changes | other changes)", ""]
    return head + [table(["file", "support", "confidence", "reverse", "file commits"], rows),
                   ""] + scan_notes(r["scan"], r["max_commits"])


def bus_factor(r: dict[str, Any]) -> list[str]:
    rows = [[x["dir"], x["bus_factor"], x["authors"], x["commits"], x["top_author"],
             pct(x["top_share"]), iso_day(x["top_last_ts"]), "AT RISK" if x["at_risk"] else ""]
            for x in r["rows"]]
    head = [f"Bus factor by directory ({r['dirs']} directories; weights decay with a "
            f"{r['half_life_days']:g}-day half-life; AT RISK = factor 1 with >= {r['min_commits']} commits).",
            "bus factor = fewest authors whose combined share is more than 50%", ""]
    return head + [table(["directory", "bus factor", "authors", "commits", "top author",
                          "top share", "top author last", ""], rows),
                   ""] + scan_notes(r["scan"], r["max_commits"])


def timeline(r: dict[str, Any]) -> list[str]:
    s = r["series"]
    first, last = s[0][0], s[-1][0]
    out = [f"Churn per {r['period']} ({first} .. {last}, {len(s)} buckets)",
           f"commits {r['commit_spark']}", f"lines   {r['line_spark']}", ""]
    out.append(table([r["period"] + " starting", "commits", "+lines", "-lines"],
                     [[d.isoformat(), c, a, x] for d, c, a, x in s]))
    return out + [""] + scan_notes(r["scan"], r["max_commits"])


def summary(r: dict[str, Any]) -> list[str]:
    age_days = (r["last_ts"] - r["first_ts"]) / 86400
    out = [f"Repository {r['path']}" + (" (bare)" if r["bare"] else ""),
           f"Commits: {r['commits']} ({r['merges']} merges); history {iso_day(r['first_ts'])} .. "
           f"{iso_day(r['last_ts'])} ({age_days / 365.25:.1f} years)",
           f"Authors: {r['author_count']} (mailmap applied); top: "
           + ", ".join(f"{n} ({c})" for n, _, c in r["authors"][:5]),
           "Languages by churn: " + (", ".join(f"{k} {pct(f)}" for k, _, f in r["languages"]) or "none"),
           f"Branches: {len(r['branches'])} local, {r['remote_branches']} remote; most recent: "
           + ", ".join(b for b, _ in r["branches"][:5]),
           f"Tags: {len(r['tags'])}" + (": " + ", ".join(t for t, _ in r["tags"][:5]) if r["tags"] else "")]
    return out + scan_notes(r["scan"], r["max_commits"])


def release(r: dict[str, Any]) -> list[str]:
    base = r["from"] or "(start of history)"
    out = [f"## {base}..{r['to']}", "",
           f"{r['commits']} commits"
           + (f", {iso_day(r['first_ts'])} to {iso_day(r['last_ts'])}" if r["last_ts"] else "")]
    if r["from"] is None:
        out.append("(no tag found before this ref and no from_ref given: showing the whole history)")
    if r["truncated"]:
        out.append(f"NOTE: only the newest {r['max_commits']} commits are listed.")
    out.append("")

    def line(e: dict[str, Any]) -> str:
        scope = f"**{e['scope']}:** " if e["scope"] else ""
        pr = f" (#{e['pr']})" if e["pr"] and f"#{e['pr']}" not in e["desc"] else ""
        return f"- {scope}{e['desc']}{pr} ({e['sha']})"

    if r["breaking"]:
        out.append("### Breaking changes")
        out += [f"- {b['note']} ({b['sha']})" for b in r["breaking"]]
        out.append("")
    for title, items in r["groups"].items():
        out.append(f"### {title}")
        out += [line(e) for e in items]
        out.append("")
    if r["merges"]:
        out.append("### Merged pull requests and branches")
        for m in r["merges"]:
            ref = f"#{m['pr']} " if m["pr"] else ""
            br = f" [{m['branch']}]" if m["branch"] else ""
            out.append(f"- {ref}{m['title']}{br} ({m['sha']})")
        out.append("")
    if r["contributors"]:
        out.append("### Contributors")
        out.append(", ".join(f"{n} ({c})" for n, c in r["contributors"]))
    return out


def search(r: dict[str, Any]) -> list[str]:
    rows = [[c.sha[:10], datetime.fromtimestamp(c.ts, timezone.utc).strftime("%Y-%m-%d"),
             c.author, c.subject] for c in r["rows"]]
    out = [f"{len(rows)} commit(s)" + (f" shown (limit {r['limit']}; more exist)" if r["more"] else ""),
           "", table(["commit", "date", "author", "subject"], rows)]
    return out


def history(r: dict[str, Any]) -> list[str]:
    rows = [[x["sha"], iso_day(x["ts"]), x["author"],
             "binary" if x["binary"] else f"+{x['added']} -{x['deleted']}",
             x["subject"] + (f"  [renamed from {x['renamed_from']}]" if x["renamed_from"] else "")]
            for x in r["rows"]]
    out = [f"History of {r['path']}" + (f" (also known as: {', '.join(r['names'][1:])})"
                                         if len(r["names"]) > 1 else "")
           + (f"; newest {r['limit']} shown" if r["more"] else ""), "",
           table(["commit", "date", "author", "lines", "subject"], rows)]
    return out
