"""GitHub client: metadata parsing and error diagnostics."""

from __future__ import annotations

import base64

import pytest
import respx
from httpx import Response

from astrbot_auto_market_push.errors import GitHubError
from astrbot_auto_market_push.github_client import GitHubClient
from astrbot_auto_market_push.models import WatchTarget

API = "https://api.github.com"
METADATA_YAML = """
name: astrbot_plugin_demo
display_name: 演示插件
version: v1.4.0
author: kelai141
desc: 一个演示插件
tags:
  - 工具
"""


def _encoded_metadata() -> str:
    return base64.b64encode(METADATA_YAML.encode("utf-8")).decode("ascii")


@respx.mock
async def test_fetch_metadata_parses_version_and_commit():
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/commits/main").mock(
        return_value=Response(200, json={"sha": "deadbeef"})
    )
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/contents/metadata.yaml").mock(
        return_value=Response(
            200,
            json={"encoding": "base64", "content": _encoded_metadata()},
        )
    )

    async with GitHubClient(token=None) as client:
        metadata, commit = await client.fetch_metadata(
            WatchTarget(repo="kelai141/astrbot_plugin_demo", ref="main")
        )

    assert metadata.name == "astrbot_plugin_demo"
    assert metadata.normalized_version == "1.4.0"
    assert metadata.tags == ["工具"]
    assert commit == "deadbeef"


@respx.mock
async def test_invalid_token_gets_actionable_error():
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/commits/main").mock(
        return_value=Response(
            401,
            json={"message": "Bad credentials", "status": "401"},
        )
    )
    async with GitHubClient(token="ghp_stale") as client:
        with pytest.raises(GitHubError) as info:
            await client.fetch_metadata(
                WatchTarget(repo="kelai141/astrbot_plugin_demo", ref="main")
            )
    assert info.value.status_code == 401
    assert "无效或已过期" in str(info.value)


@respx.mock
async def test_missing_metadata_yaml_is_reported():
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/commits/main").mock(
        return_value=Response(200, json={"sha": "deadbeef"})
    )
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/contents/metadata.yaml").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )

    async with GitHubClient() as client:
        with pytest.raises(GitHubError) as info:
            await client.fetch_metadata(
                WatchTarget(repo="kelai141/astrbot_plugin_demo", ref="main")
            )
    assert "metadata.yaml 不存在" in str(info.value)


@respx.mock
async def test_exhausted_rate_limit_is_explained():
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/commits/main").mock(
        return_value=Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
        )
    )
    async with GitHubClient() as client:
        with pytest.raises(GitHubError) as info:
            await client.fetch_metadata(
                WatchTarget(repo="kelai141/astrbot_plugin_demo", ref="main")
            )
    assert "速率限制已用尽" in str(info.value)


@respx.mock
async def test_metadata_missing_required_fields_is_reported():
    bad_yaml = base64.b64encode(b"name: astrbot_plugin_demo\nversion: 1.0.0\n").decode()
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/commits/main").mock(
        return_value=Response(200, json={"sha": "abc"})
    )
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/contents/metadata.yaml").mock(
        return_value=Response(200, json={"encoding": "base64", "content": bad_yaml})
    )

    async with GitHubClient() as client:
        with pytest.raises(GitHubError) as info:
            await client.fetch_metadata(
                WatchTarget(repo="kelai141/astrbot_plugin_demo", ref="main")
            )
    assert "缺少必填字段" in str(info.value)


@respx.mock
async def test_default_branch_is_resolved_when_ref_missing():
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo").mock(
        return_value=Response(200, json={"default_branch": "master"})
    )
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/commits/master").mock(
        return_value=Response(200, json={"sha": "cafe"})
    )
    respx.get(f"{API}/repos/kelai141/astrbot_plugin_demo/contents/metadata.yaml").mock(
        return_value=Response(200, json={"encoding": "base64", "content": _encoded_metadata()})
    )

    async with GitHubClient() as client:
        metadata, commit = await client.fetch_metadata(
            WatchTarget(repo="kelai141/astrbot_plugin_demo")
        )
    assert metadata.normalized_version == "1.4.0"
    assert commit == "cafe"
