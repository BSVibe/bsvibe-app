"""Tests for GithubClient — the httpx wrapper. httpx is mocked via respx;
no real GitHub calls. (rule python-testing: never call real APIs in tests.)"""

from __future__ import annotations

import httpx
import pytest
import respx

from plugin.github.client import GithubClient, MergeResult

API = "https://api.github.com"


@pytest.fixture
def client() -> GithubClient:
    return GithubClient("tok-123", base_url=API)


class TestCompareBranch:
    """The remote's answer to "is there anything to open a PR from".

    A ``client_attach`` run pushes its own branch from the founder's machine
    (#735), so the server has no checkout to measure. Asking github is not a
    convenience — it is the only authority, and a local belief about the push
    can be wrong in both directions.
    """

    @respx.mock
    async def test_reports_how_far_ahead_the_branch_is(self, client):
        respx.get(f"{API}/repos/o/r/compare/main...run%2Fabc12345").mock(
            return_value=httpx.Response(200, json={"ahead_by": 3, "behind_by": 1})
        )
        got = await client.compare_branch("o", "r", base="main", head="run/abc12345")
        assert got == {"exists": True, "ahead_by": 3}

    @respx.mock
    async def test_an_unknown_branch_is_absent_not_an_error(self, client):
        """A run that committed nothing never pushed, so the branch is simply not
        there. Raising would turn "nothing to deliver" into a delivery failure."""
        respx.get(url__startswith=f"{API}/repos/o/r/compare/").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        got = await client.compare_branch("o", "r", base="main", head="run/nope")
        assert got == {"exists": False, "ahead_by": 0}

    @respx.mock
    async def test_a_branch_level_with_base_is_ahead_by_zero(self, client):
        respx.get(url__startswith=f"{API}/repos/o/r/compare/").mock(
            return_value=httpx.Response(200, json={"ahead_by": 0})
        )
        got = await client.compare_branch("o", "r", base="main", head="run/same")
        assert got["ahead_by"] == 0

    @respx.mock
    async def test_a_real_failure_still_raises(self, client):
        """403 (rate limit, revoked token) is not "no branch" — swallowing it
        would report every run as having changed nothing."""
        respx.get(url__startswith=f"{API}/repos/o/r/compare/").mock(
            return_value=httpx.Response(403, json={"message": "rate limited"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.compare_branch("o", "r", base="main", head="run/x")


class TestOpenPr:
    @respx.mock
    async def test_open_pr_posts_and_returns_json(self, client):
        route = respx.post(f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(
                201, json={"number": 15, "html_url": f"{API.replace('api.', '')}/o/r/pull/15"}
            )
        )
        data = await client.open_pr("o", "r", head="feat", base="main", title="T", body="B")
        assert data["number"] == 15
        assert route.called
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer tok-123"
        assert b'"head"' in sent.content and b'"feat"' in sent.content

    @respx.mock
    async def test_open_pr_raises_on_error(self, client):
        # A 422 with no existing PR for the head is a genuine validation error —
        # after the idempotency lookup finds nothing, open_pr still raises.
        respx.post(f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(422, json={"message": "bad"})
        )
        respx.get(url__startswith=f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(200, json=[])
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.open_pr("o", "r", head="feat", base="main", title="T", body="B")

    @respx.mock
    async def test_open_pr_reuses_existing_pr_on_422(self, client):
        # Re-deliver (conflict-resolution re-push) to a branch that already has a
        # PR → 422; open_pr must return the existing OPEN PR, not raise.
        post = respx.post(f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(
                422,
                json={"errors": [{"message": "A pull request already exists for o:feat."}]},
            )
        )
        get = respx.get(url__startswith=f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(200, json=[{"number": 2, "state": "open"}])
        )
        data = await client.open_pr("o", "r", head="feat", base="main", title="T", body="B")
        assert data["number"] == 2
        assert post.called and get.called

    @respx.mock
    async def test_open_pr_head_filter_is_owner_qualified(self, client):
        respx.post(f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(422, json={"errors": [{"message": "already exists"}]})
        )
        get = respx.get(url__startswith=f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(200, json=[{"number": 9}])
        )
        # A slash-bearing branch is still filtered as owner:branch.
        await client.open_pr("o", "r", head="bsvibe/run-abc", base="main", title="T")
        assert get.calls.last.request.url.params.get("head") == "o:bsvibe/run-abc"
        assert get.calls.last.request.url.params.get("state") == "open"


class TestUpdateAndGetPr:
    @respx.mock
    async def test_update_pr_patches(self, client):
        route = respx.patch(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "title": "New"})
        )
        data = await client.update_pr("o", "r", 15, title="New")
        assert data["title"] == "New"
        assert route.calls.last.request.method == "PATCH"

    @respx.mock
    async def test_get_pr(self, client):
        respx.get(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "state": "open"})
        )
        data = await client.get_pr("o", "r", 15)
        assert data["state"] == "open"

    @respx.mock
    async def test_close_pr_sets_state_closed(self, client):
        route = respx.patch(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "state": "closed"})
        )
        await client.close_pr("o", "r", 15)
        assert b'"closed"' in route.calls.last.request.content


