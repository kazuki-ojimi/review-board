import pytest

from status import (
    my_decision,
    summarize_authored,
    summarize_merged,
    summarize_others,
    summarize_reviewing,
)

ME = "me"
AUTHOR = "alice"


def _user(login):
    return {"login": login} if login else None


def _reactions(reacted):
    if reacted is None:
        return None
    return [
        {"content": c, "viewerHasReacted": c in reacted}
        for c in ("THUMBS_UP", "HEART", "EYES")
    ]


def _comment(login, at, *, reacted=(), state=None):
    comment = {
        "author": _user(login),
        "createdAt": at,
        "reactionGroups": _reactions(reacted),
    }
    # Only review-thread comments carry a state in activity.graphql.
    if state is not None:
        comment["state"] = state
    return comment


def _review(login, at, *, state="COMMENTED", body="", reacted=()):
    return {
        "author": _user(login),
        "submittedAt": at,
        "state": state,
        "body": body,
        "reactionGroups": _reactions(reacted),
    }


def _thread(*comments, resolved=False):
    return {
        "isResolved": resolved,
        "starter": {"nodes": [{"author": comments[0]["author"]}]},
        "comments": {"nodes": [{"state": "SUBMITTED", **c} for c in comments]},
    }


def _commit(login, at):
    return {"commit": {"committedDate": at, "author": {"user": _user(login)}}}


def _pr(*, author, comments=(), reviews=(), threads=(), commits=(), draft=False):
    return {
        "number": 1,
        "title": "t",
        "url": "https://github.com/o/r/pull/1",
        "isDraft": draft,
        "createdAt": "2026-11-01T00:00:00Z",
        "updatedAt": "2026-11-05T00:00:00Z",
        "reviewDecision": None,
        "author": _user(author),
        "repository": {"nameWithOwner": "o/r"},
        "comments": {"nodes": list(comments)},
        "reviews": {"nodes": list(reviews)},
        "reviewThreads": {"nodes": list(threads)},
        "commits": {"nodes": list(commits)},
    }


