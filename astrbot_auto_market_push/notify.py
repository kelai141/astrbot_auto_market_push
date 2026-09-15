"""Optional webhook notifications.

Deliberately dependency free: a single JSON POST, which works with Discord,
Slack, Feishu/Lark bots, ntfy, or any custom receiver.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import NotifyConfig

logger = logging.getLogger(__name__)

EVENT_SUBMITTED = "submitted"
EVENT_FAILED = "failed"
EVENT_SESSION_EXPIRED = "session_expired"
EVENT_RATE_LIMITED = "rate_limited"


class Notifier:
    """Fire-and-forget webhook notifier."""

    def __init__(self, config: NotifyConfig, *, timeout: float = 15.0) -> None:
        self.config = config
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.webhook_url)

    def allows(self, event: str) -> bool:
        if not self.enabled:
            return False
        events = self.config.events or []
        return not events or event in events

    async def send(self, event: str, title: str, detail: str = "", **extra: Any) -> bool:
        """Post a notification. Never raises: notification failure is not fatal."""
        if not self.allows(event):
            return False

        payload = {
            "event": event,
            "title": title,
            "detail": detail,
            "source": "astrbot-auto-market-push",
            **extra,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self.config.webhook_url, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("通知发送失败（%s）：%s", event, exc)
            return False
        return True