class TestMergePr:
    @respx.mock
    async def test_merge_pr_200_returns_merged(self, client):
        route = respx.put(f"{API}/repos/o/r/pulls/15/merge").mock(
            return_value=httpx.Response(200, json={"sha": "abc123", "merged": True})
        )
        result = await client.merge_pr("o", "r", 15)
        assert result == MergeResult(status="merged", merged=True, sha="abc123")
        assert route.called
        sent = route.calls.last.request
        assert sent.method == "PUT"
        assert b'"merge_method"' in sent.content
        assert b'"squash"' in sent.content

    @respx.mock
    async def test_merge_pr_honours_method(self, client):
        route = respx.put(f"{API}/repos/o/r/pulls/15/merge").mock(
            return_value=httpx.Response(200, json={"sha": "def456"})
        )
        result = await client.merge_pr("o", "r", 15, method="rebase")
        assert result.merged is True
        assert b'"rebase"' in route.calls.last.request.content

    @respx.mock
    async def test_merge_pr_405_not_mergeable_no_raise(self, client):
        respx.put(f"{API}/repos/o/r/pulls/15/merge").mock(
            return_value=httpx.Response(405, json={"message": "not mergeable"})
        )
        result = await client.merge_pr("o", "r", 15)
        assert result == MergeResult(status="not_mergeable", merged=False, sha=None)

    @respx.mock
    async def test_merge_pr_409_head_changed_no_raise(self, client):
        respx.put(f"{API}/repos/o/r/pulls/15/merge").mock(
            return_value=httpx.Response(409, json={"message": "head changed"})
        )
        result = await client.merge_pr("o", "r", 15)
        assert result == MergeResult(status="head_changed", merged=False, sha=None)

    @respx.mock
    async def test_merge_pr_500_raises(self, client):
        respx.put(f"{API}/repos/o/r/pulls/15/merge").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.merge_pr("o", "r", 15)


class TestGetCheckRuns:
    @respx.mock
    async def test_get_check_runs_parses_list(self, client):
        respx.get(f"{API}/repos/o/r/commits/deadbeef/check-runs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "check_runs": [
                        {"name": "build", "status": "completed", "conclusion": "success"},
                        {"name": "test", "status": "in_progress", "conclusion": None},
                    ],
                },
            )
        )
        runs = await client.get_check_runs("o", "r", "deadbeef")
        assert len(runs) == 2
        assert runs[0]["name"] == "build"
        assert runs[0]["conclusion"] == "success"
        assert runs[1]["status"] == "in_progress"

    @respx.mock
    async def test_get_check_runs_missing_key_returns_empty(self, client):
        respx.get(f"{API}/repos/o/r/commits/deadbeef/check-runs").mock(
            return_value=httpx.Response(200, json={"total_count": 0})
        )
        runs = await client.get_check_runs("o", "r", "deadbeef")
        assert runs == []

    @respx.mock
    async def test_get_check_runs_raises_on_error(self, client):
        respx.get(f"{API}/repos/o/r/commits/deadbeef/check-runs").mock(
            return_value=httpx.Response(404, json={"message": "no ref"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_check_runs("o", "r", "deadbeef")


class TestComments:
    @respx.mock
    async def test_post_comment(self, client):
        route = respx.post(f"{API}/repos/o/r/issues/7/comments").mock(
            return_value=httpx.Response(201, json={"id": 99, "html_url": "u"})
        )
        data = await client.post_comment("o", "r", 7, "hello")
        assert data["id"] == 99
        assert b"hello" in route.calls.last.request.content

    @respx.mock
    async def test_delete_comment_returns_status(self, client):
        respx.delete(f"{API}/repos/o/r/issues/comments/99").mock(return_value=httpx.Response(204))
        status = await client.delete_comment("o", "r", 99)
        assert status == 204

    @respx.mock
    async def test_delete_comment_404_is_returned_not_raised(self, client):
        respx.delete(f"{API}/repos/o/r/issues/comments/99").mock(return_value=httpx.Response(404))
        status = await client.delete_comment("o", "r", 99)
        assert status == 404


class TestListIssues:
    @respx.mock
    async def test_list_issues_returns_list(self, client):
        respx.get(f"{API}/repos/o/r/issues").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"number": 1, "title": "a", "state": "open"},
                    {"number": 2, "title": "b", "state": "open"},
                ],
            )
        )
        issues = await client.list_issues("o", "r")
        assert len(issues) == 2
        assert issues[0]["number"] == 1

    @respx.mock
    async def test_list_issues_carries_state_and_per_page(self, client):
        route = respx.get(f"{API}/repos/o/r/issues").mock(return_value=httpx.Response(200, json=[]))
        await client.list_issues("o", "r", state="closed", per_page=5)
        url = str(route.calls[0].request.url)
        assert "state=closed" in url
        assert "per_page=5" in url

    @respx.mock
    async def test_list_issues_unexpected_shape_returns_empty(self, client):
        respx.get(f"{API}/repos/o/r/issues").mock(
            return_value=httpx.Response(200, json={"unexpected": "dict"})
        )
        issues = await client.list_issues("o", "r")
        assert issues == []


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.get(f"{API}/repos/o/r/pulls/1").mock(
            return_value=httpx.Response(200, json={"number": 1, "state": "open"})
        )
        async with httpx.AsyncClient() as injected:
            gh = GithubClient("tok", base_url=API, client=injected)
            data = await gh.get_pr("o", "r", 1)
        assert data["number"] == 1
