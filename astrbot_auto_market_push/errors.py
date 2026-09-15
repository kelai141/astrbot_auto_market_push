"""Exception hierarchy for :mod:`astrbot_auto_market_push`.

Every error raised on purpose by this project derives from :class:`AmpError`
so embedders (the CLI, the AstrBot plugin adapter, the GitHub Action) can
catch a single base class and still inspect structured details.
"""

from __future__ import annotations

from typing import Any


class AmpError(Exception):
    """Base class for all errors raised by this project."""


class ConfigError(AmpError):
    """Raised when configuration is missing, malformed or contradictory."""


class StateError(AmpError):
    """Raised when local persistent state cannot be read or written."""


class GitHubError(AmpError):
    """Raised when the GitHub REST API returns an unexpected response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SessionExpiredError(AmpError):
    """Raised when the stored AstrBot Cloud session is no longer accepted.

    The operator has to re-run the interactive ``login`` bootstrap to obtain a
    fresh ``storage_state.json``.
    """


class CloudAPIError(AmpError):
    """Raised when the AstrBot Cloud API returns an error envelope.

    AstrBot Cloud always answers with ``{"code": int, "msg": str, "data": ...}``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload

    def __str__(self) -> str:
        prefix = []
        if self.status_code:
            prefix.append(f"HTTP {self.status_code}")
        if self.code:
            prefix.append(f"code={self.code}")
        head = " ".join(prefix)
        body = super().__str__()
        return f"{head} {body}".strip() if head else body


class CloudRateLimitedError(CloudAPIError):
    """Raised when the account exceeded the marketplace review rate limit.

    AstrBot Cloud allows a limited number of concurrent reviews and rolling
    24h submissions per account; both limits are published by
    ``GET /market/options``.
    """


class GitHubAppError(AmpError):
    """Raised when the AstrBot Cloud GitHub App is not usable for a repository.

    ``reason`` is one of:

    ``connection_required``
        The AstrBot Cloud account never authorized the GitHub OAuth App.
    ``installation_required``
        The GitHub OAuth App is connected but the Cloud GitHub App was never
        installed on (or granted access to) the target repository.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
