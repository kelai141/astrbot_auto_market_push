"""Change detection: decide whether a tracked repository needs a submission.

The single source of truth for "is this repository ahead of the marketplace"
is the ``version`` field inside ``metadata.yaml`` on the tracked branch, which
is exactly what AstrBot Cloud parses server side.
"""

from __future__ import annotations

import logging

from .cloud_client import CloudClient
from .config import AppConfig
from .errors import GitHubError
from .github_client import GitHubClient
from .models import (
    ActionKind,
    ActionPlan,
    MarketPlugin,
    SubmissionStatus,
    WatchTarget,
    normalize_version,
    version_gt,
)
from .state import StateStore

logger = logging.getLogger(__name__)


class Watcher:
    """Builds an :class:`ActionPlan` per tracked repository."""

    def __init__(
        self,
        *,
        config: AppConfig,
        github: GitHubClient,
        cloud: CloudClient,
        state: StateStore,
    ) -> None:
        self.config = config
        self.github = github
        self.cloud = cloud
        self.state = state

    async def evaluate(self, target: WatchTarget) -> ActionPlan:
        repo = target.ref_name
        repo_key = repo.full_name
        repository_url = f"https://github.com/{repo_key}"

        if not target.enabled:
            return ActionPlan(target=target, kind=ActionKind.SKIP, reason="仓库已被禁用")

        try:
            metadata, commit = await self.github.fetch_metadata(target)
        except GitHubError as exc:
            return ActionPlan(target=target, kind=ActionKind.ERROR, reason=f"读取仓库失败：{exc}")

        local_version = metadata.normalized_version

        market: MarketPlugin | None = None
        try:
            market = await self.cloud.find_plugin_by_repo(repository_url)
        except Exception as exc:  # noqa: BLE001 - marketplace lookup is best effort
            logger.warning("%s：查询插件市场记录失败，将按“未上架”处理：%s", repo_key, exc)

        online_version = market.current_online_version if market else ""
        pending = bool(market and market.submission_status is SubmissionStatus.PENDING)

        self.state.touch_repo(
            repo_key=repo_key,
            repo=repository_url,
            ref=target.ref,
            version=local_version,
            commit=commit,
            market_plugin_id=market.plugin_id if market else None,
        )

        plan = ActionPlan(
            target=target,
            kind=ActionKind.SKIP,
            reason="",
            local_version=local_version,
            local_commit=commit,
            online_version=online_version,
            pending=pending,
        )

        if not local_version:
            plan.reason = "metadata.yaml 缺少 version 字段"
            return plan

        if self.state.was_submitted(repo_key, local_version):
            plan.reason = f"版本 {local_version} 已经提交过，跳过重复送审"
            return plan

        if pending:
            pending_version = normalize_version(market.latest_submission_version) if market else ""
            if pending_version and pending_version == local_version:
                plan.reason = f"版本 {local_version} 正在审核中，等待结果"
            else:
                plan.reason = (
                    f"存在待审核提交（版本 {pending_version or '未知'}），"
                    "为避免占用审核名额暂时跳过"
                )
            return plan

        if not target.auto_submit:
            plan.reason = f"已发现新版本 {local_version}，但该仓库未开启自动送审"
            return plan

        if online_version and local_version == online_version:
            plan.reason = f"线上版本已是 {online_version}，无需送审"
            return plan

        if (
            target.require_version_change
            and online_version
            and not version_gt(local_version, online_version)
        ):
            plan.reason = f"本地版本 {local_version} 不高于线上版本 {online_version}，跳过"
            return plan

        plan.kind = ActionKind.SUBMIT
        plan.reason = f"检测到版本变化（线上 {online_version or '未上架'} → 本地 {local_version}）"
        return plan

    async def evaluate_all(self) -> list[ActionPlan]:
        plans: list[ActionPlan] = []
        for target in self.config.watch.repositories:
            plan = await self.evaluate(target)
            plans.append(plan)
            logger.info(
                "[%s] %s - %s",
                plan.target.key,
                plan.kind.value,
                plan.reason,
            )
        return plans
