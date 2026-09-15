"""Configuration loading for both the CLI and the AstrBot plugin adapter.

The CLI reads a YAML file; the AstrBot plugin receives a nested ``dict`` from
``_conf_schema.json``. Both funnel into the same :class:`AppConfig` model so
the engine never has to care where it is running.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigError
from .models import RepoRef, WatchTarget

logger = logging.getLogger(__name__)

DEFAULT_CLOUD_BASE_URL = "https://cloud.astrbot.app/api/v1"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_STATE_DIR = ".state"

_ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-?(?P<default>[^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-fallback}`` placeholders.

    Undefined variables without a fallback expand to an empty string, which
    downstream validators treat as "unset".
    """
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            name = match.group("name")
            default = match.group("default")
            if name in os.environ:
                return os.environ[name]
            return default or ""

        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


class CloudConfig(BaseModel):
    """AstrBot Cloud / plugin marketplace connection settings."""

    model_config = ConfigDict(extra="ignore")

    base_url: str = DEFAULT_CLOUD_BASE_URL
    storage_state: Path | None = None
    storage_state_json: str | None = None
    timeout_seconds: float = 30.0
    user_agent: str = "astrbot-auto-market-push"

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")


class GitHubConfig(BaseModel):
    """GitHub REST API settings used for *reading* tracked repositories."""

    model_config = ConfigDict(extra="ignore")

    token: str | None = None
    api_url: str = DEFAULT_GITHUB_API_URL
    timeout_seconds: float = 30.0
    user_agent: str = "astrbot-auto-market-push"

    @field_validator("api_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")


class WatchConfig(BaseModel):
    """Polling behaviour and the list of tracked repositories."""

    model_config = ConfigDict(extra="ignore")

    interval_seconds: int = 300
    jitter_seconds: int = 15
    max_concurrency: int = 4
    repositories: list[WatchTarget] = Field(default_factory=list)

    @field_validator("interval_seconds")
    @classmethod
    def _min_interval(cls, value: int) -> int:
        if value < 30:
            raise ValueError("watch.interval_seconds 不能小于 30 秒，避免触发 GitHub 限流。")
        return value


class SubmissionConfig(BaseModel):
    """Guard rails around the actual send-for-review call."""

    model_config = ConfigDict(extra="ignore")

    dry_run: bool = False
    skip_if_pending: bool = True
    max_submissions_per_24h: int = 3
    min_interval_seconds: int = 600
    respect_cloud_limits: bool = True

    @field_validator("max_submissions_per_24h")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("submission.max_submissions_per_24h 不能为负数。")
        return value


class StateConfig(BaseModel):
    """Where run history and dedup state live."""

    model_config = ConfigDict(extra="ignore")

    path: Path = Path(DEFAULT_STATE_DIR) / "state.sqlite3"


class NotifyConfig(BaseModel):
    """Optional webhook notification settings."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    webhook_url: str | None = None
    events: list[str] = Field(default_factory=lambda: ["submitted", "failed", "session_expired"])


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    level: str = "INFO"
    file: Path | None = None


class AppConfig(BaseModel):
    """Root configuration object."""

    model_config = ConfigDict(extra="ignore")

    cloud: CloudConfig = Field(default_factory=CloudConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def base_dir(self) -> Path:
        """Directory used to resolve relative paths inside the config."""
        path = self.state.path
        return path.parent if path.parent != Path("") else Path(".")

    def resolve_paths(self, base: Path | None = None) -> AppConfig:
        """Anchor relative state/session paths to ``base`` (defaults to cwd)."""
        root = (base or Path.cwd()).resolve()
        if not self.state.path.is_absolute():
            self.state.path = root / self.state.path
        if self.cloud.storage_state and not self.cloud.storage_state.is_absolute():
            self.cloud.storage_state = root / self.cloud.storage_state
        if self.logging.file and not self.logging.file.is_absolute():
            self.logging.file = root / self.logging.file
        return self

    @property
    def enabled_targets(self) -> list[WatchTarget]:
        return [target for target in self.watch.repositories if target.enabled]


def load_config_dict(raw: dict[str, Any] | None, *, base: Path | None = None) -> AppConfig:
    """Build an :class:`AppConfig` from an already parsed mapping."""
    data = expand_env(raw or {})
    _normalize_watch_section(data)
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"配置校验失败：\n{exc}") from exc

    if config.state.path == Path(DEFAULT_STATE_DIR) / "state.sqlite3":
        root = (base or Path.cwd()).resolve()
        config.state.path = root / DEFAULT_STATE_DIR / "state.sqlite3"

    if config.cloud.storage_state is None and not config.cloud.storage_state_json:
        root = (base or Path.cwd()).resolve()
        config.cloud.storage_state = root / DEFAULT_STATE_DIR / "storage_state.json"

    return config.resolve_paths(base)


def load_config(path: str | Path | None = None, *, base: Path | None = None) -> AppConfig:
    """Load configuration from YAML, falling back to environment only.

    A missing file passed explicitly by the caller is an error, while a missing
    ``AMP_CONFIG`` (or the implicit ``./config.yaml``) only logs a warning and
    degrades to environment-only configuration. That keeps container and
    GitHub Action first-run smooth.
    """
    explicit = path is not None
    if path is None:
        env_path = os.environ.get("AMP_CONFIG")
        if env_path:
            path = env_path
        else:
            candidate = Path.cwd() / "config.yaml"
            path = candidate if candidate.exists() else None

    raw: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            if explicit:
                raise ConfigError(f"配置文件不存在：{config_path}")
            logger.warning(
                "配置文件 %s 不存在，改为仅使用环境变量（AMP_REPOSITORIES 等）配置。",
                config_path,
            )
        else:
            try:
                loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ConfigError(f"配置文件不是合法 YAML：{config_path}\n{exc}") from exc
            if loaded is not None and not isinstance(loaded, dict):
                raise ConfigError(f"配置文件顶层必须是映射（dict）：{config_path}")
            raw = loaded or {}
            base = base or config_path.resolve().parent

    # Environment fallbacks keep GitHub Action / Docker usage concise.
    github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("AMP_GITHUB_TOKEN")
    if github_token and not raw.get("github", {}).get("token"):
        raw.setdefault("github", {})["token"] = github_token

    state_json = os.environ.get("AMP_CLOUD_STORAGE_STATE")
    if state_json and not raw.get("cloud", {}).get("storage_state_json"):
        raw.setdefault("cloud", {})["storage_state_json"] = state_json

    # `AMP_REPOSITORIES="owner/a,owner/b#dev"` keeps Docker / Action usage
    # config-file free.
    repos_env = os.environ.get("AMP_REPOSITORIES")
    if repos_env and not raw.get("watch", {}).get("repositories"):
        entries = [
            line.strip() for line in repos_env.replace(",", "\n").splitlines() if line.strip()
        ]
        raw.setdefault("watch", {})["repositories"] = entries

    return load_config_dict(raw, base=base)


def merge_overrides(parsed: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply per-repository overrides onto a parsed plugin record.

    Lists are replaced wholesale (copy semantics) rather than merged, which is
    what maintainers expect when they pin ``tags`` or ``support_platforms``.
    """
    result = dict(parsed)
    for key, value in overrides.items():
        if value is None:
            continue
        result[key] = value
    return result


