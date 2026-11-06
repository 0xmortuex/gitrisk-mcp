"""History analytics. Every function takes a Repo and returns plain data.

All maths is documented where it is defined so the numbers can be reproduced
by hand; render.py turns the results into text.
"""

from __future__ import annotations

import fnmatch
import math
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .git import Commit, GitError, Repo, dec

DAY = 86400.0

DEFAULT_EXCLUDES = (
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum",
)

LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".c": "C", ".h": "C/C++ header",
    ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".java": "Java",
    ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".lua": "Lua", ".sh": "Shell", ".ps1": "PowerShell",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".vue": "Vue", ".svelte": "Svelte",
    ".md": "Markdown", ".json": "JSON", ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML",
    ".sql": "SQL", ".s": "Assembly", ".asm": "Assembly", ".mort": "Mort", ".tex": "TeX",
}


# ---------------------------------------------------------------- helpers
def now_ts(as_of: str | None) -> float:
    if not as_of:
        return datetime.now(timezone.utc).timestamp()
    try:
        d = datetime.fromisoformat(as_of)
    except ValueError as e:
        raise GitError(f"as_of={as_of!r} is not an ISO date like 2026-03-01 or 2026-03-01T12:00:00") from e
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def decay(age_days: float, half_life_days: float) -> float:
    """0.5 ** (age / half_life); half_life <= 0 disables decay (weight 1)."""
    if half_life_days <= 0:
        return 1.0
    return float(0.5 ** (max(age_days, 0.0) / half_life_days))


def iso_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def norm_path(repo: Repo, p: str | None) -> str:
    """Repo-relative, forward-slash path; '' means the repository root."""
    if not p:
        return ""
    q = p.replace("\\", "/")
    if os.path.isabs(p):
        rel = os.path.relpath(p, repo.path).replace("\\", "/")
        if rel.startswith(".."):
            raise GitError(f"{p!r} is not inside the repository {repo.path!r}")
        q = rel
    while q.startswith("./"):
        q = q[2:]
    q = q.rstrip("/")
    if q == ".":
        return ""
    if ".." in q.split("/"):
        raise GitError(f"path {p!r} must not contain '..'")
    return q


def under(path: str, prefix: str) -> bool:
    return not prefix or path == prefix or path.startswith(prefix + "/")


def excluded(path: str, globs: list[str]) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(base, g) for g in globs)


def head_files(repo: Repo, sha: str) -> set[str]:
    out = repo.run(["ls-tree", "-r", "-z", "--name-only", sha])
    return {dec(x) for x in out.split(b"\x00") if x}


class RenameMap:
    """Follows renames while walking history newest-first.

    When a commit renames old -> new, every older commit that mentions `old`
    is about the same file as `new`. Because git log is newest-first, the
    rename is always seen before the older commits that need it.
    """

    def __init__(self) -> None:
        self.alias: dict[str, str] = {}

    def resolve(self, p: str) -> str:
        seen = 0
        while p in self.alias and seen < 1000:
            p = self.alias[p]
            seen += 1
        return p

    def learn(self, c: Commit) -> None:
        for f in c.files:
            if f.old_path is not None and f.old_path != f.path:
                self.alias[f.old_path] = self.resolve(f.path)


@dataclass
class Scan:
    commits: int = 0
    truncated: bool = False
    skipped_mass: int = 0
    merges_seen: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- hotspots
