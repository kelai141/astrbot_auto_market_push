"""Interactive, one-time browser bootstrap for the AstrBot Cloud session.

The marketplace has no API tokens and gates email login behind hCaptcha, so the
only reliable way to obtain a session is to let a real browser log in once via
GitHub OAuth. This module drives that browser, waits for the session to become
usable (GitHub connected *and* the Cloud GitHub App installed), then persists a
Playwright ``storage_state`` document.

Everything after this step is plain HTTP, so the browser is never needed again
until the session expires.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .errors import ConfigError

logger = logging.getLogger(__name__)

LOGIN_URL = "https://cloud.astrbot.app/login"
PUBLISH_URL = "https://cloud.astrbot.app/publish"

_ME_SCRIPT = """
async () => {
  try {
    const res = await fetch('/api/v1/auth/me', { credentials: 'include' });
    const body = await res.json();
    return body && body.data ? body.data : null;
  } catch (err) {
    return null;
  }
}
"""

_CONNECTION_SCRIPT = """
async () => {
  try {
    const res = await fetch('/api/v1/auth/github/connection', { credentials: 'include' });
    const body = await res.json();
    return body && body.data ? body.data : null;
  } catch (err) {
    return null;
  }
}
"""


def _require_playwright():  # pragma: no cover - import guard
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ConfigError(
            "未安装 Playwright，无法执行浏览器登录引导。\n"
            "请执行：pip install 'astrbot-auto-market-push[browser]'\n"
            "然后执行：python -m playwright install chromium"
        ) from exc
    return async_playwright


async def bootstrap_login(
    *,
    state_path: Path,
    user_data_dir: Path | None = None,
    headless: bool = False,
    login_timeout: int = 600,
    wait_for_github: bool = True,
) -> Path:
    """Open a browser, guide the operator through login, save the session.

    Args:
        state_path: where the resulting ``storage_state.json`` is written.
        user_data_dir: persistent Chromium profile so GitHub itself stays
            logged in between bootstrap runs.
        headless: keep ``False`` unless the operator can log in without a UI.
        login_timeout: seconds to wait for the AstrBot Cloud session.
        wait_for_github: also wait until GitHub is connected and the Cloud
            GitHub App is installed, so the very first submission cannot fail
            on ``github_connection_required``.
    """
    async_playwright = _require_playwright()

    profile_dir = user_data_dir or state_path.parent / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("启动浏览器进行一次性登录引导……")
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")

            logger.info("请在打开的浏览器中完成 GitHub 登录（最多等待 %d 秒）……", login_timeout)
            account = await _wait_for(page, _ME_SCRIPT, login_timeout)
            if not account:
                raise ConfigError("等待登录超时。请确认已在浏览器中完成 GitHub / 邮箱登录后重试。")
            logger.info("登录成功，账号：%s", account.get("username") or account.get("id"))

            if wait_for_github:
                await _ensure_github_app(page, timeout=login_timeout)

            await context.storage_state(path=str(state_path))
        finally:
            await context.close()

    logger.info("会话已保存到 %s", state_path)
    return state_path


async def _wait_for(page, script: str, timeout: int, *, interval: float = 2.0):
    """Poll a fetch expression inside the page until it returns a truthy value."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            result = await page.evaluate(script)
        except Exception:  # noqa: BLE001 - page may be navigating
            result = None
        if result:
            return result
        await asyncio.sleep(interval)
    return None


async def _ensure_github_app(page, *, timeout: int) -> None:
    """Make sure GitHub is connected and the Cloud GitHub App is installed."""
    connection = await page.evaluate(_CONNECTION_SCRIPT)
    if connection and connection.get("connected"):
        logger.info("GitHub 账号已连接。")
        return

    logger.warning(
        "GitHub 尚未连接。正在打开 %s，请点击连接并安装 AstrBot Cloud GitHub App，"
        "务必把要送审的仓库加入可访问列表。",
        PUBLISH_URL,
    )
    await page.goto(PUBLISH_URL, wait_until="domcontentloaded")

    connection = await _wait_for(page, _CONNECTION_SCRIPT, timeout)
    if not connection or not connection.get("connected"):
        raise ConfigError(
            "GitHub 仍未连接。请重新运行 login 并在浏览器中完成 GitHub 授权与 App 安装。"
        )
    logger.info("GitHub 已连接：%s", connection)


def load_saved_state(path: Path) -> dict:
    """Load a previously saved storage state, with a clear error if missing."""
    if not path.exists():
        raise ConfigError(f"未找到会话文件 {path}。请先执行 `ampush login`。")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"会话文件 {path} 不是合法 JSON：{exc}") from exc
