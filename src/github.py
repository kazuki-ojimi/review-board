"""Fetch pull request activity from GitHub through the gh CLI."""

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date

ACTIVITY_QUERY_PATH = Path(__file__).with_name("activity.graphql")
OPEN_PRS_QUERY_PATH = Path(__file__).with_name("open_prs.graphql")
MERGED_PRS_QUERY_PATH = Path(__file__).with_name("merged_prs.graphql")
_AUTHORED = "author:@me"
_REVIEWED = "reviewed-by:@me"
_REQUESTED = "review-requested:@me"

# Searches stop at their limit, so sort them to keep the most recent results.
AUTHORED_QUERY = f"is:pr is:open {_AUTHORED} sort:updated-desc"
REVIEWED_QUERY = f"is:pr is:open {_REVIEWED} -{_AUTHORED} sort:updated-desc"
# Excludes reviewed ones so a re-requested pull request is not listed twice.
REQUESTED_QUERY = (
    f"is:pr is:open {_REQUESTED} -{_REVIEWED} -{_AUTHORED} sort:updated-desc"
)
# Everything AUTHORED_QUERY, REVIEWED_QUERY and REQUESTED_QUERY leave out.
OTHERS_QUERY = (
    f"is:pr is:open -{_AUTHORED} -{_REVIEWED} -{_REQUESTED} sort:updated-desc"
)
MERGED_QUERY = "is:pr is:merged sort:updated-desc"

ACTIVITY_LIMIT = 50
OTHERS_LIMIT = 100
MERGED_LIMIT = 100
GH_TIMEOUT_SECONDS = 60


class GitHubError(Exception):
    """Raised when the gh CLI exits non-zero or times out, or the response is not JSON, has errors, or lacks the requested fields."""

    def __init__(self, query_name: str, *, detail: str) -> None:
        """Build the message from the query file name and what went wrong.

        :param query_name: File name of the GraphQL query that was run.
        :param detail: Description of the failure.
        """
        super().__init__(f"gh api graphql ({query_name}): {detail}")


def _graphql(
    query_path: Path, variables: dict[str, str | int], required: set[str]
) -> dict[str, Any]:
    command = ["gh", "api", "graphql", "-F", f"query=@{query_path}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        command += [flag, f"{key}={value}"]
    name = query_path.name
    try:
        # S603: argv list without a shell; values are constants, repository names,
        # a date and integers.
        result = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise GitHubError(name, detail=f"timed out after {GH_TIMEOUT_SECONDS}s") from e
    if result.returncode != 0:
        detail = f"failed (exit {result.returncode}): {result.stderr.strip()}"
        raise GitHubError(name, detail=detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(name, detail="returned non-JSON output") from e
    if payload.get("errors"):
        messages = "; ".join(err.get("message", "?") for err in payload["errors"])
        raise GitHubError(name, detail=f"GraphQL errors: {messages}")
    data = payload.get("data")
    if not isinstance(data, dict) or not required <= data.keys():
        raise GitHubError(name, detail=f"response is missing {sorted(required)}")
    return data


def _repo_search_query(base: str, repos: list[str]) -> str:
    if not repos:
        msg = "repos must not be empty"
        raise ValueError(msg)
    return " ".join([base, *(f"repo:{r}" for r in repos)])


def others_search_query(repos: list[str]) -> str:
    """Build the search query for open pull requests the viewer has not opened, reviewed or been requested to review.

    :param repos: ``owner/name`` of repositories to search; must not be empty,
        because a search without ``repo:`` qualifiers covers all of GitHub.
    :return: The search string.
    :raises ValueError: When ``repos`` is empty.
    """
    return _repo_search_query(OTHERS_QUERY, repos)


def merged_search_query(repos: list[str], since: date) -> str:
    """Build the search query for pull requests merged on or after ``since``.

    :param repos: ``owner/name`` of repositories to search; must not be empty.
    :param since: First merge date to include (UTC).
    :return: The search string.
    :raises ValueError: When ``repos`` is empty.
    """
    return _repo_search_query(f"{MERGED_QUERY} merged:>={since.isoformat()}", repos)


def _empty_search() -> dict[str, Any]:
    return {"issueCount": 0, "nodes": []}


def fetch_merged(repos: list[str], since: date) -> dict[str, Any]:
    """Fetch pull requests merged on or after ``since`` in ``repos``.

    :param repos: ``owner/name`` of repositories to search.
    :param since: First merge date to include (UTC).
    :return: The ``search`` object of ``merged_prs.graphql``; an empty result
        without calling GitHub when ``repos`` is empty.
    :raises GitHubError: When the query fails (see :class:`GitHubError`).
    """
    if not repos:
        return _empty_search()
    return _graphql(
        MERGED_PRS_QUERY_PATH,
        {"q": merged_search_query(repos, since), "limit": MERGED_LIMIT},
        {"search"},
    )["search"]


def fetch_others(repos: list[str]) -> dict[str, Any]:
    """Fetch open pull requests in ``repos`` the viewer has not opened, reviewed or been requested to review.

    :param repos: ``owner/name`` of repositories to search.
    :return: The ``search`` object of ``open_prs.graphql``; an empty result
        without calling GitHub when ``repos`` is empty.
    :raises GitHubError: When the query fails (see :class:`GitHubError`).
    """
    if not repos:
        return _empty_search()
    return _graphql(
        OPEN_PRS_QUERY_PATH,
        {"q": others_search_query(repos), "limit": OTHERS_LIMIT},
        {"search"},
    )["search"]


def fetch_activity() -> dict[str, Any]:
    """Fetch the viewer's own pull requests, reviewed ones and ones awaiting their review.

    :return: The ``data`` object of ``activity.graphql``.
    :raises GitHubError: When the query fails (see :class:`GitHubError`).
    """
    return _graphql(
        ACTIVITY_QUERY_PATH,
        {
            "authored": AUTHORED_QUERY,
            "reviewed": REVIEWED_QUERY,
            "requested": REQUESTED_QUERY,
            "limit": ACTIVITY_LIMIT,
        },
        {"viewer", "authored", "reviewed", "requested"},
    )
