"""astrbot_auto_market_push - 自动跟踪 GitHub 仓库并送审 AstrBot 插件市场.

The package is deliberately split into a headless engine plus two thin
adapters: a Typer based CLI (:mod:`astrbot_auto_market_push.cli`) and an
AstrBot Star plugin (``main.py`` at the repository root).
"""

from __future__ import annotations

from .config import AppConfig, load_config, load_config_dict
from .engine import CycleReport, Engine
from .errors import (
    AmpError,
    CloudAPIError,
    CloudRateLimitedError,
    ConfigError,
    GitHubAppError,
    GitHubError,
    SessionExpiredError,
    StateError,
)
from .models import (
    ActionKind,
    ActionPlan,
    MarketPlugin,
    PluginMetadata,
    RepoRef,
    SubmissionStatus,
    SubmitOutcome,
    WatchTarget,
    normalize_version,
)

__version__ = "0.1.0"

__all__ = [
    "ActionKind",
    "ActionPlan",
    "AmpError",
    "AppConfig",
    "CloudAPIError",
    "CloudRateLimitedError",
    "ConfigError",
    "CycleReport",
    "Engine",
    "GitHubAppError",
    "GitHubError",
    "MarketPlugin",
    "PluginMetadata",
    "RepoRef",
    "SessionExpiredError",
    "StateError",
    "SubmissionStatus",
    "SubmitOutcome",
    "WatchTarget",
    "__version__",
    "load_config",
    "load_config_dict",
    "normalize_version",
]
