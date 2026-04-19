from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.github_client import GitHubClient, RepoInfo


@pytest.fixture
def client(monkeypatch):
    # Force API mode (not gh CLI) for tests
    monkeypatch.setattr("link_project_to_chat.github_client._gh_available", lambda: False)
    return GitHubClient(pat="ghp_test123")


def _mock_response(status_code: int, json_data, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.headers = headers or {}
    return resp


async def test_list_repos_returns_repos(client):
    repos_data = [
        {"name": "repo1", "full_name": "user/repo1", "html_url": "https://github.com/user/repo1", "clone_url": "https://github.com/user/repo1.git", "description": "First repo", "private": False},
        {"name": "repo2", "full_name": "user/repo2", "html_url": "https://github.com/user/repo2", "clone_url": "https://github.com/user/repo2.git", "description": "Second repo", "private": True},
    ]
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(200, repos_data, {"link": ""}))
        repos, has_next = await client.list_repos(page=1, per_page=5)
    assert len(repos) == 2
    assert repos[0].name == "repo1"
    assert repos[1].private is True
    assert has_next is False


async def test_list_repos_detects_next_page(client):
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(
            200, [{"name": "r", "full_name": "u/r", "html_url": "", "clone_url": "", "description": "", "private": False}],
            {"link": '<https://api.github.com/user/repos?page=2>; rel="next"'}
        ))
        _, has_next = await client.list_repos()
    assert has_next is True


async def test_list_repos_auth_failure(client):
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(401, {"message": "Bad credentials"}))
        with pytest.raises(Exception, match="GitHub API error 401"):
            await client.list_repos()


async def test_validate_repo_url_valid(client):
    repo_data = {"name": "myrepo", "full_name": "owner/myrepo", "html_url": "https://github.com/owner/myrepo", "clone_url": "https://github.com/owner/myrepo.git", "description": "A repo", "private": False}
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(200, repo_data))
        info = await client.validate_repo_url("https://github.com/owner/myrepo")
    assert info is not None
    assert info.full_name == "owner/myrepo"


async def test_validate_repo_url_invalid_format(client):
    info = await client.validate_repo_url("not-a-github-url")
    assert info is None


async def test_validate_repo_url_not_found(client):
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(404, {"message": "Not Found"}))
        info = await client.validate_repo_url("https://github.com/owner/nonexistent")
    assert info is None


@pytest.fixture
def gh_client(monkeypatch):
    monkeypatch.setattr("link_project_to_chat.github_client._gh_available", lambda: True)
    return GitHubClient()


async def test_list_repos_gh_includes_org_repos_and_paginates(gh_client):
    body = json.dumps([
        {"name": "user-repo", "full_name": "me/user-repo", "html_url": "https://github.com/me/user-repo",
         "clone_url": "https://github.com/me/user-repo.git", "description": "mine", "private": False},
        {"name": "org-repo", "full_name": "acme/org-repo", "html_url": "https://github.com/acme/org-repo",
         "clone_url": "https://github.com/acme/org-repo.git", "description": "org", "private": True},
    ])
    headers = (
        'HTTP/2.0 200 OK\r\n'
        'Link: <https://api.github.com/user/repos?page=2>; rel="next"\r\n'
    )
    stdout = headers + "\r\n" + body
    with patch("link_project_to_chat.github_client._run_gh", AsyncMock(return_value=(0, stdout, ""))):
        repos, has_next = await gh_client.list_repos(page=1, per_page=5)
    assert [r.full_name for r in repos] == ["me/user-repo", "acme/org-repo"]
    assert has_next is True


async def test_list_repos_gh_no_next_page(gh_client):
    body = json.dumps([])
    stdout = "HTTP/2.0 200 OK\r\n\r\n" + body
    with patch("link_project_to_chat.github_client._run_gh", AsyncMock(return_value=(0, stdout, ""))):
        repos, has_next = await gh_client.list_repos(page=1, per_page=5)
    assert repos == []
    assert has_next is False
