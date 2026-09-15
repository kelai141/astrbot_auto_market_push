"""AstrBot Cloud (plugin marketplace) API client.

Reverse engineered from ``https://cloud.astrbot.app`` on 2026-09-15. The
marketplace has no public API tokens; authentication is a plain cookie session
obtained through the browser OAuth flow. This client therefore consumes a
Playwright ``storage_state`` document and replays the cookies over HTTP.

Wire format reference (all under ``/api/v1``)::

    GET  /auth/me
    GET  /market/options
    GET  /auth/github/connection
    GET  /market/github/namespaces
    GET  /market/github/installations/{id}/repositories
    POST /market/plugins/parse/github
    POST /market/plugins
    GET  /market/plugins?search=...

Every response is an envelope ``{"code": int, "msg": str, "data": ...}``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .errors import (
    CloudAPIError,
    CloudRateLimitedError,
    GitHubAppError,
    SessionExpiredError,
)
from .models import MarketPlugin, ParsedPlugin

logger = logging.getLogger(__name__)

# Error codes the marketplace uses when the Cloud GitHub App is not ready.
_CONNECTION_REQUIRED = "github_connection_required"
_INSTALLATION_REQUIRED = "github_app_installation_required"


def load_storage_state(source: str | Path | dict[str, Any] | None) -> dict[str, Any]:
    """Normalize several cookie sources into a Playwright storage state dict.

    Accepted inputs:

    * a path to a ``storage_state.json`` produced by ``ampush login``
    * a JSON string containing that document
    * the already parsed document
    * a bare ``{"name": "value"}`` cookie mapping
    """
    if source is None:
        raise SessionExpiredError(
            "未配置 AstrBot Cloud 会话。请先执行 `ampush login` 生成 storage_state，"
            "或通过 cloud.storage_state / AMP_CLOUD_STORAGE_STATE 提供。"
        )

    if isinstance(source, dict):
        data = source
    elif isinstance(source, Path):
        data = _read_json_file(source)
    else:
        text = str(source).strip()
        if not text:
            raise SessionExpiredError("AstrBot Cloud 会话内容为空。")
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SessionExpiredError(f"storage_state JSON 解析失败：{exc}") from exc
        else:
            data = _read_json_file(Path(text))

    if "cookies" not in data:
        # Allow a bare {name: value} mapping for hand written configs.
        if all(isinstance(value, str) for value in data.values()) and data:
            data = {"cookies": [{"name": k, "value": v} for k, v in data.items()]}
        else:
            raise SessionExpiredError(
                "storage_state 缺少 `cookies` 字段。请使用 `ampush login` 重新生成。"
            )
    return data


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SessionExpiredError(
            f"会话文件不存在：{path}。请先执行 `ampush login` 完成一次浏览器登录。"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionExpiredError(f"无法读取会话文件 {path}：{exc}") from exc


class CloudClient:
    """Cookie-authenticated AstrBot Cloud API client."""

    def __init__(
        self,
        *,
        storage_state: str | Path | dict[str, Any] | None = None,
        base_url: str = "https://cloud.astrbot.app/api/v1",
        timeout: float = 30.0,
        user_agent: str = "astrbot-auto-market-push",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.storage_state = load_storage_state(storage_state)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Accept": "application/json", "User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        self._install_cookies(self.storage_state)

    def _install_cookies(self, state: dict[str, Any]) -> None:
        for cookie in state.get("cookies", []):
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            domain = cookie.get("domain") or ""
            path = cookie.get("path") or "/"
            try:
                self._client.cookies.set(str(name), str(value), domain=str(domain), path=str(path))
            except Exception:  # pragma: no cover - defensive
                logger.debug("跳过无法写入的 cookie：%s", name, exc_info=True)

    async def __aenter__(self) -> CloudClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- plumbing

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method, url, json=json_body, params=params or None
            )
        except httpx.HTTPError as exc:
            raise CloudAPIError(f"请求 AstrBot Cloud 失败：{exc}") from exc

        if response.status_code == 401:
            raise SessionExpiredError(
                "AstrBot Cloud 会话已失效（401 请先登录）。"
                "请重新执行 `ampush login` 刷新 storage_state。"
            )

        data: Any
        try:
            data = response.json()
        except ValueError:
            if response.status_code >= 400:
                raise CloudAPIError(
                    f"AstrBot Cloud 返回 HTTP {response.status_code}：{response.text[:200]}",
                    status_code=response.status_code,
                ) from None
            raise CloudAPIError("AstrBot Cloud 返回了非 JSON 响应。") from None

        code = data.get("code")
        if response.status_code >= 400 or (code is not None and code != 200):
            message = str(data.get("msg") or response.text[:200])
            self._raise_structured(response.status_code, code, message, data)

        return data.get("data")

    @staticmethod
    def _raise_structured(status_code: int, code: int | None, message: str, payload: Any) -> None:
        blob = json.dumps(payload, ensure_ascii=False, default=str)
        if _CONNECTION_REQUIRED in blob:
            raise GitHubAppError(
                "AstrBot Cloud 账号尚未授权 GitHub，请先在 cloud.astrbot.app 完成 GitHub 连接。",
                reason="connection_required",
            )
        if _INSTALLATION_REQUIRED in blob:
            raise GitHubAppError(
                "AstrBot Cloud GitHub App 未安装到该仓库，或未授予该仓库访问权限。",
                reason="installation_required",
            )
        lowered = message.lower()
        if status_code == 429 or "频繁" in message or "rate" in lowered or "limit" in lowered:
            raise CloudRateLimitedError(
                message, status_code=status_code, code=code, payload=payload
            )
        raise CloudAPIError(message, status_code=status_code, code=code, payload=payload)

    # ------------------------------------------------------------------- reads

    async def get_me(self) -> dict[str, Any] | None:
        return await self._request("GET", "/auth/me")

    async def get_options(self) -> dict[str, Any]:
        return await self._request("GET", "/market/options") or {}

    async def get_github_connection(self) -> dict[str, Any]:
        return await self._request("GET", "/auth/github/connection") or {}

    async def list_namespaces(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/market/github/namespaces") or []

    async def list_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        return (
            await self._request(
                "GET", f"/market/github/installations/{installation_id}/repositories"
            )
            or []
        )

    async def search_plugins(
        self, search: str | None = None, *, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                "/market/plugins",
                params={"page": page, "page_size": page_size, "search": search},
            )
            or {}
        )

    async def find_plugin_by_repo(self, repository_url: str) -> MarketPlugin | None:
        """Locate the marketplace record that points at ``repository_url``."""
        if not repository_url:
            return None
        target = repository_url.rstrip("/").removesuffix(".git").lower()
        slug = target.rsplit("/", 1)[-1]

        for query in (slug, repository_url):
            payload = await self.search_plugins(query, page_size=20)
            for item in payload.get("items", []):
                candidate = str(item.get("repository_url") or "").rstrip("/")
                candidate = candidate.removesuffix(".git").lower()
                if candidate == target:
                    return MarketPlugin.model_validate(item)
        return None

    # -------------------------------------------------------------- resolution

    async def resolve_repository(self, repo_full_name: str) -> tuple[int, int, dict[str, Any]]:
        """Map ``owner/name`` to ``(installation_id, repository_id, repo)``.

        Raises :class:`GitHubAppError` when the account has not connected
        GitHub or the Cloud GitHub App cannot see the repository.
        """
        connection = await self.get_github_connection()
        if not connection.get("connected"):
            raise GitHubAppError(
                "AstrBot Cloud 账号尚未连接 GitHub。",
                reason="connection_required",
            )

        namespaces = await self.list_namespaces()
        if not namespaces:
            raise GitHubAppError(
                "未找到任何 GitHub App 安装。"
                "请先在 cloud.astrbot.app 安装 AstrBot Cloud GitHub App。",
                reason="installation_required",
            )

        wanted = repo_full_name.lower()
        for namespace in namespaces:
            installation_id = namespace.get("installation_id")
            if installation_id is None:
                continue
            for repository in await self.list_repositories(int(installation_id)):
                full_name = str(repository.get("full_name") or "")
                if full_name.lower() == wanted:
                    return int(installation_id), int(repository["id"]), repository

        raise GitHubAppError(
            f"GitHub App 未授予仓库 {repo_full_name} 的访问权限。"
            "请在 GitHub App 安装设置中把该仓库加入可访问列表。",
            reason="installation_required",
        )

    # -------------------------------------------------------------- submission

    async def parse_github(self, installation_id: int, repository_id: int) -> ParsedPlugin:
        """Ask the marketplace to parse ``metadata.yaml`` server side."""
        data = await self._request(
            "POST",
            "/market/plugins/parse/github",
            json_body={
                "github_installation_id": installation_id,
                "github_repository_id": repository_id,
            },
        )
        if not isinstance(data, dict):
            raise CloudAPIError("解析接口返回了非预期的数据结构。", payload=data)
        return ParsedPlugin.model_validate(data)

    async def submit_plugin(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Send a plugin for review. ``record`` must be the full wire payload."""
        data = await self._request("POST", "/market/plugins", json_body=record)
        if data is None:
            return None
        if isinstance(data, dict):
            return data
        return {"raw": data}


def build_submission_payload(
    parsed: ParsedPlugin, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the exact payload ``POST /market/plugins`` expects.

    Mirrors the marketplace front-end: every field of the parse response is
    echoed back, with ``publish_mode``/``existing_plugin`` stripped because
    they are response-only hints.
    """
    payload = parsed.model_dump(exclude_none=False)
    payload.pop("publish_mode", None)
    payload.pop("existing_plugin", None)

    payload.setdefault("authors", [parsed.author] if parsed.author else [])
    payload.setdefault("support_platforms", [])
    payload.setdefault("tags", [])
    payload.setdefault("categories", [])
    payload.setdefault("audit_payload", {})

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        payload[key] = value

    return payload