def hotspots(
    repo: Repo, ref: str = "HEAD", since: str | None = "12 months ago", until: str | None = None,
    path: str = "", limit: int = 20, half_life_days: float = 90.0,
    max_files_per_commit: int = 100, exclude: list[str] | None = None,
    as_of: str | None = None, max_commits: int = 20000,
) -> dict[str, Any]:
    """Rank files by recency-weighted churn x author breadth.

    For a file F, each commit c touching F contributes
        w(c) = lines(c) * 0.5 ** (age_days(c) / half_life_days)
    where lines(c) = added + deleted (1 for a binary change). Then
        score(F) = sum_c w(c) * (1 + log2(distinct_authors(F)))
    A file edited by 1 author gets x1, 2 authors x2, 4 authors x3. Only files
    that still exist at `ref` are ranked. Commits touching more than
    max_files_per_commit files (mass reformat / import) are skipped.
    """
    sha = repo.resolve(ref)
    now = now_ts(as_of)
    globs = list(DEFAULT_EXCLUDES) + (exclude or [])
    prefix = norm_path(repo, path)
    live = head_files(repo, sha)
    rn = RenameMap()
    stats: dict[str, dict[str, Any]] = {}
    scan = Scan()
    for c in repo.log(sha, since=since, until=until):
        if scan.commits >= max_commits:
            scan.truncated = True
            break
        scan.commits += 1
        rn.learn(c)
        if len(c.files) > max_files_per_commit:
            scan.skipped_mass += 1
            continue
        w_age = decay((now - c.ts) / DAY, half_life_days)
        for f in c.files:
            p = rn.resolve(f.path)
            if p not in live or not under(p, prefix) or excluded(p, globs):
                continue
            lines = 1 if f.binary else f.added + f.deleted
            s = stats.setdefault(p, {"commits": 0, "lines": 0, "w": 0.0, "authors": set(),
                                     "last": 0, "binary": False})
            s["commits"] += 1
            s["lines"] += lines
            s["w"] += lines * w_age
            s["authors"].add(c.email)
            s["last"] = max(s["last"], c.ts)
            s["binary"] = s["binary"] or f.binary
    rows: list[dict[str, Any]] = []
    for p, s in stats.items():
        n_auth = len(s["authors"])
        score = s["w"] * (1 + math.log2(n_auth))
        rows.append({"path": p, "score": score, "commits": s["commits"], "lines": s["lines"],
                     "authors": n_auth, "last_ts": s["last"], "binary": s["binary"]})
    rows.sort(key=lambda r: (-r["score"], r["path"]))
    return {"rows": rows[:limit], "files_ranked": len(rows), "scan": scan, "sha": sha,
            "half_life_days": half_life_days, "max_files_per_commit": max_files_per_commit,
            "max_commits": max_commits}


# ---------------------------------------------------------------- experts
def _bus_factor(weights: list[float]) -> int:
    """Smallest number of top contributors whose combined share is MORE than 50%."""
    total = sum(weights)
    if total <= 0:
        return 0
    cum = 0.0
    for k, w in enumerate(sorted(weights, reverse=True), 1):
        cum += w
        if cum * 2 > total:
            return k
    return len(weights)


_BLAME_HDR = re.compile(rb"^([0-9a-f]{40}|[0-9a-f]{64}) \d+ \d+")


def _blame_counts(repo: Repo, sha: str, path: str) -> Counter[str]:
    out = repo.run(["blame", "--line-porcelain", "-w", "-M", sha, "--", path], timeout=300.0)
    counts: Counter[str] = Counter()
    for line in out.split(b"\n"):
        if line[:1] == b"\t" or not line:
            continue
        m = _BLAME_HDR.match(line)
        if m:
            counts[m.group(1).decode()] += 1
    return counts


def _identities(repo: Repo, shas: list[str]) -> dict[str, tuple[str, str, int]]:
    """sha -> (canonical name, canonical email, author ts), mailmap applied."""
    if not shas:
        return {}
    out = repo.run(
        ["log", "--no-walk=unsorted", "--stdin", "--format=%H%x1f%aN%x1f%aE%x1f%at"],
        input_=("\n".join(shas) + "\n").encode(),
    )
    res: dict[str, tuple[str, str, int]] = {}
    for line in dec(out).splitlines():
        h, n, e, t = line.split("\x1f")
        res[h] = (n, e.lower(), int(t))
    return res


