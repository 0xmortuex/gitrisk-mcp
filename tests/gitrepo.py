"""Build real git repositories with scripted histories (explicit identities and dates)."""

from __future__ import annotations

import hashlib
import os
import random
import subprocess
from dataclasses import dataclass

ALICE = ("Alice", "alice@x.io")
BOB = ("Bob", "bob@x.io")
BOBBY = ("Bobby", "bob@old.example")  # mapped to Bob by .mailmap
CAROL = ("Carol", "carol@x.io")


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def git(cwd: str, *args: str, env: dict[str, str] | None = None, input_: bytes | None = None) -> str:
    cmd = ["git", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *args]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, env=env or clean_env(),
                          input=input_, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode("utf-8", "replace")


class Builder:
    """Append-only edits make blame and numstat numbers exact and easy to reason about."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(path, exist_ok=True)
        git(path, "init", "-q")
        git(path, "config", "user.name", "Setup")
        git(path, "config", "user.email", "setup@x.io")
        git(path, "config", "commit.gpgsign", "false")
        git(path, "config", "core.autocrlf", "false")
        self.n = 0

    def append(self, rel: str, lines: int, tag: str) -> None:
        full = os.path.join(self.path, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "ab") as fh:
            for _ in range(lines):
                self.n += 1
                fh.write(f"{rel}:{tag}:{self.n}\n".encode())

    def write(self, rel: str, data: bytes) -> None:
        full = os.path.join(self.path, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)

    def commit(self, who: tuple[str, str], date: str, message: str, body: str | None = None,
               appends: dict[str, int] | None = None) -> str:
        for rel, n in (appends or {}).items():
            self.append(rel, n, who[0])
        env = clean_env()
        env.update(GIT_AUTHOR_NAME=who[0], GIT_AUTHOR_EMAIL=who[1], GIT_AUTHOR_DATE=date,
                   GIT_COMMITTER_NAME=who[0], GIT_COMMITTER_EMAIL=who[1],
                   GIT_COMMITTER_DATE=date)
        git(self.path, "add", "-A", env=env)
        args = ["commit", "-q", "-m", message]
        if body:
            args += ["-m", body]
        git(self.path, *args, env=env)
        return git(self.path, "rev-parse", "HEAD").strip()

    def merge(self, who: tuple[str, str], date: str, branch: str, message: str,
              body: str | None = None) -> None:
        env = clean_env()
        env.update(GIT_AUTHOR_NAME=who[0], GIT_AUTHOR_EMAIL=who[1], GIT_AUTHOR_DATE=date,
                   GIT_COMMITTER_NAME=who[0], GIT_COMMITTER_EMAIL=who[1],
                   GIT_COMMITTER_DATE=date)
        args = ["merge", "--no-ff", "-q", branch, "-m", message]
        if body:
            args += ["-m", body]
        git(self.path, *args, env=env)


AS_OF = "2026-03-01T00:00:00"


def build_acme(path: str) -> Builder:
    """The reference history. All numbers asserted in the tests derive from this script."""
    b = Builder(path)
    b.write(".mailmap", b"Bob <bob@x.io> Bobby <bob@old.example>\n")
    b.commit(ALICE, "2026-01-05T10:00:00+0000", "feat: initial engine",
             appends={"core/engine.py": 10, "core/util.py": 4, "docs/readme.md": 2})
    b.commit(BOB, "2026-01-12T10:00:00+0000", "fix(core): handle edge case (#12)",
             appends={"core/engine.py": 5, "core/util.py": 2})
    b.commit(ALICE, "2026-01-19T10:00:00+0000", "feat(api): add routes (#13)",
             appends={"core/engine.py": 3, "api/routes.py": 6})
    git(path, "tag", "v0.1")
    b.commit(BOBBY, "2026-02-02T10:00:00+0000", "refactor: tidy engine",
             appends={"core/engine.py": 2, "core/util.py": 1})
    b.commit(CAROL, "2026-02-16T10:00:00+0000", "docs: explain routes",
             appends={"api/routes.py": 4})
    b.commit(ALICE, "2026-02-23T10:00:00+0000", "chore: bump deps",
             appends={"core/engine.py": 1, "core/util.py": 1, "uv.lock": 100})
    mass = {f"vendor/f{i:02d}.txt": 1 for i in range(12)}
    mass["core/engine.py"] = 1
    b.commit(CAROL, "2026-02-24T10:00:00+0000", "style: reformat everything", appends=mass)
    b.commit(BOB, "2026-02-25T10:00:00+0000", "feat(team): add a", appends={"team/a.py": 5})
    b.commit(CAROL, "2026-02-26T10:00:00+0000", "feat(team): add b", appends={"team/b.py": 5})
    return b


def build_release_fixture(path: str) -> Builder:
    """Merge commits, PR numbers and a breaking change."""
    b = Builder(path)
    b.commit(ALICE, "2026-01-05T10:00:00+0000", "feat: base", appends={"app/main.py": 3})
    git(path, "tag", "v1.0.0")
    git(path, "checkout", "-q", "-b", "feat-x")
    b.commit(ALICE, "2026-01-10T10:00:00+0000", "feat(api): add x endpoint",
             appends={"api/x.py": 3})
    git(path, "checkout", "-q", "main")
    b.commit(BOB, "2026-01-11T10:00:00+0000", "fix!: drop legacy flag",
             body="BREAKING CHANGE: the legacy flag was removed",
             appends={"app/main.py": 1})
    b.merge(CAROL, "2026-01-12T10:00:00+0000", "feat-x",
            "Merge pull request #21 from alice/feat-x", body="Add x endpoint")
    b.commit(CAROL, "2026-01-13T10:00:00+0000", "perf(app): faster startup (#22)",
             appends={"app/main.py": 2})
    b.commit(BOB, "2026-01-14T10:00:00+0000", "update the readme a bit",
             appends={"README.md": 1})
    return b


@dataclass
class FI:
    author: tuple[str, str]
    ts: int
    message: str
    files: list[tuple[bytes, bytes]]  # (raw path bytes, content)


def fast_import(path: str, commits: list[FI]) -> None:
    """Create a repo from a stream: raw byte paths and fast bulk history."""
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "--bare")
    out = bytearray()
    for c in commits:
        who = f"{c.author[0]} <{c.author[1]}> {c.ts} +0000".encode()
        msg = c.message.encode()
        out += b"commit refs/heads/main\n"
        out += b"author " + who + b"\ncommitter " + who + b"\n"
        out += b"data %d\n" % len(msg) + msg + b"\n"
        for p, data in c.files:
            out += b"M 100644 inline " + p + b"\n"
            out += b"data %d\n" % len(data) + data + b"\n"
        out += b"\n"
    git(path, "fast-import", "--quiet", input_=bytes(out))


def big_history(n: int = 20000, files: int = 300, seed: int = 7) -> list[FI]:
    rnd = random.Random(seed)
    people = [(f"Dev{i}", f"dev{i}@x.io") for i in range(12)]
    revs = [0] * files
    ts = 1_640_995_200  # 2022-01-01
    commits = []
    for i in range(n):
        ts += rnd.randint(1000, 6000)
        k = 60 if i % 2500 == 2499 else rnd.randint(1, 3)
        picks = rnd.sample(range(files), k)
        flist = []
        for f in picks:
            revs[f] += 1
            body = "".join(f"l{j}\n" for j in range(min(revs[f], 80)))
            flist.append((f"mod{f % 15}/file{f}.py".encode(), (body + f"r{revs[f]}\n").encode()))
        commits.append(FI(rnd.choice(people), ts, f"fix: change {i}", flist))
    return commits


def snapshot(repo_dir: str) -> str:
    """Hash every file under the repo (git dir and working tree): proves nothing changed."""
    h = hashlib.sha256()
    for root, dirs, names in os.walk(repo_dir):
        dirs.sort()
        for n in sorted(names):
            full = os.path.join(root, n)
            h.update(os.path.relpath(full, repo_dir).encode())
            with open(full, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()
