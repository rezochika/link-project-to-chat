from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


@dataclass
class RepoInfo:
    name: str
    full_name: str
    html_url: str
    clone_url: str
    description: str
    private: bool


_GITHUB_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


class GitHubClient:
    def __init__(self, pat: str):
        if httpx is None:
            raise ImportError(
                "httpx is required for GitHub integration. "
                "Install with: pip install link-project-to-chat[create]"
            )
        self._pat = pat
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
        resp = await self._client.get(f"/repos/{owner}/{repo}")
        if resp.status_code != 200:
            return None
        r = resp.json()
        return RepoInfo(name=r["name"], full_name=r["full_name"], html_url=r["html_url"],
                        clone_url=r["clone_url"], description=r.get("description") or "", private=r["private"])

    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
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
        await self._client.aclose()