def file_experts(
    repo: Repo, path: str, ref: str = "HEAD", recent_days: int = 180, inactive_days: int = 365,
    as_of: str | None = None, max_files: int = 200, max_authors: int = 10,
    max_files_per_commit: int = 100,
) -> dict[str, Any]:
    """Who knows this file or directory?

    blame_share  = author's surviving lines (git blame -w -M at `ref`) / all surviving lines
    recent_share = author's lines changed in the last `recent_days` / all lines changed then
    score        = 0.6 * blame_share + 0.4 * recent_share
                   (blame_share alone when nobody touched the path recently)
    bus_factor   = fewest authors whose blame shares sum to MORE than 50%
    """
    sha = repo.resolve(ref)
    now = now_ts(as_of)
    p = norm_path(repo, path)
    spec = [p] if p else []
    listing = repo.run(["ls-tree", "-r", "-z", "-l", sha] + (["--", p] if p else []))
    blobs: list[str] = []
    skipped_nonutf8 = 0
    for ent in listing.split(b"\x00"):
        if not ent:
            continue
        meta, _, raw_name = ent.partition(b"\t")
        if meta.split()[1] == b"blob":
            try:
                raw_name.decode("utf-8")
            except UnicodeDecodeError:
                skipped_nonutf8 += 1  # cannot be passed back to git through argv portably
                continue
            blobs.append(dec(raw_name))
    if not blobs and not skipped_nonutf8:
        raise GitError(
            f"no files at {p or '(repo root)'!r} in {ref} - give a path relative to the "
            "repository root, e.g. src/app/main.py or src/app"
        )
    empty = repo.run(["hash-object", "-t", "tree", "--stdin"], input_=b"").decode().strip()
    nums = repo.run(["diff", "--numstat", "-z", empty, sha] + (["--", p] if p else []))
    loc: dict[str, int] = {}
    binary: set[str] = set()
    for tok in nums.split(b"\x00"):
        if not tok:
            continue
        a, _, raw_name = tok.split(b"\t", 2)
        if a == b"-":
            binary.add(dec(raw_name))
        else:
            loc[dec(raw_name)] = int(a)
    text_files = sorted((f for f in blobs if f not in binary), key=lambda f: (-loc.get(f, 0), f))
    skipped_binary = len(blobs) - len(text_files)
    chosen = text_files[:max_files]
    skipped_cap = len(text_files) - len(chosen)

    blame_total: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for counts in pool.map(lambda f: _blame_counts(repo, sha, f), chosen):
            blame_total.update(counts)
    ident = _identities(repo, list(blame_total))
    lines_by: Counter[str] = Counter()
    names: dict[str, Counter[str]] = defaultdict(Counter)
    last_by: dict[str, int] = {}
    for h, n in blame_total.items():
        name, email, ts = ident[h]
        lines_by[email] += n
        names[email][name] += n
        last_by[email] = max(last_by.get(email, 0), ts)
    total_lines = sum(lines_by.values())

    cutoff = now - recent_days * DAY
    since_arg = datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")
    rec_lines: Counter[str] = Counter()
    rec_commits: Counter[str] = Counter()
    for c in repo.log(sha, since=since_arg, paths=spec or None):
        if c.ts < cutoff or len(c.files) > max_files_per_commit:
            continue
        rec_commits[c.email] += 1
        rec_lines[c.email] += sum(1 if f.binary else f.added + f.deleted for f in c.files)
        last_by[c.email] = max(last_by.get(c.email, 0), c.ts)
        names[c.email][c.author] += 0
    rec_total = sum(rec_lines.values())

    rows: list[dict[str, Any]] = []
    for email in set(lines_by) | set(rec_lines):
        bs = lines_by[email] / total_lines if total_lines else 0.0
        rs = rec_lines[email] / rec_total if rec_total else 0.0
        score = (0.6 * bs + 0.4 * rs) if rec_total else bs
        rows.append({
            "name": names[email].most_common(1)[0][0], "email": email,
            "lines": lines_by[email], "blame_share": bs, "recent_lines": rec_lines[email],
            "recent_commits": rec_commits[email], "recent_share": rs, "score": score,
            "last_ts": last_by.get(email, 0),
            "inactive": (now - last_by.get(email, 0)) / DAY > inactive_days,
        })
    rows.sort(key=lambda r: (-r["score"], r["email"]))
    k = _bus_factor([float(v) for v in lines_by.values()])
    inactive_share = sum(r["blame_share"] for r in rows if r["inactive"])
    return {"path": p or ".", "files": len(chosen), "files_total": len(blobs) + skipped_nonutf8,
            "skipped_binary": skipped_binary, "skipped_nonutf8": skipped_nonutf8, "skipped_cap": skipped_cap, "lines": total_lines,
            "rows": rows[:max_authors], "authors": len(rows), "bus_factor": k,
            "inactive_share": inactive_share, "recent_days": recent_days,
            "inactive_days": inactive_days, "sha": sha}


