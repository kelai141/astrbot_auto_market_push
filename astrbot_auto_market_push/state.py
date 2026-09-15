"""SQLite backed dedup state and submission history.

The watcher must not re-submit the same version twice, and the submission guard
rails need a rolling 24h window. Both are cheap to persist in a small SQLite
database next to the session file.

The primary key everywhere is the **repository key** (``owner/name``), because
it is stable even when a maintainer renames the plugin inside ``metadata.yaml``.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .errors import StateError
from .models import SubmissionStatus, SubmitOutcome, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_state (
    repo_key              TEXT PRIMARY KEY,
    repo                  TEXT NOT NULL,
    ref                   TEXT,
    market_plugin_id      TEXT,
    last_seen_version     TEXT,
    last_seen_commit      TEXT,
    last_seen_at          TEXT,
    last_submitted_version TEXT,
    last_submitted_commit  TEXT,
    last_submitted_at     TEXT,
    last_status           TEXT,
    last_message          TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS submission_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key         TEXT NOT NULL,
    repo             TEXT NOT NULL,
    market_plugin_id TEXT,
    version          TEXT NOT NULL,
    commit_sha       TEXT,
    submitted_at     TEXT NOT NULL,
    status           TEXT NOT NULL,
    dry_run          INTEGER NOT NULL DEFAULT 0,
    message          TEXT,
    response         TEXT
);

CREATE INDEX IF NOT EXISTS idx_submission_log_time
    ON submission_log (submitted_at);
CREATE INDEX IF NOT EXISTS idx_submission_log_repo
    ON submission_log (repo_key, submitted_at);
"""


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class StateStore:
    """Tiny synchronous SQLite wrapper.

    The engine runs on a single asyncio task and every query is a point lookup
    against a small local file, so synchronous access is intentional: it keeps
    the code readable and free of extra dependencies.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError(f"无法创建状态目录 {self.path.parent}：{exc}") from exc
        try:
            self._conn = sqlite3.connect(str(self.path), timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StateError(f"无法初始化状态数据库 {self.path}：{exc}") from exc

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ read

    def get_repo_state(self, repo_key: str, *, repo: str | None = None) -> dict[str, Any] | None:
        """Look up state by repository key, falling back to the repo URL."""
        row = self._conn.execute(
            "SELECT * FROM repo_state WHERE repo_key = ?", (repo_key,)
        ).fetchone()
        if row is None and repo:
            row = self._conn.execute("SELECT * FROM repo_state WHERE repo = ?", (repo,)).fetchone()
        return dict(row) if row else None

    def submissions_since(self, since: datetime) -> int:
        cursor = self._conn.execute(
            "SELECT COUNT(*) AS n FROM submission_log WHERE dry_run = 0 AND submitted_at >= ?",
            (_to_iso(since),),
        )
        return int(cursor.fetchone()["n"])

    def submissions_in_last_24h(self) -> int:
        return self.submissions_since(utcnow() - timedelta(hours=24))

    def last_submission_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT submitted_at FROM submission_log "
            "WHERE dry_run = 0 ORDER BY submitted_at DESC LIMIT 1"
        ).fetchone()
        return _from_iso(row["submitted_at"]) if row else None

    def was_submitted(self, repo_key: str, version: str) -> bool:
        """True when this exact version was already sent for review."""
        if not version:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM submission_log WHERE repo_key = ? AND version = ? "
            "AND dry_run = 0 LIMIT 1",
            (repo_key, version),
        ).fetchone()
        return row is not None

    def history(self, *, limit: int = 20, repo_key: str | None = None) -> list[dict[str, Any]]:
        if repo_key:
            rows = self._conn.execute(
                "SELECT * FROM submission_log WHERE repo_key = ? "
                "ORDER BY submitted_at DESC LIMIT ?",
                (repo_key, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM submission_log ORDER BY submitted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_repo_states(self) -> Iterator[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM repo_state ORDER BY repo").fetchall()
        return iter([dict(row) for row in rows])

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM submission_log WHERE dry_run = 0 GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    # ----------------------------------------------------------------- write

    def touch_repo(
        self,
        *,
        repo_key: str,
        repo: str,
        ref: str | None,
        version: str,
        commit: str | None,
        market_plugin_id: str | None = None,
    ) -> None:
        """Record that a repository was polled, without changing submit state."""
        self._conn.execute(
            """
            INSERT INTO repo_state (repo_key, repo, ref, market_plugin_id,
                                    last_seen_version, last_seen_commit, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_key) DO UPDATE SET
                repo = excluded.repo,
                ref = excluded.ref,
                market_plugin_id = COALESCE(excluded.market_plugin_id,
                                            repo_state.market_plugin_id),
                last_seen_version = excluded.last_seen_version,
                last_seen_commit = excluded.last_seen_commit,
                last_seen_at = excluded.last_seen_at
            """,
            (
                repo_key,
                repo,
                ref,
                market_plugin_id,
                version,
                commit,
                _to_iso(utcnow()),
            ),
        )
        self._conn.commit()

    def record_outcome(self, outcome: SubmitOutcome) -> None:
        """Persist a submission attempt and roll the repo state forward."""
        submitted_at = _to_iso(outcome.submitted_at)
        response = (
            json.dumps(outcome.payload, ensure_ascii=False, default=str)
            if outcome.payload is not None
            else None
        )
        self._conn.execute(
            """
            INSERT INTO submission_log (repo_key, repo, market_plugin_id, version,
                                        commit_sha, submitted_at, status, dry_run,
                                        message, response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.repo_key,
                outcome.repo,
                outcome.market_plugin_id,
                outcome.version,
                outcome.commit,
                submitted_at,
                outcome.status.value,
                1 if outcome.dry_run else 0,
                outcome.message,
                response,
            ),
        )

        if outcome.dry_run:
            self._conn.commit()
            return

        if outcome.submitted:
            self._conn.execute(
                """
                INSERT INTO repo_state (repo_key, repo, market_plugin_id,
                                        last_submitted_version, last_submitted_commit,
                                        last_submitted_at, last_status, last_message,
                                        consecutive_failures)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(repo_key) DO UPDATE SET
                    repo = excluded.repo,
                    market_plugin_id = COALESCE(excluded.market_plugin_id,
                                                repo_state.market_plugin_id),
                    last_submitted_version = excluded.last_submitted_version,
                    last_submitted_commit = excluded.last_submitted_commit,
                    last_submitted_at = excluded.last_submitted_at,
                    last_status = excluded.last_status,
                    last_message = excluded.last_message,
                    consecutive_failures = 0
                """,
                (
                    outcome.repo_key,
                    outcome.repo,
                    outcome.market_plugin_id,
                    outcome.version,
                    outcome.commit,
                    submitted_at,
                    outcome.status.value,
                    outcome.message,
                ),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO repo_state (repo_key, repo, last_status, last_message,
                                        consecutive_failures)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(repo_key) DO UPDATE SET
                    last_status = excluded.last_status,
                    last_message = excluded.last_message,
                    consecutive_failures = repo_state.consecutive_failures + 1
                """,
                (
                    outcome.repo_key,
                    outcome.repo,
                    outcome.status.value,
                    outcome.message,
                ),
            )
        self._conn.commit()

    def reset(self, repo_key: str | None = None) -> int:
        """Forget stored state so the next cycle re-evaluates from scratch."""
        if repo_key:
            cursor = self._conn.execute("DELETE FROM repo_state WHERE repo_key = ?", (repo_key,))
        else:
            cursor = self._conn.execute("DELETE FROM repo_state")
        self._conn.commit()
        return cursor.rowcount or 0

    def clear_history(self) -> int:
        cursor = self._conn.execute("DELETE FROM submission_log")
        self._conn.commit()
        return cursor.rowcount or 0


def summarize_status(status: SubmissionStatus) -> str:
    """Human readable Chinese label for a submission status."""
    return {
        SubmissionStatus.PENDING: "审核中",
        SubmissionStatus.APPROVED: "已通过",
        SubmissionStatus.REJECTED: "已拒绝",
        SubmissionStatus.UNKNOWN: "未知",
    }.get(status, "未知")