class TestSummarizeAuthored:
    def test_comment_after_my_last_comment_is_new(self):
        pr = _pr(
            author=ME,
            comments=[
                _comment("bob", "2026-11-01T12:00:00Z"),
                _comment(ME, "2026-11-02T00:00:00Z"),
                _comment("bob", "2026-11-03T00:00:00Z"),
            ],
        )

        row = summarize_authored(pr, ME)

        assert row["newCount"] == 1
        assert row["newActors"] == ["bob"]
        assert row["needsAction"] is True

    def test_my_push_alone_does_not_clear_comments(self):
        pr = _pr(
            author=ME,
            comments=[_comment("bob", "2026-11-02T00:00:00Z")],
            commits=[_commit(ME, "2026-11-03T00:00:00Z")],
        )

        row = summarize_authored(pr, ME)

        assert row["newCount"] == 1
        assert row["needsAction"] is True

    def test_others_push_is_not_new(self):
        pr = _pr(author=ME, commits=[_commit("bob", "2026-11-03T00:00:00Z")])

        assert summarize_authored(pr, ME)["needsAction"] is False

    def test_unanswered_thread_needs_action_after_my_later_comment(self):
        pr = _pr(
            author=ME,
            threads=[
                _thread(_comment("bob", "2026-11-02T00:00:00Z")),
                _thread(
                    _comment("bob", "2026-11-02T00:00:00Z"),
                    _comment(ME, "2026-11-03T00:00:00Z"),
                ),
                _thread(_comment("bob", "2026-11-02T00:00:00Z"), resolved=True),
            ],
            comments=[_comment(ME, "2026-11-04T00:00:00Z")],
        )

        row = summarize_authored(pr, ME)

        assert row["newCount"] == 0
        assert row["unresolvedThreads"] == 2
        assert row["unansweredThreads"] == 1
        assert row["needsAction"] is True

    def test_my_reply_clears_new_comments(self):
        pr = _pr(
            author=ME,
            threads=[
                _thread(
                    _comment("bob", "2026-11-02T00:00:00Z"),
                    _comment(ME, "2026-11-03T00:00:00Z"),
                )
            ],
        )

        row = summarize_authored(pr, ME)

        assert row["newCount"] == 0
        assert row["needsAction"] is False
        assert row["unresolvedThreads"] == 1

    def test_without_my_activity_counts_from_creation(self):
        pr = _pr(author=ME, comments=[_comment("bob", "2026-11-02T00:00:00Z")])

        assert summarize_authored(pr, ME)["newCount"] == 1

    def test_empty_commented_review_is_not_double_counted(self):
        pr = _pr(
            author=ME,
            reviews=[
                _review("bob", "2026-11-02T00:00:00Z"),
                _review("bob", "2026-11-02T00:00:00Z", body="  \n"),
            ],
            threads=[_thread(_comment("bob", "2026-11-02T00:00:00Z"))],
        )

        assert summarize_authored(pr, ME)["newCount"] == 1

    def test_approval_without_body_counts(self):
        pr = _pr(
            author=ME,
            reviews=[_review("bob", "2026-11-02T00:00:00Z", state="APPROVED")],
        )

        assert summarize_authored(pr, ME)["newCount"] == 1

    def test_my_pending_review_does_not_clear_comments(self):
        pr = _pr(
            author=ME,
            comments=[_comment("bob", "2026-11-02T00:00:00Z")],
            reviews=[_review(ME, None, state="PENDING", body="draft")],
            threads=[
                _thread(
                    _comment("bob", "2026-11-02T00:00:00Z"),
                    _comment(ME, "2026-11-03T00:00:00Z", state="PENDING"),
                )
            ],
        )

        row = summarize_authored(pr, ME)

        assert row["newCount"] == 2
        assert row["unansweredThreads"] == 1

    def test_comment_without_reaction_groups_is_new(self):
        pr = _pr(
            author=ME,
            comments=[_comment("bob", "2026-11-02T00:00:00Z", reacted=None)],
        )

        assert summarize_authored(pr, ME)["needsAction"] is True

    def test_deleted_user_comment_is_new(self):
        pr = _pr(author=ME, comments=[_comment(None, "2026-11-02T00:00:00Z")])

        assert summarize_authored(pr, ME)["newActors"] == ["(unknown user)"]

    def test_reaction_to_thread_comment_clears_new_comments_up_to_it(self):
        pr = _pr(
            author=ME,
            threads=[
                _thread(_comment("bob", "2026-11-02T00:00:00Z", reacted=["THUMBS_UP"]))
            ],
            comments=[
                _comment("carol", "2026-11-01T12:00:00Z"),
                _comment("carol", "2026-11-03T00:00:00Z"),
            ],
        )

        assert summarize_authored(pr, ME)["newCount"] == 1

    def test_pr_without_a_requested_field_is_reported(self):
        pr = _pr(author=ME)
        del pr["comments"]

        with pytest.raises(ValueError, match="missing field 'comments'"):
            summarize_authored(pr, ME)

    def test_my_reaction_clears_that_comment_and_earlier(self):
        pr = _pr(
            author=ME,
            comments=[
                _comment("bot", "2026-11-02T00:00:00Z"),
                _comment("bob", "2026-11-03T00:00:00Z", reacted=["THUMBS_UP"]),
                _comment("bob", "2026-11-04T00:00:00Z"),
            ],
        )

        row = summarize_authored(pr, ME)

        assert row["newCount"] == 1
        assert row["lastNewAt"] == "2026-11-04T00:00:00+00:00"

    def test_eyes_reaction_does_not_clear(self):
        pr = _pr(
            author=ME,
            comments=[_comment("bob", "2026-11-02T00:00:00Z", reacted=["EYES"])],
        )

        assert summarize_authored(pr, ME)["needsAction"] is True

    def test_reaction_to_review_clears_it(self):
        pr = _pr(
            author=ME,
            reviews=[
                _review(
                    "bob", "2026-11-02T00:00:00Z", body="looks fine", reacted=["HEART"]
                )
            ],
        )

        assert summarize_authored(pr, ME)["needsAction"] is False

    def test_reaction_to_last_thread_comment_answers_thread(self):
        pr = _pr(
            author=ME,
            threads=[
                _thread(_comment("bob", "2026-11-02T00:00:00Z", reacted=["THUMBS_UP"])),
                _thread(_comment("bob", "2026-11-01T12:00:00Z", reacted=["EYES"])),
                _thread(
                    _comment("bob", "2026-11-01T00:00:00Z", reacted=["THUMBS_UP"]),
                    _comment("carol", "2026-11-01T06:00:00Z"),
                ),
            ],
        )

        assert summarize_authored(pr, ME)["unansweredThreads"] == 2

    def test_thread_with_only_my_pending_comments_is_not_counted(self):
        pr = _pr(
            author=ME,
            threads=[_thread(_comment(ME, "2026-11-02T00:00:00Z", state="PENDING"))],
        )

        row = summarize_authored(pr, ME)

        assert (row["unresolvedThreads"], row["needsAction"]) == (0, False)

    def test_draft_never_needs_action(self):
        pr = _pr(
            author=ME, comments=[_comment("bob", "2026-11-02T00:00:00Z")], draft=True
        )

        row = summarize_authored(pr, ME)

        assert (row["newCount"], row["needsAction"]) == (1, False)

    def test_comment_in_resolved_thread_is_not_new(self):
        pr = _pr(
            author=ME,
            threads=[
                _thread(
                    _comment("bob", "2026-11-02T00:00:00Z"),
                    _comment(ME, "2026-11-03T00:00:00Z"),
                    _comment("bob", "2026-11-04T00:00:00Z"),
                    resolved=True,
                )
            ],
        )

        assert summarize_authored(pr, ME)["needsAction"] is False

    def test_my_comment_in_resolved_thread_still_counts_as_handled(self):
        pr = _pr(
            author=ME,
            comments=[_comment("bob", "2026-11-02T00:00:00Z")],
            threads=[_thread(_comment(ME, "2026-11-03T00:00:00Z"), resolved=True)],
        )

        assert summarize_authored(pr, ME)["newCount"] == 0


