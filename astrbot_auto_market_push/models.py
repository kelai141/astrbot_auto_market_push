"""Domain models shared by the CLI and the AstrBot plugin adapter."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.\-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.\-]+))?$"
)


def normalize_version(raw: str | None) -> str:
    """Strip a leading ``v``/``V`` and surrounding whitespace.

    Mirrors the normalization AstrBot Cloud applies before comparing versions.
    """
    if raw is None:
        return ""
    value = str(raw).strip()
    if value[:1] in {"v", "V"}:
        value = value[1:].strip()
    return value


def parse_semver(raw: str | None) -> tuple[int, int, int, str | None] | None:
    """Return ``(major, minor, patch, prerelease)`` or ``None``."""
    match = SEMVER_RE.match(normalize_version(raw))
    if not match:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        match.group("prerelease"),
    )


def is_semver(raw: str | None) -> bool:
    return parse_semver(raw) is not None


def _prerelease_key(prerelease: str | None) -> tuple[tuple[int, Any], ...]:
    """Ordering key matching SemVer precedence rules.

    A release outranks every prerelease of the same core version, and numeric
    identifiers rank below alphanumeric ones. Each identifier is normalized to
    a ``(rank, value)`` pair so comparisons never mix ``str`` and ``int``.
    """
    if not prerelease:
        # Sentinel that outranks any prerelease identifier.
        return ((2, 0),)
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split("."))


def version_sort_key(raw: str | None) -> tuple[int, int, int, int, Any]:
    """Sortable key that never raises for non-semver input.

    Non parsable versions sort below any parsable one, so ad-hoc tags such as
    ``nightly`` never shadow a real release.
    """
    parsed = parse_semver(raw)
    if parsed is None:
        return (0, 0, 0, 0, _prerelease_key(None))
    major, minor, patch, prerelease = parsed
    return (1, major, minor, patch, _prerelease_key(prerelease))


def version_gt(candidate: str | None, baseline: str | None) -> bool:
    """True when ``candidate`` is strictly newer than ``baseline``."""
    if not candidate:
        return False
    if not baseline:
        return True
    return version_sort_key(candidate) > version_sort_key(baseline)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SubmissionStatus(str, Enum):
    """Lifecycle of a marketplace review submission."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: Any) -> SubmissionStatus:
        if isinstance(raw, SubmissionStatus):
            return raw
        if raw is None:
            return cls.UNKNOWN
        text = str(raw).strip().lower()
        for member in cls:
            if member.value == text:
                return member
        if text in {"queued", "in_review", "reviewing", "waiting"}:
            return cls.PENDING
        if text in {"pass", "passed", "accepted", "published", "ok"}:
            return cls.APPROVED
        if text in {"fail", "failed", "denied", "declined"}:
            return cls.REJECTED
        return cls.UNKNOWN


