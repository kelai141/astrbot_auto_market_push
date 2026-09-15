"""Change detection decisions.

The watcher is the piece that decides whether a submission happens, so it is
covered exhaustively with stub GitHub/Cloud clients.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrbot_auto_market_push.config import load_config_dict
from astrbot_auto_market_push.errors import GitHubError
from astrbot_auto_market_push.models import (
    ActionKind,
    MarketPlugin,
    PluginMetadata,
    SubmissionStatus,
    SubmitOutcome,
    WatchTarget,
)
from astrbot_auto_market_push.state import StateStore
from astrbot_auto_market_push.watcher import Watcher

REPO = "kelai141/astrbot_plugin_demo"


class StubGitHub:
    def __init__(
        self,
        version: str | None = "1.1.0",
        *,
        commit: str = "sha",
        error: Exception | None = None,
    ) -> None:
        self.version = version
        self.commit = commit
        self.error = error

    async def fetch_metadata(self, target: WatchTarget) -> tuple[PluginMetadata, str]:
        if self.error:
            raise self.error
        metadata = PluginMetadata(
            name="astrbot_plugin_demo",
            version=self.version or "",
            author="kelai141",
        )
        return metadata, self.commit


class StubCloud:
    def __init__(self, plugin: MarketPlugin | None = None, error: Exception | None = None) -> None:
        self.plugin = plugin
        self.error = error

    async def find_plugin_by_repo(self, url: str) -> MarketPlugin | None:
        if self.error:
            raise self.error
        return self.plugin


@pytest.fixture
def store(tmp_path):
    with StateStore(tmp_path / "state.db") as instance:
        yield instance


def _config(*, auto_submit: bool = True, require_version_change: bool = True):
    return load_config_dict(
        {
            "watch": {
                "repositories": [
                    {
                        "repo": REPO,
                        "auto_submit": auto_submit,
                        "require_version_change": require_version_change,
                    }
                ]
            }
        }
    )


def _target(config) -> WatchTarget:
    return config.watch.repositories[0]


async def _evaluate(config, store, github, cloud):
    watcher = Watcher(config=config, github=github, cloud=cloud, state=store)
    return await watcher.evaluate(_target(config))


def _market(version: str, *, status: str = "approved", submission_version: Any = None):
    return MarketPlugin.model_validate(
        {
            "plugin_id": REPO,
            "published_version": version,
            "latest_submission_status": status,
            "latest_submission_version": submission_version,
        }
    )


async def test_new_repo_is_submitted(store):
    plan = await _evaluate(_config(), store, StubGitHub("1.0.0"), StubCloud(None))
    assert plan.kind is ActionKind.SUBMIT
    assert plan.online_version == ""


async def test_newer_version_is_submitted(store):
    plan = await _evaluate(_config(), store, StubGitHub("1.1.0"), StubCloud(_market("1.0.0")))
    assert plan.kind is ActionKind.SUBMIT


async def test_same_version_is_skipped(store):
    plan = await _evaluate(_config(), store, StubGitHub("1.0.0"), StubCloud(_market("1.0.0")))
    assert plan.kind is ActionKind.SKIP
    assert "无需送审" in plan.reason


async def test_older_version_is_skipped(store):
    plan = await _evaluate(_config(), store, StubGitHub("0.9.0"), StubCloud(_market("1.0.0")))
    assert plan.kind is ActionKind.SKIP
    assert "不高于线上版本" in plan.reason


async def test_already_submitted_version_is_skipped(store):
    store.record_outcome(
        SubmitOutcome(
            repo_key=REPO,
            repo=f"https://github.com/{REPO}",
            version="1.1.0",
            submitted=True,
            status=SubmissionStatus.PENDING,
        )
    )
    plan = await _evaluate(_config(), store, StubGitHub("1.1.0"), StubCloud(None))
    assert plan.kind is ActionKind.SKIP
    assert "已经提交过" in plan.reason


async def test_pending_same_version_is_skipped(store):
    cloud = StubCloud(_market("1.0.0", status="pending", submission_version="1.1.0"))
    plan = await _evaluate(_config(), store, StubGitHub("1.1.0"), cloud)
    assert plan.kind is ActionKind.SKIP
    assert plan.pending is True
    assert "正在审核中" in plan.reason


async def test_pending_other_version_is_skipped(store):
    cloud = StubCloud(_market("1.0.0", status="pending", submission_version="1.0.5"))
    plan = await _evaluate(_config(), store, StubGitHub("1.1.0"), cloud)
    assert plan.kind is ActionKind.SKIP
    assert "存在待审核提交" in plan.reason


async def test_auto_submit_disabled_only_tracks(store):
    plan = await _evaluate(_config(auto_submit=False), store, StubGitHub("1.1.0"), StubCloud(None))
    assert plan.kind is ActionKind.SKIP
    assert "未开启自动送审" in plan.reason


async def test_github_failure_reports_error(store):
    plan = await _evaluate(_config(), store, StubGitHub(error=GitHubError("boom")), StubCloud(None))
    assert plan.kind is ActionKind.ERROR
    assert "读取仓库失败" in plan.reason


async def test_market_lookup_failure_falls_back_to_unshelved(store):
    cloud = StubCloud(error=RuntimeError("network down"))
    plan = await _evaluate(_config(), store, StubGitHub("1.1.0"), cloud)
    assert plan.kind is ActionKind.SUBMIT
    assert plan.online_version == ""


async def test_missing_version_is_skipped(store):
    plan = await _evaluate(_config(), store, StubGitHub(None), StubCloud(None))
    assert plan.kind is ActionKind.SKIP
    assert "缺少 version" in plan.reason


async def test_disabled_target_is_skipped(store):
    config = _config()
    config.watch.repositories[0].enabled = False
    plan = await _evaluate(config, store, StubGitHub("1.1.0"), StubCloud(None))
    assert plan.kind is ActionKind.SKIP
    assert "已被禁用" in plan.reason