class TestSummarizeReviewing:
    def test_author_reply_after_my_review(self):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            comments=[_comment(AUTHOR, "2026-11-03T00:00:00Z")],
        )

        row = summarize_reviewing(pr, ME)

        assert row["needsAction"] is True
        assert row["reviewRequested"] is False
        assert row["newActors"] == [AUTHOR]

    def test_new_actors_are_unique_and_sorted(self):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            comments=[
                _comment(AUTHOR, "2026-11-03T00:00:00Z"),
                _comment(AUTHOR, "2026-11-04T00:00:00Z"),
            ],
            commits=[_commit("aaron", "2026-11-05T00:00:00Z")],
        )

        row = summarize_reviewing(pr, ME)

        # Commits come after comments in the events, so this also checks sorting.
        assert (row["newCount"], row["newActors"]) == (3, ["aaron", "alice"])

    def test_push_after_my_review_counts_as_reply(self):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            commits=[_commit(AUTHOR, "2026-11-03T00:00:00Z")],
        )

        assert summarize_reviewing(pr, ME)["needsAction"] is True

    def test_other_reviewer_comment_is_not_a_reply(self):
        pr = _pr(
            author=AUTHOR,
            reviews=[
                _review(ME, "2026-11-02T00:00:00Z", state="COMMENTED", body="nit")
            ],
            comments=[_comment("bob", "2026-11-03T00:00:00Z")],
        )

        assert summarize_reviewing(pr, ME)["needsAction"] is False

    def test_author_activity_before_my_review_is_not_a_reply(self):
        pr = _pr(
            author=AUTHOR,
            comments=[_comment(AUTHOR, "2026-11-01T12:00:00Z")],
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
        )

        row = summarize_reviewing(pr, ME)

        assert (row["newCount"], row["needsAction"]) == (0, False)

    def test_author_reply_in_unresolved_thread_is_a_reply(self):
        pr = _pr(
            author=AUTHOR,
            threads=[
                _thread(
                    _comment(ME, "2026-11-02T00:00:00Z"),
                    _comment(AUTHOR, "2026-11-03T00:00:00Z"),
                )
            ],
        )

        assert summarize_reviewing(pr, ME)["needsAction"] is True

    def test_without_my_activity_nothing_is_a_reply(self):
        pr = _pr(author=AUTHOR, comments=[_comment(AUTHOR, "2026-11-03T00:00:00Z")])

        assert summarize_reviewing(pr, ME)["newCount"] == 0

    def test_draft_never_needs_action(self):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            comments=[_comment(AUTHOR, "2026-11-03T00:00:00Z")],
            draft=True,
        )

        row = summarize_reviewing(pr, ME)

        assert (row["newCount"], row["needsAction"]) == (1, False)

    def test_reaction_to_author_reply_clears_it(self):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            comments=[_comment(AUTHOR, "2026-11-03T00:00:00Z", reacted=["THUMBS_UP"])],
        )

        assert summarize_reviewing(pr, ME)["needsAction"] is False

    @pytest.mark.parametrize(
        "commit",
        [
            _commit(None, "2026-11-03T00:00:00Z"),
            {"commit": {"committedDate": "2026-11-03T00:00:00Z", "author": None}},
        ],
    )
    def test_commit_without_github_user_counts_as_reply(self, commit):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            commits=[commit],
        )

        row = summarize_reviewing(pr, ME)

        assert row["needsAction"] is True
        assert row["newActors"] == ["(unknown user)"]

    def test_reaction_to_author_thread_reply_clears_it(self):
        pr = _pr(
            author=AUTHOR,
            threads=[
                _thread(
                    _comment(ME, "2026-11-02T00:00:00Z"),
                    _comment(AUTHOR, "2026-11-03T00:00:00Z", reacted=["THUMBS_UP"]),
                )
            ],
        )

        assert summarize_reviewing(pr, ME)["needsAction"] is False

    def test_deleted_author_comments_are_not_replies(self):
        pr = _pr(
            author=None,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            comments=[_comment(None, "2026-11-03T00:00:00Z")],
        )

        row = summarize_reviewing(pr, ME)

        assert row["needsAction"] is False
        assert row["author"] is None

    def test_deleted_author_pr_still_counts_commits(self):
        pr = _pr(
            author=None,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            commits=[_commit("bob", "2026-11-03T00:00:00Z")],
        )

        assert summarize_reviewing(pr, ME)["needsAction"] is True

    @pytest.mark.parametrize(
        ("comment_at", "expected"),
        [("2026-11-02T06:00:00Z", False), ("2026-11-03T00:00:00Z", True)],
    )
    def test_my_commit_is_my_activity(self, comment_at, expected):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="CHANGES_REQUESTED")],
            commits=[_commit(ME, "2026-11-02T12:00:00Z")],
            comments=[_comment(AUTHOR, comment_at)],
        )

        assert summarize_reviewing(pr, ME)["needsAction"] is expected

    def test_reply_on_pr_i_approved_does_not_need_action(self):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="APPROVED")],
            comments=[_comment(AUTHOR, "2026-11-03T00:00:00Z")],
        )

        row = summarize_reviewing(pr, ME)

        assert (row["myDecision"], row["newCount"], row["needsAction"]) == (
            "APPROVED",
            1,
            False,
        )

    def test_requested_review_needs_action_without_replies(self):
        row = summarize_reviewing(_pr(author=AUTHOR), ME, requested=True)

        assert (row["reviewRequested"], row["newCount"], row["needsAction"]) == (
            True,
            0,
            True,
        )

    def test_requested_review_on_draft_does_not_need_action(self):
        row = summarize_reviewing(_pr(author=AUTHOR, draft=True), ME, requested=True)

        assert row["needsAction"] is False

    def test_reply_after_my_approval_was_dismissed_needs_action(self):
        pr = _pr(
            author=AUTHOR,
            reviews=[_review(ME, "2026-11-02T00:00:00Z", state="DISMISSED")],
            comments=[_comment(AUTHOR, "2026-11-03T00:00:00Z")],
        )

        row = summarize_reviewing(pr, ME)

        assert (row["myDecision"], row["needsAction"]) == ("DISMISSED", True)

    def test_thread_with_only_my_pending_comments_is_not_mine_yet(self):
        pr = _pr(
            author=AUTHOR,
            threads=[_thread(_comment(ME, "2026-11-02T00:00:00Z", state="PENDING"))],
        )

        assert summarize_reviewing(pr, ME)["myThreads"] == 0

    def test_author_reply_in_resolved_thread_is_not_a_reply(self):
        pr = _pr(
            author=AUTHOR,
            threads=[
                _thread(
                    _comment(ME, "2026-11-02T00:00:00Z"),
                    _comment(AUTHOR, "2026-11-03T00:00:00Z"),
                    resolved=True,
                )
            ],
        )

        assert summarize_reviewing(pr, ME)["needsAction"] is False

    def test_my_threads_resolved_and_total(self):
        pr = _pr(
            author=AUTHOR,
            threads=[
                _thread(
                    _comment(ME, "2026-11-02T00:00:00Z"),
                    _comment(AUTHOR, "2026-11-03T00:00:00Z"),
                ),
                _thread(_comment(ME, "2026-11-02T00:00:00Z")),
                _thread(_comment(ME, "2026-11-02T00:00:00Z"), resolved=True),
                _thread(_comment("bob", "2026-11-02T00:00:00Z")),
            ],
        )

        row = summarize_reviewing(pr, ME)

        assert row["myThreads"] == 3
        assert row["myThreadsResolved"] == 1

    def test_thread_without_a_starter_is_not_mine(self):
        thread = _thread(_comment(ME, "2026-11-02T00:00:00Z"))
        thread["starter"] = {"nodes": []}
        pr = _pr(author=AUTHOR, threads=[thread])

        assert summarize_reviewing(pr, ME)["myThreads"] == 0


