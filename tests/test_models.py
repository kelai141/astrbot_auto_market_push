"""Model level behaviour: version parsing, comparison and repository refs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from astrbot_auto_market_push.models import (
    MarketPlugin,
    PluginMetadata,
    RepoRef,
    SubmissionStatus,
    normalize_version,
    version_gt,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("v1.2.3", "1.2.3"),
        ("V1.2.3", "1.2.3"),
        (" 1.2.3 ", "1.2.3"),
        ("1.2.3", "1.2.3"),
        (None, ""),
        ("", ""),
        ("v", ""),
    ],
)
def test_normalize_version(raw, expected):
    assert normalize_version(raw) == expected


@pytest.mark.parametrize(
    ("candidate", "baseline", "expected"),
    [
        ("1.2.4", "1.2.3", True),
        ("1.3.0", "1.2.9", True),
        ("2.0.0", "1.9.9", True),
        ("1.2.3", "1.2.3", False),
        ("1.2.2", "1.2.3", False),
        ("v1.2.4", "1.2.3", True),
        ("1.2.3", None, True),
        (None, "1.2.3", False),
        # Prereleases must not shadow a real release of the same core version.
        ("1.2.3-rc.1", "1.2.3", False),
        ("1.2.3", "1.2.3-rc.1", True),
        # Non semver tags sort below any parsable version.
        ("nightly", "1.0.0", False),
        ("1.0.0", "nightly", True),
    ],
)
def test_version_gt(candidate, baseline, expected):
    assert version_gt(candidate, baseline) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("kelai141/astrbot_plugin_foo", "kelai141/astrbot_plugin_foo"),
        ("https://github.com/kelai141/astrbot_plugin_foo", "kelai141/astrbot_plugin_foo"),
        ("https://github.com/kelai141/astrbot_plugin_foo.git", "kelai141/astrbot_plugin_foo"),
        ("kelai141/astrbot_plugin_foo/", "kelai141/astrbot_plugin_foo"),
    ],
)
def test_repo_ref_parse(raw, expected):
    assert RepoRef.parse(raw).full_name == expected


@pytest.mark.parametrize("raw", ["", "kelai141", "not a repo"])
def test_repo_ref_parse_invalid(raw):
    with pytest.raises(ValueError):
        RepoRef.parse(raw)


def test_plugin_metadata_ignores_unknown_keys():
    metadata = PluginMetadata.model_validate(
        {
            "name": "astrbot_plugin_demo",
            "version": "v1.4.0",
            "author": "kelai141",
            "some_future_field": {"nested": True},
            "tags": ["工具"],
        }
    )
    assert metadata.normalized_version == "1.4.0"
    assert metadata.tags == ["工具"]


def test_plugin_metadata_requires_core_fields():
    with pytest.raises(ValidationError):
        PluginMetadata.model_validate({"name": "astrbot_plugin_demo"})


def test_market_plugin_version_preference_and_status():
    plugin = MarketPlugin.model_validate(
        {
            "plugin_id": "kelai141/astrbot_plugin_demo",
            "published_version": "1.2.0",
            "latest_version": "1.3.0",
            "latest_submission_version": "1.4.0",
            "latest_submission_status": "pending",
        }
    )
    assert plugin.current_online_version == "1.2.0"
    assert plugin.submission_status is SubmissionStatus.PENDING

    pending_only = MarketPlugin.model_validate(
        {
            "plugin_id": "a/b",
            "latest_submission_version": "v2.0.0",
            "latest_submission_status": "queued",
        }
    )
    assert pending_only.current_online_version == "2.0.0"
    assert pending_only.submission_status is SubmissionStatus.PENDING


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pending", SubmissionStatus.PENDING),
        ("in_review", SubmissionStatus.PENDING),
        ("approved", SubmissionStatus.APPROVED),
        ("published", SubmissionStatus.APPROVED),
        ("rejected", SubmissionStatus.REJECTED),
        ("something-new", SubmissionStatus.UNKNOWN),
        (None, SubmissionStatus.UNKNOWN),
    ],
)
def test_submission_status_parse(raw, expected):
    assert SubmissionStatus.parse(raw) is expected
