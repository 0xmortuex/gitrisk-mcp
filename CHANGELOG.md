# Changelog

## 0.1.0 - 2026-11-06

First release.

- Nine read-only tools: `repo_summary`, `hotspots`, `file_experts`, `change_coupling`,
  `bus_factor`, `churn_timeline`, `release_notes`, `commit_search`, `file_history`.
- Blame-based expertise (surviving lines, whitespace and moved lines ignored) blended with
  recent activity; inactive-owner flag; bus factor as "fewest authors above 50%".
- Rename following, `.mailmap` support, mass-commit and lock-file filtering, merge-commit handling.
- `--dir` sandbox, shallow-clone warning, bare repositories, non-UTF-8 path names, binary files.
- Streaming `git log -z --numstat` with `max_commits` limits.
