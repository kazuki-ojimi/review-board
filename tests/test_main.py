import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest

import main
from github import GitHubError
from main import (
    MemoryCache,
    build_board,
    build_merged,
    build_others,
    make_caches,
    make_handler,
)


def test_memory_cache_fetches_only_when_empty_or_forced():
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    cache = MemoryCache(fetch)

    assert cache.get(force=False) == ({"n": 1}, False)
    assert cache.get(force=False) == ({"n": 1}, True)
    assert cache.get(force=True) == ({"n": 2}, False)
    assert len(calls) == 2


@pytest.mark.parametrize("path", ["/api/others", "/api/merged"])
def test_refreshing_a_modal_list_reuses_the_cached_board(monkeypatch, path):
    board_fetches = []
    monkeypatch.setattr(main, "fetch_activity", lambda: board_fetches.append(1) or {})
    monkeypatch.setattr(
        main,
        "build_board",
        lambda _data: {"repos": ["o/r"], "reposTruncated": False},
    )
    searched = []
    empty = {"issueCount": 0, "nodes": []}
    monkeypatch.setattr(
        main, "fetch_others", lambda repos: searched.append(repos) or empty
    )
    monkeypatch.setattr(
        main, "fetch_merged", lambda repos, _since: searched.append(repos) or empty
    )
    caches = make_caches()

    caches[path].get(force=True)
    caches[path].get(force=True)

    assert len(board_fetches) == 1
    assert searched == [["o/r"], ["o/r"]]


def test_failed_refresh_keeps_the_previous_value():
    results = [{"n": 1}, GitHubError("q.graphql", detail="boom")]

    def fetch():
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    cache = MemoryCache(fetch)
    cache.get(force=False)

    with pytest.raises(GitHubError):
        cache.get(force=True)
    assert cache.get(force=False) == ({"n": 1}, True)


def test_build_merged_searches_from_the_utc_date_fourteen_days_before(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main,
        "fetch_merged",
        lambda repos, since: (
            calls.append((repos, since)) or {"issueCount": 7, "nodes": []}
        ),
    )

    # `now` is UTC; the window starts at 00:00 UTC of the date 14 days earlier.
    merged = build_merged(
        {"repos": ["o/r"], "reposTruncated": True},
        datetime(2026, 11, 13, 20, 0, tzinfo=UTC),
    )

    assert calls == [(["o/r"], datetime(2026, 10, 30, tzinfo=UTC).date())]
    assert (merged["since"], merged["total"], merged["reposTruncated"]) == (
        "2026-10-30T00:00:00+00:00",
        7,
        True,
    )


def _pr(number, *, needs_action, updated, draft=False):
    comments = (
        [
            {
                "author": {"login": "other"},
                "createdAt": "2026-11-02T00:00:00Z",
                "reactionGroups": [],
            }
        ]
        if needs_action
        else []
    )
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "isDraft": draft,
        "createdAt": "2026-11-01T00:00:00Z",
        "updatedAt": updated,
        "reviewDecision": None,
        "author": {"login": "me"},
        "repository": {"nameWithOwner": "o/r"},
        "comments": {"nodes": comments},
        "reviews": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "commits": {"nodes": []},
    }


def test_build_board_sorts_action_first_and_drafts_last():
    data = {
        "viewer": {
            "login": "me",
            "repositoriesContributedTo": {"totalCount": 0, "nodes": []},
        },
        "authored": {
            "issueCount": 5,
            "nodes": [
                _pr(1, needs_action=False, updated="2026-11-01T00:00:00Z"),
                _pr(2, needs_action=True, updated="2026-11-03T00:00:00Z"),
                None,
                _pr(3, needs_action=True, updated="2026-11-05T00:00:00Z"),
                _pr(4, needs_action=True, updated="2026-11-08T00:00:00Z", draft=True),
                _pr(5, needs_action=False, updated="2026-11-09T00:00:00Z", draft=True),
            ],
        },
        "reviewed": {"issueCount": 0, "nodes": []},
        "requested": {"issueCount": 0, "nodes": []},
    }

    board = build_board(data)

    # Drafts never need action, so they are ordered by update time only, and
    # stay below #1 even though they were updated later.
    assert [r["number"] for r in board["authored"]] == [3, 2, 1, 5, 4]
    assert board["totals"] == {"authored": 5, "reviewing": 0}
    assert board["reposTruncated"] is False


def test_build_board_merges_reviewed_and_requested_rows_and_sorts_them(monkeypatch):
    monkeypatch.setattr(
        main,
        "summarize_reviewing",
        lambda pr, _viewer, requested=False: {
            "number": pr["number"],
            "isDraft": pr["isDraft"],
            "updatedAt": pr["updatedAt"],
            "needsAction": pr["number"] == 1 or requested,
            "reviewRequested": requested,
        },
    )
    data = {
        "viewer": {
            "login": "me",
            "repositoriesContributedTo": {"totalCount": 0, "nodes": []},
        },
        "authored": {"issueCount": 0, "nodes": []},
        "reviewed": {
            "issueCount": 2,
            "nodes": [
                _pr(2, needs_action=False, updated="2026-11-09T00:00:00Z"),
                None,
                _pr(1, needs_action=False, updated="2026-11-01T00:00:00Z"),
            ],
        },
        "requested": {
            "issueCount": 4,
            "nodes": [_pr(3, needs_action=False, updated="2026-11-05T00:00:00Z")],
        },
    }

    board = build_board(data)

    # Rows needing action come first, newest update first within each group.
    assert [(r["number"], r["reviewRequested"]) for r in board["reviewing"]] == [
        (3, True),
        (1, False),
        (2, False),
    ]
    assert board["totals"]["reviewing"] == 6


