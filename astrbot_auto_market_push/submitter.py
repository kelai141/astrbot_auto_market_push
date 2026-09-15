"""Execute an :class:`ActionPlan`: resolve, parse and send for review."""

from __future__ import annotations

import logging
from typing import Any

from .cloud_client import CloudClient, build_submission_payload
from .config import AppConfig
from .errors import (
    CloudAPIError,
    CloudRateLimitedError,
    GitHubAppError,
    SessionExpiredError,
)
from .models import (
    ActionKind,
    ActionPlan,
    SubmissionStatus,
    SubmitOutcome,
    utcnow,
)
from .notify import EVENT_FAILED, EVENT_RATE_LIMITED, EVENT_SUBMITTED, Notifier
from .state import StateStore

logger = logging.getLogger(__name__)


class Submitter:
    """Turns a submit plan into a marketplace review submission."""

    def __init__(
        self,
        *,
        config: AppConfig,
        cloud: CloudClient,
        state: StateStore,
        notifier: Notifier,
        options: dict[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        self.config = config
        self.cloud = cloud
        self.state = state
        self.notifier = notifier
        self.options = options or {}
        self.force = force

    # ------------------------------------------------------------------ guards

    def block_reason(self) -> str | None:
        """Return a human readable reason when submissions must be throttled."""
        submission = self.config.submission

        if submission.min_interval_seconds > 0:
            last = self.state.last_submission_at()
            if last is not None:
                elapsed = (utcnow() - last).total_seconds()
                if elapsed < submission.min_interval_seconds:
                    wait = int(submission.min_interval_seconds - elapsed)
                    return f"距离上次送审仅 {int(elapsed)} 秒，需再等待 {wait} 秒"

        sent = self.state.submissions_in_last_24h()

        if submission.max_submissions_per_24h > 0 and sent >= submission.max_submissions_per_24h:
            return (
                f"本地限流：24 小时内已送审 {sent} 次，"
                f"达到上限 {submission.max_submissions_per_24h} 次"
            )

        if submission.respect_cloud_limits:
            cloud_limit = self.options.get("max_review_submissions_per_account_24h")
            if isinstance(cloud_limit, int) and cloud_limit > 0 and sent >= cloud_limit:
                return f"云端限流：该账号 24 小时内最多送审 {cloud_limit} 次，已达上限"

        return None

    # ----------------------------------------------------------------- execute

    async def execute(self, plan: ActionPlan) -> SubmitOutcome:
        if plan.kind is not ActionKind.SUBMIT:
            raise ValueError("Submitter.execute 只能处理 SUBMIT 类型的计划。")

        target = plan.target
        repo = target.ref_name
        repo_key = repo.full_name
        repository_url = f"https://github.com/{repo_key}"
        dry_run = self.config.submission.dry_run

        if not self.force:
            blocked = self.block_reason()
            if blocked:
                logger.warning("[%s] 送审被限流拦截：%s", repo_key, blocked)
                outcome = SubmitOutcome(
                    repo_key=repo_key,
                    repo=repository_url,
                    version=plan.local_version,
                    commit=plan.local_commit,
                    submitted=False,
                    status=SubmissionStatus.UNKNOWN,
                    message=blocked,
                )
                return outcome

        outcome = SubmitOutcome(
            repo_key=repo_key,
            repo=repository_url,
            version=plan.local_version,
            commit=plan.local_commit,
            dry_run=dry_run,
            submitted=False,
            status=SubmissionStatus.UNKNOWN,
            message="",
        )

        try:
            installation_id, repository_id, repository = await self.cloud.resolve_repository(
                repo_key
            )
            parsed = await self.cloud.parse_github(installation_id, repository_id)
        except GitHubAppError as exc:
            outcome.message = f"GitHub App 未就绪：{exc}"
            logger.error("[%s] %s", repo_key, outcome.message)
            return outcome
        except CloudRateLimitedError as exc:
            outcome.message = f"云端限流：{exc}"
            logger.warning("[%s] %s", repo_key, outcome.message)
            await self.notifier.send(
                EVENT_RATE_LIMITED, f"{repo_key} 送审被云端限流", outcome.message
            )
            return outcome
        except SessionExpiredError:
            raise
        except CloudAPIError as exc:
            outcome.message = f"解析仓库失败：{exc}"
            logger.error("[%s] %s", repo_key, outcome.message)
            return outcome

        outcome.market_plugin_id = parsed.plugin_id or None
        parsed_version = parsed.version
        if parsed_version:
            outcome.version = parsed_version

        payload = build_submission_payload(parsed, target.overrides)
        outcome.payload = payload

        if dry_run:
            outcome.message = (
                f"[DRY-RUN] 将提交 {parsed.plugin_id} 版本 {outcome.version}"
                f"（commit {parsed.git_commit or 'unknown'}）"
            )
            logger.info("[%s] %s", repo_key, outcome.message)
            self.state.record_outcome(outcome)
            return outcome

        try:
            response = await self.cloud.submit_plugin(payload)
        except CloudRateLimitedError as exc:
            outcome.message = f"提交被云端限流：{exc}"
            logger.warning("[%s] %s", repo_key, outcome.message)
            await self.notifier.send(
                EVENT_RATE_LIMITED, f"{repo_key} 送审被云端限流", outcome.message
            )
            self.state.record_outcome(outcome)
            return outcome
        except SessionExpiredError:
            raise
        except CloudAPIError as exc:
            outcome.message = f"提交失败：{exc}"
            logger.error("[%s] %s", repo_key, outcome.message)
            self.state.record_outcome(outcome)
            await self.notifier.send(EVENT_FAILED, f"{repo_key} 送审失败", outcome.message)
            return outcome

        outcome.submitted = True
        outcome.status = _extract_status(response)
        outcome.submission_id = _extract_id(response)
        outcome.message = (
            f"已提交 {parsed.plugin_id} 版本 {outcome.version}，状态：{outcome.status.value}"
        )
        logger.info("[%s] %s", repo_key, outcome.message)

        self.state.record_outcome(outcome)
        await self.notifier.send(
            EVENT_SUBMITTED,
            f"{repo_key} 已提交审核",
            outcome.message,
            plugin_id=parsed.plugin_id,
            version=outcome.version,
            commit=parsed.git_commit,
            repository=repository.get("full_name") if repository else repo_key,
        )
        return outcome


def _extract_status(response: dict[str, Any] | None) -> SubmissionStatus:
    if not response:
        return SubmissionStatus.PENDING
    for key in ("latest_submission_status", "submission_status", "status", "review_status"):
        if response.get(key):
            return SubmissionStatus.parse(response[key])
    return SubmissionStatus.PENDING


def _extract_id(response: dict[str, Any] | None) -> str | int | None:
    if not response:
        return None
    for key in ("latest_submission_id", "submission_id", "id"):
        value = response.get(key)
        if isinstance(value, (str, int)):
            return value
    return None
