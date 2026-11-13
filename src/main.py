"""Serve a local dashboard of the viewer's pull requests, the ones they reviewed, unreviewed ones and recently merged ones."""

import argparse
import json
import logging
import threading
import webbrowser
from datetime import UTC, datetime, time, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from collections.abc import Callable

from github import GitHubError, fetch_activity, fetch_merged, fetch_unreviewed
from status import (
    pr_nodes,
    summarize_authored,
    summarize_merged,
    summarize_reviewed,
    summarize_unreviewed,
)

STATIC_DIR = Path(__file__).with_name("static")
# URL path -> (file name in STATIC_DIR, content type)
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MERGED_DAYS = 14

logger = logging.getLogger("review-board")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = sorted(rows, key=lambda r: r["updatedAt"], reverse=True)
    return sorted(rows, key=lambda r: (r["isDraft"], not r["needsAction"]))


def build_board(data: dict[str, Any]) -> dict[str, Any]:
    """Turn the GraphQL ``data`` object into the payload served at ``/api/prs``.

    :param data: Return value of :func:`github.fetch_activity`.
    :return: Rows for both tables, counts used to detect truncated searches, and
        the repositories the other lists search.
    """
    viewer = data["viewer"]["login"]
    authored = pr_nodes(data["authored"])
    reviewed = pr_nodes(data["reviewed"])
    contributed = data["viewer"]["repositoriesContributedTo"]
    return {
        "viewer": viewer,
        "fetchedAt": _now(),
        "repos": [
            r["nameWithOwner"] for r in contributed["nodes"] if not r["isArchived"]
        ],
        "reposTruncated": contributed["totalCount"] > len(contributed["nodes"]),
        "authored": _sort_rows([summarize_authored(pr, viewer) for pr in authored]),
        "reviewed": _sort_rows([summarize_reviewed(pr, viewer) for pr in reviewed]),
        "totals": {
            "authored": data["authored"]["issueCount"],
            "reviewed": data["reviewed"]["issueCount"],
        },
    }


def build_unreviewed(board: dict[str, Any]) -> dict[str, Any]:
    """Fetch open pull requests in the board's repositories that the viewer has neither opened nor reviewed.

    :param board: Payload of :func:`build_board`, for its repository list.
    :return: The payload served at ``/api/unreviewed``.
    :raises GitHubError: When fetching fails.
    """
    search = fetch_unreviewed(board["repos"])
    return {
        "fetchedAt": _now(),
        "rows": summarize_unreviewed(search),
        "total": search["issueCount"],
        "reposTruncated": board["reposTruncated"],
    }