class PluginMetadata(BaseModel):
    """The subset of ``metadata.yaml`` this project cares about.

    Unknown keys are ignored on purpose: the plugin specification evolves and
    maintainers may add fields this tool does not understand yet.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    version: str
    author: str
    display_name: str | None = None
    short_desc: str | None = None
    desc: str | None = None
    repo: str | None = None
    astrbot_version: str | None = None
    support_platforms: list[str] = Field(default_factory=list)
    social_link: str | None = None
    tags: list[str] = Field(default_factory=list)

    @property
    def normalized_version(self) -> str:
        return normalize_version(self.version)


class RepoRef(BaseModel):
    """A parsed ``owner/name`` GitHub repository reference."""

    model_config = ConfigDict(frozen=True)

    owner: str
    name: str

    @classmethod
    def parse(cls, raw: str) -> RepoRef:
        text = (raw or "").strip()
        text = text.removeprefix("https://github.com/").removeprefix("http://github.com/")
        text = text.removesuffix(".git").strip("/")
        parts = [part for part in text.split("/") if part]
        if len(parts) < 2:
            raise ValueError(f"仓库引用 {raw!r} 无效，应为 'owner/name' 或 GitHub 仓库 URL。")
        return cls(owner=parts[0], name=parts[1])

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class ParsedPlugin(BaseModel):
    """Response payload of ``POST /market/plugins/parse/github``.

    Field names match the marketplace wire format exactly; do not rename
    without updating the submission payload builder.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    publisher_namespace: str | None = None
    github_installation_id: int | None = None
    github_repository_id: int | None = None
    author: str
    display_name: str | None = None
    description: str | None = None
    authors: list[str] = Field(default_factory=list)
    author_name: str | None = None
    repository_url: str | None = None
    git_commit: str | None = None
    download_url: str | None = None
    logo_url: str | None = None
    version: str = ""
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    categories: list[str] = Field(default_factory=list)
    astrbot_version: str | None = None
    support_platforms: list[str] = Field(default_factory=list)
    source_type: str = "github"
    audit_payload: dict[str, Any] = Field(default_factory=dict)
    parse_token: str | None = None
    publish_mode: str | None = None
    existing_plugin: dict[str, Any] | None = None

    @property
    def plugin_id(self) -> str:
        return f"{self.publisher_namespace or ''}/{self.name}".strip("/")


class MarketPlugin(BaseModel):
    """A plugin record as returned by ``GET /market/plugins``."""

    model_config = ConfigDict(extra="allow")

    plugin_id: str
    name: str | None = None
    publisher_namespace: str | None = None
    slug: str | None = None
    repository_url: str | None = None
    version: str | None = None
    published_version: str | None = None
    latest_version: str | None = None
    latest_submission_version: str | None = None
    latest_submission_status: str | None = None
    is_published: bool | None = None

    @property
    def current_online_version(self) -> str:
        """Newest version known to the marketplace, in preference order."""
        for candidate in (
            self.published_version,
            self.latest_version,
            self.latest_submission_version,
            self.version,
        ):
            normalized = normalize_version(candidate)
            if normalized:
                return normalized
        return ""

    @property
    def submission_status(self) -> SubmissionStatus:
        return SubmissionStatus.parse(self.latest_submission_status)


class WatchTarget(BaseModel):
    """One repository to track."""

    model_config = ConfigDict(extra="ignore")

    repo: str
    ref: str | None = None
    metadata_path: str = "metadata.yaml"
    enabled: bool = True
    auto_submit: bool = True
    require_version_change: bool = True
    overrides: dict[str, Any] = Field(default_factory=dict)

    @property
    def ref_name(self) -> RepoRef:
        return RepoRef.parse(self.repo)

    @property
    def key(self) -> str:
        suffix = f"#{self.ref}" if self.ref else ""
        return f"{self.ref_name.full_name}{suffix}"


class ActionKind(str, Enum):
    SUBMIT = "submit"
    SKIP = "skip"
    ERROR = "error"


class ActionPlan(BaseModel):
    """The decision taken for one watch target during a poll cycle."""

    target: WatchTarget
    kind: ActionKind
    reason: str
    local_version: str = ""
    local_commit: str | None = None
    online_version: str = ""
    pending: bool = False


class SubmitOutcome(BaseModel):
    """Result of a real (or simulated) submission attempt.

    ``repo_key`` is the tracker identity (``owner/name``) and doubles as the
    persistence key, while ``market_plugin_id`` is the marketplace identity
    (``publisher_namespace/name``) once the server has parsed the repository.
    """

    repo_key: str
    repo: str
    market_plugin_id: str | None = None
    version: str
    commit: str | None = None
    dry_run: bool = False
    submitted: bool = False
    submission_id: str | int | None = None
    status: SubmissionStatus = SubmissionStatus.UNKNOWN
    message: str = ""
    payload: dict[str, Any] | None = None
    submitted_at: datetime = Field(default_factory=utcnow)
