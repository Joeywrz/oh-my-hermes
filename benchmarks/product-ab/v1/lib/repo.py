"""Read-only git plumbing plus the disposable candidate workspace.

Every fact about a pull request's tree comes from the local object store, not
from the network: the corpus pins digests at build time and the run re-derives
the same bytes from git, so a run needs no GitHub access at all.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
import hashlib
from pathlib import Path
import shutil
import subprocess

GIT_TIMEOUT_SECONDS = 120


class GitError(RuntimeError):
    """A git command this lane needs did not succeed."""


def git(repo: Path, *arguments: str, timeout: int = GIT_TIMEOUT_SECONDS) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode:
        raise GitError(
            f"git {' '.join(arguments[:3])} failed with exit {completed.returncode}: "
            f"{completed.stderr.strip()[:400]}"
        )
    return completed.stdout


def git_ok(repo: Path, *arguments: str, timeout: int = GIT_TIMEOUT_SECONDS) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    return completed.returncode == 0


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def parents(repo: Path, commit: str) -> list[str]:
    line = git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    return line[1:]


def merge_base_of(repo: Path, merge_commit: str) -> str:
    """The commit a candidate starts from.

    A GitHub merge commit has two parents: the base branch tip and the pull
    request head. Their merge base is where the branch actually started. A
    squash or rebase merge has one parent, and that parent is the same thing.
    """

    commit_parents = parents(repo, merge_commit)
    if len(commit_parents) >= 2:
        return git(repo, "merge-base", commit_parents[0], commit_parents[1]).strip()
    if len(commit_parents) == 1:
        return commit_parents[0]
    raise GitError(f"commit has no parent: {merge_commit}")


def changed_paths(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    """`(status, path)` for every path the range changed, rename-aware."""

    raw = git(repo, "diff", "--name-status", "-M", "--no-renames", base, head)
    rows: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        rows.append((fields[0].strip(), fields[-1].strip()))
    return rows


def numstat(repo: Path, base: str, head: str) -> dict[str, tuple[int, int]]:
    """Added and deleted line counts per path; binary files count as zero."""

    raw = git(repo, "diff", "--numstat", "-M", "--no-renames", base, head)
    counts: dict[str, tuple[int, int]] = {}
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        added, deleted, path = fields
        if added == "-" or deleted == "-":
            counts[path.strip()] = (0, 0)
            continue
        counts[path.strip()] = (int(added), int(deleted))
    return counts


def diff_text(repo: Path, base: str, head: str, paths: Sequence[str]) -> str:
    if not paths:
        return ""
    return git(repo, "diff", "-M", "--no-renames", base, head, "--", *paths)


def file_at(repo: Path, commit: str, path: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if completed.returncode:
        return None
    return completed.stdout


def grep_paths(repo: Path, commit: str, needles: Sequence[str], prefix: str) -> list[str]:
    """Paths under ``prefix`` at ``commit`` containing any fixed needle."""

    if not needles:
        return []
    arguments = ["grep", "-l", "-F"]
    for needle in needles:
        arguments.extend(["-e", needle])
    arguments.extend([commit, "--", prefix])
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    # git grep exits 1 when nothing matched, which is not an error here.
    if completed.returncode not in {0, 1}:
        raise GitError(f"git grep failed with exit {completed.returncode}")
    found = []
    for line in completed.stdout.splitlines():
        _commit, separator, path = line.partition(":")
        if separator and path.strip():
            found.append(path.strip())
    return sorted(set(found))


def blob_sizes(repo: Path, commit: str, prefix: str) -> dict[str, int]:
    """Byte size of every blob under ``prefix`` at ``commit``."""

    raw = git(repo, "ls-tree", "-r", "-l", commit, "--", prefix)
    sizes: dict[str, int] = {}
    for line in raw.splitlines():
        head, separator, path = line.partition("\t")
        if not separator:
            continue
        fields = head.split()
        if len(fields) < 4 or not fields[3].isdigit():
            continue
        sizes[path.strip()] = int(fields[3])
    return sizes


def blob_digest(repo: Path, commit: str, path: str) -> str:
    """sha256 of one path's content at one commit; a missing path digests as ``-``."""

    content = file_at(repo, commit, path)
    if content is None:
        return "-"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@contextmanager
def candidate_workspace(repo: Path, commit: str, root: Path, name: str) -> Iterator[Path]:
    """A detached worktree at ``commit``, removed however the block exits.

    Detached on purpose: the lane never creates a branch, so two runs of the
    same task can never collide on a branch name, and a leaked worktree is
    one `git worktree prune` away rather than a stale branch to reap.
    """

    workspace = root / name
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    # Removing the directory does not remove git's registration of it, and an
    # earlier crashed run leaves exactly that: an entry pointing at a path that
    # no longer exists, which makes the next `worktree add` refuse. Prune first
    # so a crash costs one run, not every run after it.
    git(repo, "worktree", "prune")
    git(repo, "worktree", "add", "--detach", str(workspace), commit)
    try:
        yield workspace
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(workspace)],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "prune"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