@pytest.mark.parametrize(
    ("reviews", "expected"),
    [
        ([], None),
        ([_review(ME, "2026-11-02T00:00:00Z", body="nit")], None),
        ([_review(ME, "2026-11-02T00:00:00Z", state="APPROVED")], "APPROVED"),
        (
            [
                _review(ME, "2026-11-02T00:00:00Z", state="APPROVED"),
                _review(ME, "2026-11-03T00:00:00Z", body="one more thing"),
            ],
            "APPROVED",
        ),
        (
            [
                _review(ME, "2026-11-02T00:00:00Z", state="APPROVED"),
                _review(ME, "2026-11-03T00:00:00Z", state="CHANGES_REQUESTED"),
            ],
            "CHANGES_REQUESTED",
        ),
        # The latest submission wins regardless of the order of the list.
        (
            [
                _review(ME, "2026-11-03T00:00:00Z", state="CHANGES_REQUESTED"),
                _review(ME, "2026-11-02T00:00:00Z", state="APPROVED"),
            ],
            "CHANGES_REQUESTED",
        ),
        # Dismissing an approval turns that same review into DISMISSED.
        ([_review(ME, "2026-11-02T00:00:00Z", state="DISMISSED")], "DISMISSED"),
        ([_review("bob", "2026-11-02T00:00:00Z", state="APPROVED")], None),
        ([_review(ME, None, state="PENDING")], None),
        ([_review(ME, None, state="APPROVED")], None),
    ],
)
def test_my_decision(reviews, expected):
    assert my_decision(_pr(author=AUTHOR, reviews=reviews), ME) == expected


