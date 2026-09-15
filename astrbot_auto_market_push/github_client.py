"""Read-only GitHub REST client used to detect version changes.

This client never writes to GitHub. It only reads ``metadata.yaml``, the head
commit of a branch, tags and releases so the watcher can decide whether the
tracked repository is ahead of what the marketplace already has.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any

import httpx
import yaml

from .errors import GitHubError
from .models import PluginMetadata, RepoRef, WatchTarget

logger = logging.getLogger(__name__)


class GitHubClient:
    """Minimal async GitHub REST wrapper (only what the watcher needs)."""

    def __init__(
        self,
        *,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        timeout: float = 30.0,
        user_agent: str = "astrbot-auto-market-push",
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": user_agent,
        }
        self._has_token = bool(token)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- helpers

    async def _get(self, url: str, **params: Any) -> httpx.Response:
        cleaned = {key: value for key, value in params.items() if value is not None}
        try:
            response = await self._client.get(url, params=cleaned or None)
        except httpx.HTTPError as exc:
            raise GitHubError(f"访问 GitHub API 失败：{exc}") from exc

        if response.status_code == 401:
            if self._has_token:
                raise GitHubError(
                    "GitHub 返回 401 Bad credentials：配置的 Token 无效或已过期。"
                    "请更新 github.token / GITHUB_TOKEN，或清空该配置改用匿名访问"
                    "（匿名仅能读取公开仓库，且限流为 60 次/小时）。",
                    status_code=401,
                )
            raise GitHubError("GitHub 返回 401：匿名请求被拒绝。", status_code=401)
        if response.status_code == 404:
            raise GitHubError(
                f"GitHub 资源不存在：{url}"
                + ("（也可能是当前 Token 无权访问该私有仓库）" if self._has_token else ""),
                status_code=404,
            )
        if response.status_code in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                reset = response.headers.get("x-ratelimit-reset", "?")
                raise GitHubError(
                    "GitHub API 速率限制已用尽"
                    f"（剩余 0，重置时间戳 {reset}）。请配置 github.token 提高配额。",
                    status_code=response.status_code,
                )
            raise GitHubError(
                f"GitHub API 拒绝访问（HTTP {response.status_code}）：{response.text[:200]}",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub API 返回 HTTP {response.status_code}：{response.text[:200]}",
                status_code=response.status_code,
            )
        return response

    # ------------------------------------------------------------------- reads

    async def get_repository(self, repo: RepoRef) -> dict[str, Any]:
        response = await self._get(f"/repos/{repo.owner}/{repo.name}")
        return response.json()

    async def get_default_branch(self, repo: RepoRef) -> str:
        payload = await self.get_repository(repo)
        branch = payload.get("default_branch")
        if not branch:
            raise GitHubError(f"仓库 {repo.full_name} 未返回 default_branch。")
        return str(branch)

    async def get_branch_head(self, repo: RepoRef, branch: str) -> str | None:
        response = await self._get(f"/repos/{repo.owner}/{repo.name}/branches/{branch}")
        payload = response.json()
        return (payload.get("commit") or {}).get("sha")

    async def get_commit_sha(self, repo: RepoRef, ref: str) -> str | None:
        """Resolve a branch/tag/commit-ish to a commit SHA."""
        response = await self._get(f"/repos/{repo.owner}/{repo.name}/commits/{ref}")
        payload = response.json()
        return payload.get("sha")

    async def get_file_text(self, repo: RepoRef, path: str, ref: str | None) -> str | None:
        """Fetch a UTF-8 text file, returning ``None`` when it does not exist."""
        try:
            response = await self._get(f"/repos/{repo.owner}/{repo.name}/contents/{path}", ref=ref)
        except GitHubError as exc:
            if exc.status_code == 404:
                return None
            raise

        payload = response.json()
        if isinstance(payload, list):
            raise GitHubError(f"{repo.full_name}:{path} 是目录而不是文件。")

        encoding = payload.get("encoding")
        content = payload.get("content")
        if encoding == "base64" and content:
            try:
                return base64.b64decode(content).decode("utf-8-sig")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise GitHubError(f"{repo.full_name}:{path} 不是合法的 UTF-8 文本：{exc}") from exc
        if payload.get("download_url"):
            raw = await self._client.get(payload["download_url"])
            raw.raise_for_status()
            return raw.text
        return None

    async def get_latest_release(self, repo: RepoRef) -> dict[str, Any] | None:
        try:
            response = await self._get(f"/repos/{repo.owner}/{repo.name}/releases/latest")
        except GitHubError as exc:
            if exc.status_code == 404:
                return None
            raise
        return response.json()

    async def list_tags(self, repo: RepoRef, *, limit: int = 20) -> list[str]:
        response = await self._get(f"/repos/{repo.owner}/{repo.name}/tags", per_page=limit)
        return [item.get("name", "") for item in response.json()]

    # ------------------------------------------------------------------ domain

    async def fetch_metadata(self, target: WatchTarget) -> tuple[PluginMetadata, str | None]:
        """Return the parsed ``metadata.yaml`` plus the commit it came from.

        When ``ref`` is not configured the repository default branch is used,
        which is exactly what AstrBot Cloud parses when a submission is made.
        """
        repo = target.ref_name
        ref = target.ref
        if not ref:
            ref = await self.get_default_branch(repo)

        head_sha = await self.get_commit_sha(repo, ref)
        raw = await self.get_file_text(repo, target.metadata_path, ref)
        if raw is None:
            raise GitHubError(
                f"{repo.full_name} 的 {target.metadata_path} 不存在（ref={ref}）。"
                "AstrBot 插件必须带有 metadata.yaml。"
            )

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise GitHubError(
                f"{repo.full_name}:{target.metadata_path} 不是合法 YAML：{exc}"
            ) from exc
        if not isinstance(data, dict):
            raise GitHubError(f"{repo.full_name}:{target.metadata_path} 顶层必须是映射。")

        for required in ("name", "version", "author"):
            if not data.get(required):
                raise GitHubError(
                    f"{repo.full_name}:{target.metadata_path} 缺少必填字段 {required!r}。"
                )

        try:
            metadata = PluginMetadata.model_validate(data)
        except Exception as exc:  # pydantic ValidationError
            raise GitHubError(f"{repo.full_name}:{target.metadata_path} 解析失败：{exc}") from exc

        return metadata, head_sha
