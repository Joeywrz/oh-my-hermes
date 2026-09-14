"""The only network reads in this lane, and only at corpus build time.

`omh` core makes no network call. This module is benchmark tooling under
`benchmarks/`, it shells out to `gh`, and it runs exactly once per corpus
build. After the manifest is written every run is offline: the task text is
pinned in the manifest and every tree fact comes from the local git object
store.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

GH_TIMEOUT_SECONDS = 120

PULL_REQUEST_FIELDS = (
    "number,title,body,mergedAt,mergeCommit,additions,deletions,"
    "closingIssuesReferences,labels,author"
)


class GitHubError(RuntimeError):
    """A `gh` read this lane needs did not succeed."""


def gh_json(*arguments: str, timeout: int = GH_TIMEOUT_SECONDS) -> Any:
    completed = subprocess.run(
        ["gh", *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode:
        raise GitHubError(
            f"gh {' '.join(arguments[:3])} failed with exit {completed.returncode}: "
            f"{completed.stderr.strip()[:400]}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise GitHubError("gh did not return JSON") from error


def authenticated() -> bool:
    completed = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
        timeout=GH_TIMEOUT_SECONDS,
    )
    return completed.returncode == 0


def merged_pull_requests(repository: str, limit: int) -> list[dict[str, Any]]:
    rows = gh_json(
        "pr",
        "list",
        "--repo",
        repository,
        "--state",
        "merged",
        "--limit",
        str(limit),
        "--json",
        PULL_REQUEST_FIELDS,
    )
    if not isinstance(rows, list):
        raise GitHubError("gh pr list did not return a list")
    return [row for row in rows if isinstance(row, dict)]


def issue_body(repository: str, number: int) -> str:
    row = gh_json("issue", "view", str(number), "--repo", repository, "--json", "body,title")
    if not isinstance(row, dict):
        raise GitHubError("gh issue view did not return an object")
    return str(row.get("body") or "")
