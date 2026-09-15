"""Orchestration: build the clients, evaluate targets, submit, repeat."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import datetime

from pydantic import BaseModel, Field

from .cloud_client import CloudClient
from .config import AppConfig
from .errors import SessionExpiredError
from .github_client import GitHubClient
from .models import ActionKind, ActionPlan, SubmitOutcome, utcnow
from .notify import EVENT_SESSION_EXPIRED, Notifier
from .state import StateStore
from .submitter import Submitter
from .watcher import Watcher

logger = logging.getLogger(__name__)


class CycleReport(BaseModel):
    """Result of a single poll/submit cycle."""

    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    plans: list[ActionPlan] = Field(default_factory=list)
    outcomes: list[SubmitOutcome] = Field(default_factory=list)
    session_expired: bool = False

    @property
    def submitted(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.submitted and not outcome.dry_run)

    @property
    def simulated(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.dry_run)

    @property
    def failed(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.submitted and not outcome.dry_run)

    @property
    def skipped(self) -> int:
        return sum(1 for plan in self.plans if plan.kind is ActionKind.SKIP)

    @property
    def errors(self) -> int:
        return sum(1 for plan in self.plans if plan.kind is ActionKind.ERROR)


class Engine:
    """Owns the lifecycle of every collaborator and runs poll cycles."""

    def __init__(self, config: AppConfig, *, force: bool = False) -> None:
        self.config = config
        self.force = force
        self.state = StateStore(config.state.path)
        self.github = GitHubClient(
            token=config.github.token,
            api_url=config.github.api_url,
            timeout=config.github.timeout_seconds,
            user_agent=config.github.user_agent,
        )
        self.cloud = CloudClient(
            storage_state=(config.cloud.storage_state_json or config.cloud.storage_state),
            base_url=config.cloud.base_url,
            timeout=config.cloud.timeout_seconds,
            user_agent=config.cloud.user_agent,
        )
        self.notifier = Notifier(config.notify)
        self.options: dict = {}
        self._closed = False

    async def __aenter__(self) -> Engine:
        await self._load_options()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _load_options(self) -> None:
        try:
            self.options = await self.cloud.get_options()
            logger.debug("云端选项：%s", self.options)
        except SessionExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001 - options are advisory only
            logger.warning("读取 /market/options 失败，将使用本地默认限流：%s", exc)
            self.options = {}

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.cloud.aclose()
        await self.github.aclose()
        self.state.close()

    # ------------------------------------------------------------------ health

    async def health(self) -> dict:
        """Collect diagnostics for the ``doctor`` command."""
        report: dict = {
            "cloud_base_url": self.config.cloud.base_url,
            "session": {"ok": False, "error": None},
            "account": None,
            "options": self.options,
            "targets": [],
            "state_path": str(self.config.state.path),
            "github_token": bool(self.config.github.token),
        }

        try:
            account = await self.cloud.get_me()
            report["session"] = {"ok": account is not None, "error": None}
            report["account"] = {
                "username": (account or {}).get("username"),
                "role": (account or {}).get("role"),
            }
        except SessionExpiredError as exc:
            report["session"] = {"ok": False, "error": str(exc)}
            return report
        except Exception as exc:  # noqa: BLE001
            report["session"] = {"ok": False, "error": str(exc)}
            return report

        try:
            connection = await self.cloud.get_github_connection()
        except Exception as exc:  # noqa: BLE001
            connection = {"connected": False, "error": str(exc)}
        report["github_connection"] = connection

        for target in self.config.watch.repositories:
            entry: dict = {"repo": target.repo, "enabled": target.enabled}
            try:
                installation_id, repository_id, repository = await self.cloud.resolve_repository(
                    target.ref_name.full_name
                )
                entry.update(
                    {
                        "github_app_ok": True,
                        "installation_id": installation_id,
                        "repository_id": repository_id,
                        "default_branch": repository.get("default_branch"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                entry.update({"github_app_ok": False, "github_app_error": str(exc)})
            report["targets"].append(entry)

        return report

    # ------------------------------------------------------------------- cycle

    async def cycle(self) -> CycleReport:
        report = CycleReport()
        watcher = Watcher(
            config=self.config, github=self.github, cloud=self.cloud, state=self.state
        )
        submitter = Submitter(
            config=self.config,
            cloud=self.cloud,
            state=self.state,
            notifier=self.notifier,
            options=self.options,
            force=self.force,
        )

        plans = await watcher.evaluate_all()
        report.plans = plans

        try:
            for plan in plans:
                if plan.kind is not ActionKind.SUBMIT:
                    continue
                outcome = await submitter.execute(plan)
                report.outcomes.append(outcome)
        except SessionExpiredError as exc:
            report.session_expired = True
            logger.error("会话已失效：%s", exc)
            await self.notifier.send(
                EVENT_SESSION_EXPIRED,
                "AstrBot Cloud 会话已失效",
                str(exc),
            )

        report.finished_at = utcnow()
        return report

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Poll until stopped. A lost session halts the loop instead of spinning."""
        interval = self.config.watch.interval_seconds
        jitter = max(0, self.config.watch.jitter_seconds)

        logger.info(
            "开始轮询 %d 个仓库，间隔 %d 秒（抖动 ±%d 秒）",
            len(self.config.watch.repositories),
            interval,
            jitter,
        )

        while True:
            if stop_event is not None and stop_event.is_set():
                break

            try:
                report = await self.cycle()
            except SessionExpiredError:
                logger.error("会话失效，停止轮询。请重新执行 `ampush login`。")
                await self.notifier.send(
                    EVENT_SESSION_EXPIRED,
                    "AstrBot Cloud 会话已失效",
                    "轮询已停止，请重新登录并刷新 storage_state。",
                )
                return

            logger.info(
                "本轮结束：送审 %d，模拟 %d，失败 %d，跳过 %d，错误 %d",
                report.submitted,
                report.simulated,
                report.failed,
                report.skipped,
                report.errors,
            )

            delay = interval + random.uniform(-jitter, jitter)
            delay = max(5.0, delay)
            if stop_event is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                if stop_event.is_set():
                    break
            else:
                await asyncio.sleep(delay)