# ---------------------------------------------------------------- coupling
def change_coupling(
    repo: Repo, path: str, ref: str = "HEAD", since: str | None = None, min_support: int = 2,
    min_confidence: float = 0.2, max_files_per_commit: int = 50, limit: int = 20,
    max_commits: int = 20000,
) -> dict[str, Any]:
    """Files that change in the same commits as `path`.

    support       = commits touching both files
    confidence    = support / commits touching `path`   (P(other | path))
    reverse_conf  = support / commits touching `other`  (P(path | other))
    Commits touching more than max_files_per_commit files are ignored (mass
    reformats and merges of vendor drops say nothing about real coupling).
    Renames are followed, so history under an old name counts for the new one.
    """
    sha = repo.resolve(ref)
    target = norm_path(repo, path)
    if not target:
        raise GitError("path is required - pass the file whose co-changing files you want")
    live = head_files(repo, sha)
    if target not in live:
        raise GitError(
            f"{target!r} is not a file at {ref}. change_coupling works on one file; give a "
            "path relative to the repository root"
        )
    rn = RenameMap()
    totals: Counter[str] = Counter()
    co: Counter[str] = Counter()
    n_target = 0
    scan = Scan()
    for c in repo.log(sha, since=since):
        if scan.commits >= max_commits:
            scan.truncated = True
            break
        scan.commits += 1
        rn.learn(c)
        if len(c.files) > max_files_per_commit:
            scan.skipped_mass += 1
            continue
        files = {rn.resolve(f.path) for f in c.files}
        totals.update(files)
        if target in files:
            n_target += 1
            co.update(files - {target})
    if n_target == 0:
        raise GitError(f"no usable commits touch {target!r} in the scanned range")
    rows: list[dict[str, Any]] = []
    for other, sup in co.items():
        if other not in live or sup < min_support:
            continue
        conf = sup / n_target
        if conf < min_confidence:
            continue
        rows.append({"path": other, "support": sup, "confidence": conf,
                     "reverse_confidence": sup / totals[other], "commits": totals[other]})
    rows.sort(key=lambda r: (-r["confidence"], -r["support"], r["path"]))
    return {"target": target, "target_commits": n_target, "rows": rows[:limit],
            "matches": len(rows), "scan": scan, "max_files_per_commit": max_files_per_commit,
            "max_commits": max_commits, "sha": sha}


# ---------------------------------------------------------------- bus factor
def bus_factor(
    repo: Repo, ref: str = "HEAD", path: str = "", depth: int = 1, since: str | None = None,
    half_life_days: float = 365.0, min_commits: int = 3, max_files_per_commit: int = 100,
    limit: int = 30, as_of: str | None = None, max_commits: int = 20000,
) -> dict[str, Any]:
    """Bus factor per directory, from history.

    Each author's weight in a directory is
        sum over their commits of lines(c in dir) * 0.5 ** (age / half_life_days)
    bus_factor(dir) = fewest authors whose weights sum to MORE than 50%.
    Only files that still exist at `ref` count. Directories are the first
    `depth` path components (files at the root form '.').
    """
    sha = repo.resolve(ref)
    now = now_ts(as_of)
    prefix = norm_path(repo, path)
    live = head_files(repo, sha)
    rn = RenameMap()
    dirs: dict[str, dict[str, Any]] = {}
    scan = Scan()
    for c in repo.log(sha, since=since):
        if scan.commits >= max_commits:
            scan.truncated = True
            break
        scan.commits += 1
        rn.learn(c)
        if len(c.files) > max_files_per_commit:
            scan.skipped_mass += 1
            continue
        w_age = decay((now - c.ts) / DAY, half_life_days)
        touched: set[str] = set()
        for f in c.files:
            p = rn.resolve(f.path)
            if p not in live or not under(p, prefix):
                continue
            parts = p.split("/")[:-1][:depth]
            d = "/".join(parts) if parts else "."
            e = dirs.setdefault(d, {"w": defaultdict(float), "commits": 0, "names": {},
                                    "last": {}})
            lines = 1 if f.binary else f.added + f.deleted
            e["w"][c.email] += lines * w_age
            e["names"].setdefault(c.email, c.author)
            e["last"][c.email] = max(e["last"].get(c.email, 0), c.ts)
            touched.add(d)
        for d in touched:
            dirs[d]["commits"] += 1
    rows: list[dict[str, Any]] = []
    for d, e in dirs.items():
        weights = e["w"]
        total = sum(weights.values())
        if total <= 0:
            continue
        top_email = max(weights, key=lambda k: (weights[k], k))
        rows.append({
            "dir": d, "bus_factor": _bus_factor(list(weights.values())),
            "authors": len(weights), "commits": e["commits"],
            "top_author": e["names"][top_email], "top_share": weights[top_email] / total,
            "top_last_ts": e["last"][top_email],
            "at_risk": _bus_factor(list(weights.values())) == 1 and e["commits"] >= min_commits,
        })
    rows.sort(key=lambda r: (r["bus_factor"], -r["top_share"], r["dir"]))
    return {"rows": rows[:limit], "dirs": len(rows), "scan": scan, "min_commits": min_commits,
            "half_life_days": half_life_days, "max_commits": max_commits, "sha": sha}