def build_merged(board: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Fetch pull requests merged in the last :data:`MERGED_DAYS` days.

    :param board: Payload of :func:`build_board`, for its repository list.
    :param now: Current time (UTC); the window starts at the beginning of the UTC
        date ``MERGED_DAYS`` days before it.
    :return: The payload served at ``/api/merged``.
    :raises GitHubError: When fetching fails.
    """
    since = (now - timedelta(days=MERGED_DAYS)).date()
    search = fetch_merged(board["repos"], since)
    return {
        "fetchedAt": now.isoformat(),
        # The exact start of the window, so the page does not have to know it is UTC.
        "since": datetime.combine(since, time.min, tzinfo=UTC).isoformat(),
        "days": MERGED_DAYS,
        "rows": summarize_merged(search),
        "total": search["issueCount"],
        "reposTruncated": board["reposTruncated"],
    }


class MemoryCache:
    """Hold the last fetched payload in memory until a refresh is requested.

    Nothing is fetched in the background, so GitHub is only called while a
    client is asking for data.
    """

    def __init__(self, fetch: Callable[[], dict[str, Any]]) -> None:
        """Create an empty cache.

        :param fetch: Builds a fresh payload; may raise :class:`GitHubError`.
        """
        self._fetch = fetch
        self._lock = threading.Lock()
        self._value: dict[str, Any] | None = None

    def get(self, *, force: bool) -> tuple[dict[str, Any], bool]:
        """Return the cached payload, fetching when forced or when nothing is cached.

        :param force: Ignore the cache and refetch.
        :return: The payload and whether it came from the cache.
        :raises GitHubError: When fetching fails.
        """
        # Read without the lock so a cached request is not held up by a running refetch.
        if not force and (value := self._value) is not None:
            return value, True
        with self._lock:
            if self._value is not None and not force:
                return self._value, True
            self._value = self._fetch()
            return self._value, False


def make_caches() -> dict[str, MemoryCache]:
    """Build one cache per API path.

    The unreviewed and merged lists search the repositories of the cached board
    and do not refresh it.

    :return: Caches keyed by URL path.
    """
    board = MemoryCache(lambda: build_board(fetch_activity()))
    return {
        "/api/prs": board,
        "/api/unreviewed": MemoryCache(
            lambda: build_unreviewed(board.get(force=False)[0])
        ),
        "/api/merged": MemoryCache(
            lambda: build_merged(board.get(force=False)[0], datetime.now(UTC))
        ),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the static page and the JSON API backed by :attr:`caches`."""

    caches: ClassVar[dict[str, MemoryCache]] = {}

    def do_GET(self) -> None:
        """Handle a GET request."""
        # Reject other Host names so a DNS-rebound page cannot read the data.
        port = cast("tuple[str, int]", self.server.server_address)[1]
        if self.headers.get("Host") not in {f"{HOST}:{port}", f"localhost:{port}"}:
            self._send_text(HTTPStatus.FORBIDDEN, b"forbidden host")
            return
        url = urlparse(self.path)
        if url.path in STATIC_FILES:
            name, content_type = STATIC_FILES[url.path]
            self._send(HTTPStatus.OK, content_type, (STATIC_DIR / name).read_bytes())
        elif url.path in self.caches:
            self._serve_api(self.caches[url.path], path=url.path, query=url.query)
        else:
            self._send_text(HTTPStatus.NOT_FOUND, b"not found")

    def _serve_api(self, cache: MemoryCache, *, path: str, query: str) -> None:
        # Other sites can still send requests here (e.g. an <img> pointing at
        # ?refresh=1) and burn the GitHub API quota. Browsers mark them with
        # Sec-Fetch-Site; clients without the header (curl) pass.
        if self.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}:
            self._send_text(HTTPStatus.FORBIDDEN, b"cross-site request")
            return
        force = parse_qs(query).get("refresh") == ["1"]
        try:
            payload, cached = cache.get(force=force)
        except GitHubError as e:
            logger.warning("fetch failed (%s): %s", path, e)
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
            return
        except Exception:
            logger.exception("building %s failed", path)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal error; see the server log"},
            )
            return
        self._send_json(HTTPStatus.OK, {**payload, "cached": cached})

    def _send_text(self, status: HTTPStatus, body: bytes) -> None:
        self._send(status, "text/plain; charset=utf-8", body)

    def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self._send(status, "application/json; charset=utf-8", encoded)

    def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Keep other sites from framing the page, which would let them trigger
        # same-origin refreshes from inside the frame.
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    # A002: the parameter name is fixed by BaseHTTPRequestHandler.log_message.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Send access logs to the debug level."""
        logger.debug(format, *args)


def make_handler(caches: dict[str, MemoryCache]) -> type[DashboardHandler]:
    """Build a request handler class bound to ``caches``.

    :param caches: Caches keyed by API path, shared by all requests.
    :return: Handler class for :class:`ThreadingHTTPServer`.
    """
    return type("BoundDashboardHandler", (DashboardHandler,), {"caches": caches})


def main() -> None:
    """Start the dashboard server on localhost."""
    parser = argparse.ArgumentParser(
        description="Serve the review-board dashboard on localhost."
        " Requires the gh CLI to be logged in.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"default: {DEFAULT_PORT}"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="do not open a browser tab"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    with ThreadingHTTPServer((HOST, args.port), make_handler(make_caches())) as server:
        url = f"http://{HOST}:{args.port}/"
        logger.info("serving %s", url)
        if not args.no_open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("stopped")


if __name__ == "__main__":
    main()