def build_targets_from_entries(
    entries: list[Any] | None,
    overrides_map: dict[str, dict[str, Any]] | None = None,
) -> list[WatchTarget]:
    """Build watch targets from the compact list used by ``_conf_schema.json``.

    Each entry is either a full mapping (``{"repo": ..., "ref": ...}``) or the
    short string form ``owner/name`` / ``owner/name#branch``. Per-repository
    field overrides live in a separate mapping keyed by ``owner/name``; for
    mapping entries an inline ``overrides`` key wins over that mapping.
    """
    targets: list[WatchTarget] = []
    overrides_map = overrides_map or {}

    for entry in entries or []:
        if isinstance(entry, dict):
            raw_repo = str(entry.get("repo") or "").strip()
            if not raw_repo:
                continue
            try:
                repo_key = RepoRef.parse(raw_repo).full_name
            except ValueError as exc:
                raise ConfigError(f"监控目标 {raw_repo!r} 无效：{exc}") from exc

            ref = entry.get("ref")
            if isinstance(ref, str):
                ref = ref.strip() or None
            merged_overrides = {
                **overrides_map.get(repo_key, {}),
                **(entry.get("overrides") or {}),
            }
            targets.append(
                WatchTarget.model_validate(
                    {**entry, "repo": repo_key, "ref": ref, "overrides": merged_overrides}
                )
            )
            continue

        text = str(entry).strip()
        if not text or text.startswith("#"):
            continue

        ref: str | None = None
        if "#" in text:
            text, ref = text.split("#", 1)
            text = text.strip()
            ref = ref.strip() or None
        if not text:
            continue

        try:
            repo_key = RepoRef.parse(text).full_name
        except ValueError as exc:
            raise ConfigError(f"监控目标 {entry!r} 无效：{exc}") from exc

        overrides = overrides_map.get(repo_key) or overrides_map.get(text) or {}
        targets.append(WatchTarget(repo=repo_key, ref=ref, overrides=dict(overrides)))

    return targets


def parse_overrides_yaml(text: str | None) -> dict[str, dict[str, Any]]:
    """Parse the ``overrides_yaml`` block used by the AstrBot plugin config."""
    if not text or not str(text).strip():
        return {}
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"overrides_yaml 不是合法 YAML：{exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError("overrides_yaml 顶层必须是映射（repo -> 覆盖字段）。")
    return {str(key): dict(value or {}) for key, value in parsed.items()}


def _normalize_watch_section(data: dict[str, Any]) -> None:
    """Accept the compact ``repositories`` list in both file and plugin config."""
    watch = data.get("watch")
    if not isinstance(watch, dict):
        return

    overrides_map = parse_overrides_yaml(watch.pop("overrides_yaml", None))
    entries = watch.get("repositories")
    if entries is None:
        return
    watch["repositories"] = [
        target.model_dump() for target in build_targets_from_entries(entries, overrides_map)
    ]