# ---------------------------------------------------------------- timeline
SPARK = "_.:-=+*#%@"


def sparkline(values: list[int]) -> str:
    """ASCII sparkline: '_' is zero, then eight rising levels up to '@' (the max)."""
    top = max(values, default=0)
    if top == 0:
        return "_" * len(values)
    out = []
    for v in values:
        if v == 0:
            out.append("_")
        else:
            out.append(SPARK[1 + min(8, int(v * 9 / top - 1e-9))] if top else "_")
    return "".join(out)


def _bucket(ts: int, period: str) -> date:
    d = datetime.fromtimestamp(ts, timezone.utc).date()
    if period == "week":
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def churn_timeline(
    repo: Repo, ref: str = "HEAD", period: str = "month", since: str | None = None,
    path: str = "", author: str | None = None, max_commits: int = 20000, max_buckets: int = 400,
) -> dict[str, Any]:
    """Commits and changed lines per week (Monday start) or month, UTC, merges excluded."""
    if period not in ("week", "month"):
        raise GitError("period must be 'week' or 'month'")
    sha = repo.resolve(ref)
    prefix = norm_path(repo, path)
    buckets: dict[date, list[int]] = {}
    scan = Scan()
    for c in repo.log(sha, since=since, paths=[prefix] if prefix else None,
                      extra=[f"--author={author}", "--fixed-strings", "-i"] if author else None):
        if scan.commits >= max_commits:
            scan.truncated = True
            break
        scan.commits += 1
        b = buckets.setdefault(_bucket(c.ts, period), [0, 0, 0])
        b[0] += 1
        for f in c.files:
            b[1] += f.added
            b[2] += f.deleted
    if not buckets:
        raise GitError("no commits match (check since=, path= and author=)")
    first, last = min(buckets), max(buckets)
    series: list[tuple[date, int, int, int]] = []
    cur = first
    while cur <= last:
        v = buckets.get(cur, [0, 0, 0])
        series.append((cur, v[0], v[1], v[2]))
        if len(series) > max_buckets:
            raise GitError(
                f"more than {max_buckets} {period}s in range - use period='month' or set since="
            )
        cur = cur + timedelta(days=7) if period == "week" else (
            date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1))
    return {"series": series, "period": period, "scan": scan, "max_commits": max_commits,
            "commit_spark": sparkline([s[1] for s in series]),
            "line_spark": sparkline([s[2] + s[3] for s in series])}


