"""AstrBot Cloud client: session loading, payload building and error mapping."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from astrbot_auto_market_push.cloud_client import (
    CloudClient,
    build_submission_payload,
    load_storage_state,
)
from astrbot_auto_market_push.errors import (
    CloudAPIError,
    CloudRateLimitedError,
    GitHubAppError,
    SessionExpiredError,
)
from astrbot_auto_market_push.models import ParsedPlugin

BASE_URL = "https://cloud.astrbot.app/api/v1"

COOKIE_STATE = {
    "cookies": [
        {
            "name": "astrbot_session",
            "value": "s3cret",
            "domain": "cloud.astrbot.app",
            "path": "/",
        }
    ],
    "origins": [],
}


def test_load_storage_state_accepts_dict_and_bare_mapping():
    assert load_storage_state(COOKIE_STATE) is COOKIE_STATE
    bare = load_storage_state({"astrbot_session": "s3cret"})
    assert bare["cookies"][0]["name"] == "astrbot_session"


def test_load_storage_state_accepts_json_string():
    state = load_storage_state(json.dumps(COOKIE_STATE))
    assert state["cookies"][0]["value"] == "s3cret"


def test_load_storage_state_reads_file(tmp_path):
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps(COOKIE_STATE), encoding="utf-8")
    assert load_storage_state(path)["cookies"][0]["value"] == "s3cret"


def test_load_storage_state_rejects_missing_file(tmp_path):
    with pytest.raises(SessionExpiredError):
        load_storage_state(tmp_path / "nope.json")


def test_load_storage_state_rejects_empty():
    with pytest.raises(SessionExpiredError):
        load_storage_state("")


def test_build_submission_payload_strips_response_only_fields():
    parsed = ParsedPlugin.model_validate(
        {
            "name": "astrbot_plugin_demo",
            "author": "kelai141",
            "publisher_namespace": "kelai141",
            "version": "1.0.0",
            "authors": ["kelai141"],
            "tags": ["工具"],
            "publish_mode": "create",
            "existing_plugin": {"slug": "x"},
            "parse_token": "tok",
        }
    )
    payload = build_submission_payload(parsed, {"tags": ["自动化"]})
    assert "publish_mode" not in payload
    assert "existing_plugin" not in payload
    assert payload["tags"] == ["自动化"]
    assert payload["parse_token"] == "tok"
    assert payload["name"] == "astrbot_plugin_demo"


@respx.mock
async def test_get_options_returns_data():
    respx.get(f"{BASE_URL}/market/options").mock(
        return_value=Response(
            200, json={"code": 200, "msg": "ok", "data": {"publish_enabled": True}}
        )
    )
    async with CloudClient(storage_state=COOKIE_STATE) as client:
        assert await client.get_options() == {"publish_enabled": True}


@respx.mock
async def test_401_maps_to_session_expired():
    respx.get(f"{BASE_URL}/auth/me").mock(
        return_value=Response(401, json={"code": 401, "msg": "请先登录", "data": None})
    )
    async with CloudClient(storage_state=COOKIE_STATE) as client:
        with pytest.raises(SessionExpiredError):
            await client.get_me()


@respx.mock
async def test_rate_limit_maps_to_dedicated_error():
    respx.post(f"{BASE_URL}/market/plugins").mock(
        return_value=Response(429, json={"code": 429, "msg": "提交过于频繁", "data": None})
    )
    async with CloudClient(storage_state=COOKIE_STATE) as client:
        with pytest.raises(CloudRateLimitedError):
            await client.submit_plugin({"name": "x"})


@respx.mock
async def test_github_connection_required_is_detected():
    respx.post(f"{BASE_URL}/market/plugins/parse/github").mock(
        return_value=Response(
            400,
            json={
                "code": 400,
                "msg": "github_connection_required",
                "data": None,
            },
        )
    )
    async with CloudClient(storage_state=COOKIE_STATE) as client:
        with pytest.raises(GitHubAppError) as info:
            await client.parse_github(1, 2)
    assert info.value.reason == "connection_required"


@respx.mock
async def test_generic_error_maps_to_cloud_api_error():
    respx.post(f"{BASE_URL}/market/plugins").mock(
        return_value=Response(400, json={"code": 400, "msg": "字段缺失", "data": None})
    )
    async with CloudClient(storage_state=COOKIE_STATE) as client:
        with pytest.raises(CloudAPIError) as info:
            await client.submit_plugin({"name": "x"})
    assert info.value.code == 400
    assert "字段缺失" in str(info.value)


@respx.mock
async def test_resolve_repository_finds_installation_and_repo():
    respx.get(f"{BASE_URL}/auth/github/connection").mock(
        return_value=Response(200, json={"code": 200, "data": {"connected": True}})
    )
    respx.get(f"{BASE_URL}/market/github/namespaces").mock(
        return_value=Response(200, json={"code": 200, "data": [{"installation_id": 42}]})
    )
    respx.get(f"{BASE_URL}/market/github/installations/42/repositories").mock(
        return_value=Response(
            200,
            json={
                "code": 200,
                "data": [
                    {"id": 7, "full_name": "Kelai141/AstrBot_Plugin_Demo"},
                ],
            },
        )
    )
    async with CloudClient(storage_state=COOKIE_STATE) as client:
        installation_id, repository_id, repository = await client.resolve_repository(
            "kelai141/astrbot_plugin_demo"
        )
    assert (installation_id, repository_id) == (42, 7)
    assert repository["full_name"] == "Kelai141/AstrBot_Plugin_Demo"


@respx.mock
async def test_resolve_repository_raises_when_app_cannot_see_repo():
    respx.get(f"{BASE_URL}/auth/github/connection").mock(
        return_value=Response(200, json={"code": 200, "data": {"connected": True}})
    )
    respx.get(f"{BASE_URL}/market/github/namespaces").mock(
        return_value=Response(200, json={"code": 200, "data": [{"installation_id": 42}]})
    )
    respx.get(f"{BASE_URL}/market/github/installations/42/repositories").mock(
        return_value=Response(200, json={"code": 200, "data": []})
    )
    async with CloudClient(storage_state=COOKIE_STATE) as client:
        with pytest.raises(GitHubAppError) as info:
            await client.resolve_repository("kelai141/astrbot_plugin_demo")
    assert info.value.reason == "installation_required"
