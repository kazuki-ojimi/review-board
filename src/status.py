"""Turn GitHub pull request data into dashboard rows, deciding who should act next."""

from dataclasses import KW_ONLY, dataclass
from datetime import datetime
from typing import Any

# Review states that carry a decision even when the body is empty.
_DECISION_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
# Reactions that mean "looking at it" rather than "handled".
_NON_ACK_REACTIONS = frozenset({"EYES"})


@dataclass(frozen=True)
class Event:
    """One piece of activity on a pull request.

    ``is_commit`` separates pushes from comments and reviews.
    ``acknowledged`` is true when the viewer reacted to it with anything other
    than 👀, which marks it and everything before it as handled.
    ``resolved`` is true for comments in a resolved review thread; they still
    count as the viewer's own activity but never as something to respond to.
    """

    actor: str | None
    at: datetime
    _: KW_ONLY
    is_commit: bool = False
    acknowledged: bool = False
    resolved: bool = False


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _login(node: dict[str, Any] | None) -> str | None:
    if not node:
        return None
    return node.get("login")


def _nodes(pr: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = pr.get(key)
    if connection is None:
        msg = f"pull request {pr.get('url')} is missing field {key!r}"
        raise ValueError(msg)
    return connection["nodes"]


def _acknowledged(node: dict[str, Any]) -> bool:
    return any(
        g["viewerHasReacted"] and g["content"] not in _NON_ACK_REACTIONS
        for g in node["reactionGroups"] or []
    )


def _thread_comments(thread: dict[str, Any]) -> list[dict[str, Any]]:
    # Comments of the viewer's unsubmitted review are not visible to anyone else.
    return [c for c in thread["comments"]["nodes"] if c["state"] != "PENDING"]


def _pr_ref(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "author": _login(pr["author"]),
    }


def _written_event(node: dict[str, Any], at: str, *, resolved: bool = False) -> Event:
    return Event(
        _login(node["author"]),
        _parse_time(at),
        acknowledged=_acknowledged(node),
        resolved=resolved,
    )


def pr_nodes(search: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the pull requests of a search result, dropping null nodes.

    GitHub returns null for nodes the token cannot read.

    :param search: A ``search`` object from one of the GraphQL queries.
    :return: The non-null nodes.
    """
    return [n for n in search["nodes"] if n]


def extract_events(pr: dict[str, Any]) -> list[Event]:
    """Flatten comments, reviews, review-thread comments and the head commit into events.

    Reviews submitted with state COMMENTED and an empty body are skipped because
    their content is already counted as review-thread comments.
    Pending (unsubmitted) reviews and their thread comments are skipped.

    :param pr: A ``PullRequestActivity`` node from ``activity.graphql``.
    :return: Events in no particular order.
    :raises ValueError: When a requested connection field is missing from ``pr``.
    """
    events = [_written_event(c, c["createdAt"]) for c in _nodes(pr, "comments")]
    for review in _nodes(pr, "reviews"):
        if review["submittedAt"] is None:
            continue
        if review["state"] not in _DECISION_STATES and not review["body"].strip():
            continue
        events.append(_written_event(review, review["submittedAt"]))
    for thread in _nodes(pr, "reviewThreads"):
        events.extend(
            _written_event(c, c["createdAt"], resolved=thread["isResolved"])
            for c in _thread_comments(thread)
        )
    for node in _nodes(pr, "commits"):
        commit = node["commit"]
        user = (commit.get("author") or {}).get("user")
        events.append(
            Event(_login(user), _parse_time(commit["committedDate"]), is_commit=True)
        )
    return events


def _handled_until(events: list[Event], viewer: str) -> datetime | None:
    times = [e.at for e in events if e.actor == viewer or e.acknowledged]
    return max(times) if times else None


def _base_row(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        **_pr_ref(pr),
        "isDraft": pr["isDraft"],
        "reviewDecision": pr["reviewDecision"],
        "updatedAt": pr["updatedAt"],
    }


def _summarize_new(events: list[Event]) -> dict[str, Any]:
    return {
        "newCount": len(events),
        # No login: a deleted account, or a commit author without a GitHub account.
        "newActors": sorted({e.actor or "(unknown user)" for e in events}),
        "lastNewAt": max(e.at for e in events).isoformat() if events else None,
    }


def _open_threads(pr: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [
        comments
        for t in _nodes(pr, "reviewThreads")
        if not t["isResolved"] and (comments := _thread_comments(t))
    ]


def _is_answered(comments: list[dict[str, Any]], viewer: str) -> bool:
    last = comments[-1]
    return _login(last["author"]) == viewer or _acknowledged(last)


def summarize_authored(pr: dict[str, Any], viewer: str) -> dict[str, Any]:
    """Summarize a pull request opened by the viewer.

    Commits are ignored on both sides: pushing a fix does not count as responding.
    New comments clear when the viewer comments or reviews; an unanswered thread
    clears with a reply or a non-👀 reaction on its latest comment, or when it is
    resolved.
    Comments and reviews by others after the viewer's latest comment or review,
    or after the latest one the viewer acknowledged with a reaction, are "new";
    if there is neither, everything by
    others after creation is new. Comments in resolved threads are never new.
    A non-draft pull request needs action when there is anything new or an
    unresolved review thread whose latest comment is neither the viewer's nor
    acknowledged.

    :param pr: A ``PullRequestActivity`` node.
    :param viewer: Login of the current user.
    :return: A row for the "my pull requests" table.
    :raises ValueError: When a requested connection field is missing from ``pr``.
    """
    events = [e for e in extract_events(pr) if not e.is_commit]
    since = _handled_until(events, viewer) or _parse_time(pr["createdAt"])
    new = [e for e in events if e.actor != viewer and not e.resolved and e.at > since]
    open_threads = _open_threads(pr)
    unanswered = sum(1 for t in open_threads if not _is_answered(t, viewer))
    return {
        **_base_row(pr),
        **_summarize_new(new),
        "unresolvedThreads": len(open_threads),
        "unansweredThreads": unanswered,
        "needsAction": (bool(new) or unanswered > 0) and not pr["isDraft"],
    }


def summarize_unreviewed(search: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the unreviewed open pull request search into rows.

    Rows are ordered by the latest commit time (creation time when there is no
    commit), newest first, with drafts after everything else.

    :param search: The ``search`` object of ``open_prs.graphql``.
    :return: Rows for the "unreviewed open pull requests" table.
    :raises ValueError: When a pull request in ``search`` lacks its ``commits`` field.
    """
    rows = []
    for pr in pr_nodes(search):
        commits = _nodes(pr, "commits")
        rows.append(
            {
                **_pr_ref(pr),
                "isDraft": pr["isDraft"],
                "reviewDecision": pr["reviewDecision"],
                "createdAt": pr["createdAt"],
                "lastCommitAt": commits[0]["commit"]["committedDate"]
                if commits
                else None,
            }
        )
    # committedDate keeps the committer's UTC offset, so compare parsed times.
    rows.sort(
        key=lambda r: _parse_time(r["lastCommitAt"] or r["createdAt"]), reverse=True
    )
    rows.sort(key=lambda r: r["isDraft"])
    return rows


def summarize_merged(search: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the merged pull request search into rows, most recently merged first.

    :param search: The ``search`` object of ``merged_prs.graphql``.
    :return: Rows for the "recently merged pull requests" list.
    """
    rows = [{**_pr_ref(pr), "mergedAt": pr["mergedAt"]} for pr in pr_nodes(search)]
    rows.sort(key=lambda r: _parse_time(r["mergedAt"]), reverse=True)
    return rows


def my_decision(pr: dict[str, Any], viewer: str) -> str | None:
    """Return the state of the viewer's latest approving, rejecting or dismissed review.

    COMMENTED reviews do not change an earlier decision, matching how GitHub
    keeps an approval in place after later comments.

    :param pr: A ``PullRequestActivity`` node.
    :param viewer: Login of the current user.
    :return: ``APPROVED``, ``CHANGES_REQUESTED``, ``DISMISSED`` or ``None``.
    :raises ValueError: When a requested connection field is missing from ``pr``.
    """
    decisions = [
        r
        for r in _nodes(pr, "reviews")
        if _login(r["author"]) == viewer
        and r["submittedAt"] is not None
        and r["state"] in _DECISION_STATES
    ]
    if not decisions:
        return None
    return max(decisions, key=lambda r: _parse_time(r["submittedAt"]))["state"]


def summarize_reviewed(pr: dict[str, Any], viewer: str) -> dict[str, Any]:
    """Summarize a pull request the viewer has reviewed.

    A reply is a comment, review or thread comment by the pull request author,
    or a commit by anyone other than the viewer, after the viewer's latest
    activity or the latest comment, review or thread comment the viewer
    acknowledged.
    Comments by other reviewers and comments in resolved threads are not
    treated as replies. When the author's account is deleted, only commits count.
    It needs action when there is a reply, unless it is a draft or the viewer's
    latest decision is an approval.

    :param pr: A ``PullRequestActivity`` node.
    :param viewer: Login of the current user.
    :return: A row for the "reviewed pull requests" table.
    :raises ValueError: When a requested connection field is missing from ``pr``.
    """
    author = _login(pr["author"])
    events = extract_events(pr)
    handled = _handled_until(events, viewer)
    replies = [
        e
        for e in events
        if handled is not None
        and e.at > handled
        and not e.resolved
        and e.actor != viewer
        and ((author is not None and e.actor == author) or e.is_commit)
    ]
    my_threads = [
        t
        for t in _nodes(pr, "reviewThreads")
        if t["starter"]["nodes"]
        and _login(t["starter"]["nodes"][0]["author"]) == viewer
        and _thread_comments(t)
    ]
    decision = my_decision(pr, viewer)
    return {
        **_base_row(pr),
        **_summarize_new(replies),
        "myDecision": decision,
        "myThreads": len(my_threads),
        "myThreadsResolved": sum(1 for t in my_threads if t["isResolved"]),
        "needsAction": bool(replies) and not pr["isDraft"] and decision != "APPROVED",
    }