@pytest.mark.parametrize(("total", "truncated"), [(3, True), (2, False)])
def test_build_board_lists_non_archived_repos_and_truncation(total, truncated):
    data = {
        "viewer": {
            "login": "me",
            "repositoriesContributedTo": {
                "totalCount": total,
                "nodes": [
                    {"nameWithOwner": "o/a", "isArchived": False},
                    {"nameWithOwner": "o/b", "isArchived": True},
                ],
            },
        },
        "authored": {"issueCount": 0, "nodes": []},
        "reviewed": {"issueCount": 0, "nodes": []},
        "requested": {"issueCount": 0, "nodes": []},
    }

    board = build_board(data)

    assert board["repos"] == ["o/a"]
    assert board["reposTruncated"] is truncated


def test_build_others_passes_totals_through(monkeypatch):
    monkeypatch.setattr(
        main, "fetch_others", lambda _repos: {"issueCount": 7, "nodes": []}
    )

    payload = build_others({"repos": ["o/r"], "reposTruncated": True})

    assert (payload["total"], payload["reposTruncated"], payload["rows"]) == (
        7,
        True,
        [],
    )


def test_concurrent_first_reads_fetch_once():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        started.set()
        release.wait(5)
        return {"n": 1}

    cache = MemoryCache(fetch)
    first = threading.Thread(target=cache.get, kwargs={"force": False})
    first.start()
    started.wait(5)
    second = threading.Thread(target=cache.get, kwargs={"force": False})
    second.start()
    release.set()
    first.join(5)
    second.join(5)

    assert len(calls) == 1


def test_cached_read_is_not_held_up_by_a_running_refetch():
    started = threading.Event()
    release = threading.Event()
    values = [{"n": 1}, {"n": 2}]

    def fetch():
        value = values.pop(0)
        if value["n"] == 2:
            started.set()
            release.wait(5)
        return value

    cache = MemoryCache(fetch)
    cache.get(force=False)
    refetch = threading.Thread(target=cache.get, kwargs={"force": True})
    refetch.start()
    started.wait(5)

    try:
        assert cache.get(force=False) == ({"n": 1}, True)
    finally:
        release.set()
        refetch.join(5)


@pytest.fixture
def serve():
    servers = []

    def start(result):
        fetches = []

        def fetch():
            fetches.append(1)
            if isinstance(result, Exception):
                raise result
            return result

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler({"/api/prs": MemoryCache(fetch)})
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", fetches

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


# S310: the URLs are always http://127.0.0.1 of the test server.
def _get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as res:  # noqa: S310
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_handler_refreshes_only_with_refresh_1(serve):
    base, fetches = serve({"x": 1})

    _, first = _get(f"{base}/api/prs")
    status, body = _get(f"{base}/api/prs?refresh=true")
    _get(f"{base}/api/prs?refresh=1")

    assert json.loads(first)["cached"] is False
    assert status == 200
    assert json.loads(body) == {"x": 1, "cached": True}
    assert len(fetches) == 2


@pytest.mark.parametrize(
    ("error", "expected"),
    [(GitHubError("q.graphql", detail="gh failed"), 502), (KeyError("nodes"), 500)],
)
def test_handler_reports_fetch_errors_as_json(serve, error, expected):
    base, _ = serve(error)

    status, body = _get(f"{base}/api/prs")

    assert status == expected
    assert "error" in json.loads(body)


def test_handler_rejects_other_hosts_and_unknown_paths(serve):
    base, _ = serve({"x": 1})
    port = base.rsplit(":", 1)[1]

    assert _get(f"{base}/api/prs", {"Host": f"evil.example:{port}"})[0] == 403
    assert _get(f"{base}/api/prs", {"Host": f"localhost:{port}"})[0] == 200
    assert _get(f"{base}/nope")[0] == 404


@pytest.mark.parametrize(
    ("site", "expected"),
    [("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200)],
)
def test_handler_rejects_cross_site_api_requests(serve, site, expected):
    base, fetches = serve({"x": 1})

    status, _ = _get(f"{base}/api/prs?refresh=1", {"Sec-Fetch-Site": site})

    assert status == expected
    assert len(fetches) == (1 if expected == 200 else 0)


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html; charset=utf-8"),
        ("/manifest.webmanifest", "application/manifest+json"),
        ("/icon.svg", "image/svg+xml"),
        ("/icon-192.png", "image/png"),
        ("/icon-512.png", "image/png"),
    ],
)
def test_handler_serves_static_files(serve, path, content_type):
    base, _ = serve({"x": 1})
    # S310: the URL is the test server on 127.0.0.1.
    request = urllib.request.Request(f"{base}{path}")  # noqa: S310

    with urllib.request.urlopen(request, timeout=5) as res:  # noqa: S310
        assert res.status == 200
        assert res.headers["Content-Type"] == content_type
        assert res.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert res.headers["Cache-Control"] == "no-store"
