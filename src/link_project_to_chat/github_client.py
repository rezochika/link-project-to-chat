from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@dataclass
class RepoInfo:
    name: str
    full_name: str
    html_url: str
    clone_url: str
    description: str
    private: bool


_GITHUB_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


def _gh_available() -> bool:
    """Check if gh CLI is installed and authenticated."""
    return shutil.which("gh") is not None


async def _run_gh(*args: str) -> tuple[int, str, str]:
    """Run a gh CLI command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


class GitHubClient:
    """GitHub client that uses gh CLI if available, falls back to PAT + httpx."""

    def __init__(self, pat: str = ""):
        self._pat = pat
        self._use_gh = _gh_available()
        self._client = None
        if not self._use_gh:
            if httpx is None:
                raise ImportError(
                    "Neither gh CLI nor httpx available. "
                    "Install gh CLI (https://cli.github.com) or run: pip install link-project-to-chat[create]"
                )
            if not pat:
                raise ValueError("GitHub PAT required when gh CLI is not available.")
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )

    async def list_repos(self, page: int = 1, per_page: int = 5) -> tuple[list[RepoInfo], bool]:
        if self._use_gh:
            return await self._list_repos_gh(page, per_page)
        return await self._list_repos_api(page, per_page)

    async def _list_repos_gh(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        # gh repo list gives us repos; we fetch per_page+1 to detect next page
        limit = per_page + 1
        skip = (page - 1) * per_page
        code, stdout, stderr = await _run_gh(
            "repo", "list", "--limit", str(skip + limit),
            "--json", "name,nameWithOwner,url,sshUrl,isPrivate,description",
        )
        if code != 0:
            raise Exception(f"gh repo list failed: {stderr}")
        all_repos = json.loads(stdout) if stdout else []
        page_repos = all_repos[skip:skip + per_page]
        has_next = len(all_repos) > skip + per_page
        repos = [
            RepoInfo(
                name=r["name"],
                full_name=r["nameWithOwner"],
                html_url=r["url"],
                clone_url=r["url"] + ".git",
                description=r.get("description") or "",
                private=r["isPrivate"],
            )
            for r in page_repos
        ]
        return repos, has_next

    async def _list_repos_api(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        resp = await self._client.get("/user/repos", params={"sort": "updated", "page": page, "per_page": per_page})
        if resp.status_code != 200:
            raise Exception(f"GitHub API error {resp.status_code}: {resp.json().get('message', '')}")
        repos = [
            RepoInfo(name=r["name"], full_name=r["full_name"], html_url=r["html_url"],
                     clone_url=r["clone_url"], description=r.get("description") or "", private=r["private"])
            for r in resp.json()
        ]
        has_next = 'rel="next"' in resp.headers.get("link", "")
        return repos, has_next

    async def validate_repo_url(self, url: str) -> RepoInfo | None:
        match = _GITHUB_URL_RE.match(url.strip())
        if not match:
            return None
        owner, repo = match.group(1), match.group(2)
        if self._use_gh:
            return await self._validate_gh(owner, repo)
        return await self._validate_api(owner, repo)

    async def _validate_gh(self, owner: str, repo: str) -> RepoInfo | None:
        code, stdout, stderr = await _run_gh(
            "repo", "view", f"{owner}/{repo}",
            "--json", "name,nameWithOwner,url,sshUrl,isPrivate,description",
        )
        if code != 0:
            return None
        r = json.loads(stdout)
        return RepoInfo(
            name=r["name"],
            full_name=r["nameWithOwner"],
            html_url=r["url"],
            clone_url=r["url"] + ".git",
            description=r.get("description") or "",
            private=r["isPrivate"],
        )

    async def _validate_api(self, owner: str, repo: str) -> RepoInfo | None:
        resp = await self._client.get(f"/repos/{owner}/{repo}")
        if resp.status_code != 200:
            return None
        r = resp.json()
        return RepoInfo(name=r["name"], full_name=r["full_name"], html_url=r["html_url"],
                        clone_url=r["clone_url"], description=r.get("description") or "", private=r["private"])

    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self._use_gh:
            # gh repo clone handles auth automatically
            proc = await asyncio.create_subprocess_exec(
                "gh", "repo", "clone", repo.full_name, str(dest),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise Exception(f"gh repo clone failed: {stderr.decode().strip()}")
        else:
            clone_url = repo.clone_url
            if self._pat:
                clone_url = clone_url.replace("https://", f"https://{self._pat}@")
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", clone_url, str(dest),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise Exception(f"git clone failed: {stderr.decode().strip()}")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
