"""Thin, read-only wrapper around the real `git` binary.

Rules this module enforces:

* every command is an argument list (never a shell string);
* only read-only git subcommands are ever issued (log, blame, rev-parse,
  rev-list, ls-tree, diff, for-each-ref, shortlog);
* GIT_OPTIONAL_LOCKS=0 so even a stat-refresh can not write the index;
* repositories must live under a `--dir` root (the sandbox).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_S = 120.0


class GitError(Exception):
    """A failure the agent can act on (bad ref, missing repo, sandbox, timeout)."""


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int
    deleted: int
    binary: bool = False
    old_path: str | None = None  # set when git detected a rename/copy


@dataclass
class Commit:
    sha: str
    ts: int
    author: str
    email: str
    parents: tuple[str, ...]
    subject: str
    body: str = ""
    files: list[FileChange] = field(default_factory=list)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


def dec(raw: bytes) -> str:
    """Decode git output; non-UTF-8 bytes become visible \\xNN escapes (never lost, never merged)."""
    return raw.decode("utf-8", "backslashreplace")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"  # never write the index, even to refresh stat info
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_LITERAL_PATHSPECS"] = "1"  # paths are paths, not globs
    env["GIT_PAGER"] = "cat"
    return env


# Applied to every call: neutralise user/repo config that would change the
# output format or run external programs.
_BASE = [
    "git",
    "-c", "core.quotepath=off",
    "-c", "core.fsmonitor=false",
    "-c", "core.autocrlf=false",
    "-c", "log.showSignature=false",
    "-c", "color.ui=never",
    "-c", "i18n.logOutputEncoding=UTF-8",
]


def _run(args: list[str], cwd: str | None, input_: bytes | None, timeout: float) -> bytes:
    cmd = _BASE + (["-C", cwd] if cwd else []) + args
    try:
        proc = subprocess.run(
            cmd, input=input_, capture_output=True, timeout=timeout, env=_env(), check=False
        )
    except FileNotFoundError as e:
        raise GitError("the `git` executable was not found on PATH - install git") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0]} timed out after {timeout:.0f}s - narrow the request") from e
    if proc.returncode != 0:
        err = dec(proc.stderr).strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {' '.join(args[:2])} failed: {err}")
    return proc.stdout


COMMIT_FMT = "%x1e%H%x1f%at%x1f%aN%x1f%aE%x1f%P%x1f%s"


def _parse_record(rec: bytes, with_body: bool) -> Commit:
    head, _, rest = rec.partition(b"\x00")
    fields = dec(head).split("\x1f")
    sha, ts, name, email, parents, subject = fields[:6]
    body = fields[6].strip() if with_body and len(fields) > 6 else ""
    files: list[FileChange] = []
    toks = rest.lstrip(b"\n").split(b"\x00")
    i = 0
    while i < len(toks):
        tok = toks[i]
        if not tok:
            i += 1
            continue
        a, d, p = tok.split(b"\t", 2)
        binary = a == b"-"
        added = 0 if binary else int(a)
        deleted = 0 if binary else int(d)
        if p == b"":  # rename/copy: next two tokens are old and new path
            files.append(FileChange(dec(toks[i + 2]), added, deleted, binary, dec(toks[i + 1])))
            i += 3
        else:
            files.append(FileChange(dec(p), added, deleted, binary))
            i += 1
    return Commit(
        sha, int(ts), name, email.lower(), tuple(parents.split()), subject, body, files
    )


class Repo:
    """An opened repository (working tree or bare)."""

    def __init__(self, path: str, bare: bool, shallow: bool) -> None:
        self.path = path
        self.bare = bare
        self.shallow = shallow

    # -- plain commands -------------------------------------------------
    def run(self, args: list[str], input_: bytes | None = None,
            timeout: float = DEFAULT_TIMEOUT_S) -> bytes:
        return _run(args, self.path, input_, timeout)

    def text(self, args: list[str], input_: bytes | None = None) -> str:
        return dec(self.run(args, input_))

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.shallow:
            out.append(
                "WARNING: this is a shallow clone - history before the shallow boundary is "
                "missing, so ages, authorship and churn are understated. Run "
                "`git fetch --unshallow` in the repo for full results."
            )
        return out

    # -- refs -----------------------------------------------------------
    def resolve(self, ref: str) -> str:
        """Resolve a ref to a commit sha; refuses anything that looks like an option."""
        if not ref or ref.startswith("-"):
            raise GitError(f"invalid ref {ref!r}: refs must not be empty or start with '-'")
        try:
            out = self.text(["rev-parse", "--verify", "--quiet", "--end-of-options",
                             ref + "^{commit}"])
        except GitError as e:
            raise GitError(
                f"unknown ref {ref!r} in {self.path} - use a branch, tag or commit id "
                "(an empty repository has no HEAD yet)"
            ) from e
        return out.strip()

    # -- streaming log --------------------------------------------------
    def log(
        self,
        rev: str = "HEAD",
        *,
        numstat: bool = True,
        body: bool = False,
        merges: str = "exclude",  # exclude | include | only
        since: str | None = None,
        until: str | None = None,
        paths: list[str] | None = None,
        max_count: int | None = None,
        extra: list[str] | None = None,
        timeout: float = 300.0,
    ) -> Iterator[Commit]:
        """Stream commits newest-first, parsing as the bytes arrive."""
        args =["log", "-z", "-M", "--no-color", "--date-order"]
        fmt = COMMIT_FMT + ("%x1f%b" if body else "")
        args.append("--format=" + fmt)
        if numstat:
            args.append("--numstat")
        if merges == "exclude":
            args.append("--no-merges")
        elif merges == "only":
            args.append("--merges")
        if since:
            args.append(f"--since={since}")
        if until:
            args.append(f"--until={until}")
        if max_count is not None:
            args.append(f"--max-count={max_count}")
        args += extra or []
        args.append(rev)
        if paths:
            args += ["--"] + paths
        yield from self._stream(args, body, timeout)

    def _stream(self, args: list[str], with_body: bool, timeout: float) -> Iterator[Commit]:
        cmd = _BASE + ["-C", self.path] + args
        deadline = time.monotonic() + timeout
        with tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=err, env=_env()
                )
            except FileNotFoundError as e:
                raise GitError("the `git` executable was not found on PATH") from e
            assert proc.stdout is not None
            finished = False
            try:
                buf = b""
                while True:
                    if time.monotonic() > deadline:
                        raise GitError(
                            f"git log timed out after {timeout:.0f}s - narrow with since=, "
                            "path= or max_commits="
                        )
                    chunk = proc.stdout.read1(1 << 16)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    buf += chunk
                    parts = buf.split(b"\x1e")
                    buf = parts.pop()
                    for rec in parts:
                        if rec:
                            yield _parse_record(rec, with_body)
                if buf:
                    yield _parse_record(buf, with_body)
                finished = True
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()
            if finished and proc.returncode != 0:
                err.seek(0)
                msg = dec(err.read()).strip() or f"exit code {proc.returncode}"
                raise GitError(f"git log failed: {msg}")


class Sandbox:
    """The set of directory roots the server may read repositories from."""

    def __init__(self, roots: list[str]) -> None:
        if not roots:
            roots = [os.getcwd()]
        self.roots = [self._norm(r) for r in roots]

    @staticmethod
    def _norm(p: str) -> str:
        return os.path.normcase(os.path.realpath(p))

    def _inside(self, p: str) -> bool:
        n = self._norm(p)
        for root in self.roots:
            try:
                if os.path.commonpath([n, root]) == root:
                    return True
            except ValueError:  # different drives on Windows
                continue
        return False

    def open(self, path: str | None) -> Repo:
        if not path:
            path = self.roots[0]
        if not os.path.isdir(path):
            raise GitError(f"{path!r} is not a directory")
        if not self._inside(path):
            raise GitError(
                f"{path!r} is outside the allowed directories "
                f"({', '.join(self.roots)}). Start the server with --dir <path> to allow it."
            )
        info = _run(
            ["rev-parse", "--is-bare-repository", "--is-shallow-repository", "--absolute-git-dir"],
            path, None, 30.0,
        ) if _is_repo(path) else b""
        if not info:
            raise GitError(f"{path!r} is not inside a git repository")
        lines = dec(info).splitlines()
        bare = lines[0] == "true"
        shallow = lines[1] == "true"
        if bare:
            top = lines[2]
        else:
            top = dec(_run(["rev-parse", "--show-toplevel"], path, None, 30.0)).strip()
        if not self._inside(top):
            raise GitError(
                f"{path!r} belongs to the repository at {top!r}, which is outside the allowed "
                "directories. Start the server with --dir pointing at the repository root."
            )
        return Repo(top, bare, shallow)


def _is_repo(path: str) -> bool:
    try:
        _run(["rev-parse", "--git-dir"], path, None, 30.0)
    except GitError:
        return False
    return True