# ---------------------------------------------------------------- summary
def repo_summary(repo: Repo, ref: str = "HEAD", max_commits: int = 10000) -> dict[str, Any]:
    sha = repo.resolve(ref)
    total = int(repo.text(["rev-list", "--count", sha]).strip())
    merges = int(repo.text(["rev-list", "--count", "--merges", sha]).strip())
    roots = repo.text(["rev-list", "--max-parents=0", "--format=%at", sha]).split()
    first_ts = min(int(x) for x in roots if x.isdigit() and len(x) < 12)
    last_ts = int(repo.text(["log", "-1", "--format=%at", sha]).strip())
    authors: Counter[str] = Counter()
    names: dict[str, str] = {}
    lang: Counter[str] = Counter()
    scan = Scan()
    for c in repo.log(sha, max_count=max_commits + 1):
        if scan.commits >= max_commits:
            scan.truncated = True
            break
        scan.commits += 1
        authors[c.email] += 1
        names.setdefault(c.email, c.author)
        for f in c.files:
            if excluded(f.path, list(DEFAULT_EXCLUDES)):
                continue  # lock files would swamp the language mix
            ext = os.path.splitext(f.path)[1].lower()
            lang[LANGUAGES.get(ext, "Other")] += 0 if f.binary else f.added + f.deleted
    refs = repo.text(["for-each-ref", "--format=%(refname)%1f%(committerdate:unix)%1f%(creatordate:unix)",
                      "--sort=-committerdate", "refs/heads", "refs/remotes", "refs/tags"])
    heads: list[tuple[str, int]] = []
    remotes = 0
    tags: list[tuple[str, int]] = []
    for line in refs.splitlines():
        name, cts, crt = line.split("\x1f")
        if name.startswith("refs/heads/"):
            heads.append((name[11:], int(cts or 0)))
        elif name.startswith("refs/remotes/"):
            if not name.endswith("/HEAD"):
                remotes += 1
        elif name.startswith("refs/tags/"):
            tags.append((name[10:], int(crt or cts or 0)))
    tags.sort(key=lambda t: -t[1])
    lang_total = sum(lang.values())
    return {
        "path": repo.path, "bare": repo.bare, "shallow": repo.shallow, "sha": sha,
        "commits": total, "merges": merges, "first_ts": first_ts, "last_ts": last_ts,
        "authors": [(names[e], e, n) for e, n in authors.most_common(10)],
        "author_count": len(authors),
        "languages": [(k, v, v / lang_total if lang_total else 0.0)
                      for k, v in lang.most_common(8) if v > 0],
        "branches": heads, "remote_branches": remotes, "tags": tags, "scan": scan,
        "max_commits": max_commits,
    }


# ---------------------------------------------------------------- release notes
TYPE_TITLES = {
    "feat": "Features", "fix": "Bug fixes", "perf": "Performance", "refactor": "Refactoring",
    "docs": "Documentation", "test": "Tests", "build": "Build", "ci": "CI", "chore": "Chores",
    "style": "Style", "revert": "Reverts",
}
_CC = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
_PR_TAIL = re.compile(r"\(#(\d+)\)\s*$")
_PR_MERGE = re.compile(r"^Merge pull request #(\d+) from (\S+)")


def _latest_tag_before(repo: Repo, to_sha: str) -> str | None:
    for start in (to_sha, to_sha + "^"):
        try:
            out = repo.text(["describe", "--tags", "--abbrev=0", start]).strip()
        except GitError:
            return None
        tag_sha = repo.text(["rev-parse", "--verify", out + "^{commit}"]).strip()
        if tag_sha != to_sha:
            return out
    return None


