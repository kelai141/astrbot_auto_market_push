"""Configuration loading, env expansion and target normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_auto_market_push.config import (
    build_targets_from_entries,
    expand_env,
    load_config_dict,
)
from astrbot_auto_market_push.errors import ConfigError


def test_expand_env_supports_defaults(monkeypatch):
    monkeypatch.setenv("AMP_TEST_PRESENT", "here")
    monkeypatch.delenv("AMP_TEST_MISSING", raising=False)

    raw = {
        "a": "${AMP_TEST_PRESENT}",
        "b": "${AMP_TEST_MISSING}",
        "c": "${AMP_TEST_MISSING:-fallback}",
        "d": ["${AMP_TEST_PRESENT}", {"nested": "${AMP_TEST_MISSING:-x}"}],
    }
    assert expand_env(raw) == {
        "a": "here",
        "b": "",
        "c": "fallback",
        "d": ["here", {"nested": "x"}],
    }


def test_build_targets_from_entries_supports_short_form_and_overrides():
    targets = build_targets_from_entries(
        ["kelai141/astrbot_plugin_a", "kelai141/astrbot_plugin_b#dev", "# comment", ""],
        {"kelai141/astrbot_plugin_a": {"tags": ["工具"]}},
    )
    assert [t.ref_name.full_name for t in targets] == [
        "kelai141/astrbot_plugin_a",
        "kelai141/astrbot_plugin_b",
    ]
    assert targets[0].overrides == {"tags": ["工具"]}
    assert targets[1].ref == "dev"


def test_build_targets_from_entries_rejects_garbage():
    with pytest.raises(ConfigError):
        build_targets_from_entries(["not-a-repo"])


def test_load_config_dict_applies_defaults(tmp_path):
    config = load_config_dict({"watch": {"repositories": [{"repo": "a/b"}]}}, base=tmp_path)

    assert config.state.path == tmp_path / ".state" / "state.sqlite3"
    assert config.cloud.storage_state == tmp_path / ".state" / "storage_state.json"
    assert config.cloud.base_url == "https://cloud.astrbot.app/api/v1"
    assert [t.repo for t in config.enabled_targets] == ["a/b"]


def test_load_config_dict_rejects_too_short_interval():
    with pytest.raises(ConfigError):
        load_config_dict({"watch": {"interval_seconds": 5}})


def test_load_config_dict_resolves_relative_paths(tmp_path):
    config = load_config_dict({"state": {"path": "custom/state.db"}}, base=tmp_path)
    assert config.state.path == tmp_path / "custom" / "state.db"
    assert isinstance(config.state.path, Path)


def test_string_entries_are_accepted_in_yaml_form(tmp_path):
    config = load_config_dict(
        {
            "watch": {
                "repositories": ["a/b", "c/d#dev"],
                "overrides_yaml": "a/b:\n  tags: [工具]\n",
            }
        },
        base=tmp_path,
    )
    assert [t.repo for t in config.watch.repositories] == ["a/b", "c/d"]
    assert config.watch.repositories[1].ref == "dev"
    assert config.watch.repositories[0].overrides == {"tags": ["工具"]}


def test_mapping_entries_merge_shared_overrides(tmp_path):
    config = load_config_dict(
        {
            "watch": {
                "repositories": [{"repo": "a/b", "ref": "main", "overrides": {"category": "工具"}}],
                "overrides_yaml": "a/b:\n  tags: [自动化]\n",
            }
        },
        base=tmp_path,
    )
    target = config.watch.repositories[0]
    assert target.ref == "main"
    assert target.overrides == {"tags": ["自动化"], "category": "工具"}


def test_bad_overrides_yaml_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config_dict(
            {"watch": {"repositories": ["a/b"], "overrides_yaml": "[not, a, mapping]"}},
            base=tmp_path,
        )


def test_repositories_can_come_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AMP_REPOSITORIES", "kelai141/a, kelai141/b#dev")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    monkeypatch.setenv("AMP_CLOUD_STORAGE_STATE", '{"cookies":[]}')
    monkeypatch.chdir(tmp_path)

    from astrbot_auto_market_push.config import load_config

    config = load_config()
    assert [t.repo for t in config.watch.repositories] == ["kelai141/a", "kelai141/b"]
    assert config.watch.repositories[1].ref == "dev"
    assert config.github.token == "ghp_env"
    assert config.cloud.storage_state_json == '{"cookies":[]}'


def test_implicit_missing_config_file_degrades_to_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AMP_REPOSITORIES", raising=False)
    monkeypatch.setenv("AMP_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    monkeypatch.chdir(tmp_path)

    from astrbot_auto_market_push.config import load_config

    config = load_config()
    assert config.watch.repositories == []