def _open_pr(number, *, draft=False, committed=None):
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "isDraft": draft,
        "createdAt": "2026-11-03T00:00:00Z",
        "reviewDecision": None,
        "author": _user("bob"),
        "repository": {"nameWithOwner": "o/r"},
        "commits": {
            "nodes": [{"commit": {"committedDate": committed}}] if committed else []
        },
    }


class TestSummarizeOthers:
    def test_sorts_by_last_commit_with_drafts_last(self):
        search = {
            "issueCount": 4,
            "nodes": [
                _open_pr(1, committed="2026-11-02T00:00:00Z"),
                _open_pr(2, committed="2026-11-05T00:00:00Z"),
                _open_pr(3, committed="2026-11-09T00:00:00Z", draft=True),
                None,
                _open_pr(4),
            ],
        }

        rows = summarize_others(search)

        assert [r["number"] for r in rows] == [2, 4, 1, 3]

    def test_compares_commit_times_across_utc_offsets(self):
        search = {
            "issueCount": 2,
            "nodes": [
                _open_pr(1, committed="2026-11-02T20:00:00+09:00"),
                _open_pr(2, committed="2026-11-02T12:00:00Z"),
            ],
        }

        rows = summarize_others(search)

        assert [r["number"] for r in rows] == [2, 1]


def _merged_pr(number, merged):
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "mergedAt": merged,
        "author": _user("bob"),
        "repository": {"nameWithOwner": "o/r"},
    }


class TestSummarizeMerged:
    def test_sorts_by_merge_time_newest_first(self):
        search = {
            "issueCount": 3,
            "nodes": [
                _merged_pr(1, "2026-11-03T00:00:00Z"),
                _merged_pr(2, "2026-11-05T00:00:00Z"),
                None,
                _merged_pr(3, "2026-11-04T00:00:00Z"),
            ],
        }

        rows = summarize_merged(search)

        assert [r["number"] for r in rows] == [2, 3, 1]
        assert rows[0]["author"] == "bob"