def release_notes(
    repo: Repo, to_ref: str = "HEAD", from_ref: str | None = None, group_by: str = "type",
    max_commits: int = 500,
) -> dict[str, Any]:
    """Group the commits in from_ref..to_ref by conventional-commit type or by area."""
    if group_by not in ("type", "area"):
        raise GitError("group_by must be 'type' or 'area'")
    to_sha = repo.resolve(to_ref)
    from_name = from_ref
    if from_ref is None:
        from_name = _latest_tag_before(repo, to_sha)
    rev = to_sha
    from_sha = None
    if from_name:
        from_sha = repo.resolve(from_name)
        rev = f"{from_sha}..{to_sha}"
    entries: list[dict[str, Any]] = []
    merges: list[dict[str, Any]] = []
    breaking: list[dict[str, Any]] = []
    authors: Counter[str] = Counter()
    names: dict[str, str] = {}
    truncated = False
    first_ts = last_ts = 0
    for c in repo.log(rev, merges="include", body=True):
        if len(entries) + len(merges) >= max_commits:
            truncated = True
            break
        last_ts = last_ts or c.ts
        first_ts = c.ts
        pr = None
        if c.is_merge:
            m = _PR_MERGE.match(c.subject)
            title = c.body.splitlines()[0] if (m and c.body) else c.subject
            merges.append({"sha": c.sha[:7], "pr": int(m.group(1)) if m else None,
                           "title": title, "branch": m.group(2) if m else None})
            continue
        authors[c.email] += 1
        names.setdefault(c.email, c.author)
        tail = _PR_TAIL.search(c.subject)
        if tail:
            pr = int(tail.group(1))
        cc = _CC.match(c.subject)
        ctype = cc.group("type").lower() if cc else None
        is_breaking = bool(cc and cc.group("bang")) or bool(
            re.search(r"^BREAKING[ -]CHANGE:", c.body, re.M))
        desc = cc.group("desc") if cc else c.subject
        lines_by_area: Counter[str] = Counter()
        for f in c.files:
            parts = f.path.split("/")
            lines_by_area["/".join(parts[:1]) if len(parts) > 1 else "(repo root)"] += (
                f.added + f.deleted + 1)
        area = (sorted(lines_by_area.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                if lines_by_area else "(no file changes)")
        e = {"sha": c.sha[:7], "type": ctype if ctype in TYPE_TITLES else None,
             "scope": cc.group("scope") if cc and cc.group("scope") else None,
             "desc": desc, "pr": pr, "area": area, "author": c.author, "breaking": is_breaking}
        entries.append(e)
        if is_breaking:
            note = re.search(r"^BREAKING[ -]CHANGE:\s*(.+)$", c.body, re.M)
            breaking.append({**e, "note": note.group(1) if note else desc})
    groups: dict[str, list[dict[str, Any]]] = {}
    if group_by == "type":
        order = list(TYPE_TITLES.values()) + ["Other changes"]
        for e in entries:
            groups.setdefault(TYPE_TITLES.get(e["type"] or "", "Other changes"), []).append(e)
        groups = {k: groups[k] for k in order if k in groups}
    else:
        for e in entries:
            groups.setdefault(e["area"], []).append(e)
        groups = dict(sorted(groups.items()))
    return {"from": from_name, "to": to_ref, "to_sha": to_sha, "from_sha": from_sha,
            "groups": groups, "merges": merges, "breaking": breaking,
            "contributors": [(names[e], n) for e, n in authors.most_common()],
            "commits": len(entries) + len(merges), "truncated": truncated,
            "max_commits": max_commits, "group_by": group_by,
            "first_ts": first_ts, "last_ts": last_ts}


# ---------------------------------------------------------------- search & history
def commit_search(
    repo: Repo, ref: str = "HEAD", query: str | None = None, author: str | None = None,
    path: str = "", since: str | None = None, until: str | None = None,
    content: str | None = None, include_merges: bool = False, limit: int = 20,
) -> dict[str, Any]:
    """Case-insensitive literal search over message, author, path, dates, and added/removed text."""
    p = norm_path(repo, path)
    if not any([query, author, p, since, until, content]):
        raise GitError("give at least one filter: query, author, path, since, until or content")
    if not 1 <= limit <= 200:
        raise GitError("limit must be between 1 and 200")
    sha = repo.resolve(ref)
    extra = ["--fixed-strings", "-i"]
    if query:
        extra.append(f"--grep={query}")
    if author:
        extra.append(f"--author={author}")
    if content:
        extra.append(f"-S{content}")
    rows: list[Commit] = []
    more = False
    for c in repo.log(sha, numstat=False, merges="include" if include_merges else "exclude",
                      since=since, until=until, paths=[p] if p else None, extra=extra):
        if len(rows) >= limit:
            more = True
            break
        rows.append(c)
    return {"rows": rows, "more": more, "limit": limit, "sha": sha}


def file_history(repo: Repo, path: str, ref: str = "HEAD", limit: int = 30) -> dict[str, Any]:
    """Commits that touched `path`, following it across renames."""
    p = norm_path(repo, path)
    if not p:
        raise GitError("path is required")
    if not 1 <= limit <= 500:
        raise GitError("limit must be between 1 and 500")
    sha = repo.resolve(ref)
    rows: list[dict[str, Any]] = []
    names: list[str] = [p]
    more = False
    for c in repo.log(sha, extra=["--follow"], paths=[p]):
        if len(rows) >= limit:
            more = True
            break
        f = c.files[0] if c.files else None
        renamed = f.old_path if f and f.old_path else None
        if renamed and renamed not in names:
            names.append(renamed)
        rows.append({"sha": c.sha[:7], "ts": c.ts, "author": c.author, "subject": c.subject,
                     "added": f.added if f else 0, "deleted": f.deleted if f else 0,
                     "binary": bool(f and f.binary), "renamed_from": renamed})
    if not rows:
        raise GitError(
            f"no history for {p!r} at {ref} - paths are relative to the repository root and use "
            "forward slashes"
        )
    return {"path": p, "rows": rows, "names": names, "more": more, "limit": limit, "sha": sha}
