import subprocess
from datetime import date
from pathlib import Path

import pytest

import github
from github import (
    GitHubError,
    fetch_merged,
    fetch_unreviewed,
    unreviewed_search_query,
)


@pytest.mark.parametrize(
    "fetch",
    [lambda: fetch_merged([], date(2026, 10, 27)), lambda: fetch_unreviewed([])],
)
def test_fetch_without_repos_returns_empty_without_calling_gh(monkeypatch, fetch):
    def fail(*_args, **_kwargs):
        pytest.fail("gh must not be called")

    monkeypatch.setattr(github.subprocess, "run", fail)

    assert fetch() == {"issueCount": 0, "nodes": []}


@pytest.fixture
def graphql_calls(monkeypatch):
    calls = []

    def fake(query_path, variables, required):
        calls.append((query_path.name, variables, required))
        return {"search": {"issueCount": 1, "nodes": []}, "viewer": {}}

    monkeypatch.setattr(github, "_graphql", fake)
    return calls


def test_fetch_activity_sends_both_searches(graphql_calls):
    github.fetch_activity()

    assert graphql_calls == [
        (
            "activity.graphql",
            {
                "authored": "is:pr is:open author:@me sort:updated-desc",
                "reviewed": "is:pr is:open reviewed-by:@me -author:@me sort:updated-desc",
                "limit": 50,
            },
            {"viewer", "authored", "reviewed"},
        )
    ]


@pytest.mark.parametrize(
    ("fetch", "query_file", "query"),
    [
        (
            lambda: fetch_unreviewed(["o/a"]),
            "open_prs.graphql",
            "is:pr is:open -author:@me -reviewed-by:@me sort:updated-desc repo:o/a",
        ),
        (
            lambda: fetch_merged(["o/a"], date(2026, 10, 27)),
            "merged_prs.graphql",
            "is:pr is:merged sort:updated-desc merged:>=2026-10-27 repo:o/a",
        ),
    ],
)
def test_fetch_with_repos_returns_the_search(graphql_calls, fetch, query_file, query):
    assert fetch() == {"issueCount": 1, "nodes": []}
    assert graphql_calls == [(query_file, {"q": query, "limit": 100}, {"search"})]


def test_unreviewed_search_query_adds_repo_qualifiers():
    query = unreviewed_search_query(["o/a", "o/b"])

    assert query.endswith(" repo:o/a repo:o/b")


def test_unreviewed_search_query_rejects_empty_repos():
    with pytest.raises(ValueError, match="must not be empty"):
        unreviewed_search_query([])


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (lambda: _completed(1, stderr="HTTP 502"), "failed \\(exit 1\\): HTTP 502"),
        (lambda: _completed(stdout="<html>"), "non-JSON"),
        (
            lambda: _completed(stdout='{"errors": [{"message": "bad"}]}'),
            "GraphQL errors: bad",
        ),
        (lambda: _completed(stdout='{"data": {}}'), "missing \\['search'\\]"),
        (lambda: _completed(stdout='{"data": null}'), "missing \\['search'\\]"),
    ],
)
def test_graphql_failures_raise_github_error(monkeypatch, run, message):
    monkeypatch.setattr(github.subprocess, "run", lambda *_a, **_k: run())

    with pytest.raises(GitHubError, match=message):
        github._graphql(Path("q.graphql"), {}, {"search"})


def test_graphql_passes_ints_with_capital_f_and_strings_with_lowercase_f(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == github.GH_TIMEOUT_SECONDS
        return _completed(stdout='{"data": {"search": {}}}')

    monkeypatch.setattr(github.subprocess, "run", run)

    data = github._graphql(Path("q.graphql"), {"q": "@me", "limit": 5}, {"search"})

    assert commands[0] == [
        "gh",
        "api",
        "graphql",
        "-F",
        "query=@q.graphql",
        "-f",
        "q=@me",
        "-F",
        "limit=5",
    ]
    assert data == {"search": {}}


def test_graphql_timeout_raises_github_error(monkeypatch):
    def timeout(*_args, **_kwargs):
        command = ["gh"]
        raise subprocess.TimeoutExpired(command, github.GH_TIMEOUT_SECONDS)

    monkeypatch.setattr(github.subprocess, "run", timeout)

    with pytest.raises(GitHubError, match="timed out"):
        github._graphql(Path("q.graphql"), {}, {"search"})
