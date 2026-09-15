"""Dedup, rate limit bookkeeping and history persistence."""

from __future__ import annotations

from datetime import timedelta

from astrbot_auto_market_push.models import SubmissionStatus, SubmitOutcome, utcnow
from astrbot_auto_market_push.state import StateStore


def _outcome(**overrides) -> SubmitOutcome:
    data = {
        "repo_key": "kelai141/astrbot_plugin_demo",
        "repo": "https://github.com/kelai141/astrbot_plugin_demo",
        "version": "1.0.0",
        "commit": "abc123",
        "submitted": True,
        "status": SubmissionStatus.PENDING,
        "message": "ok",
    }
    data.update(overrides)
    return SubmitOutcome(**data)


def test_touch_and_record_roundtrip(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        store.touch_repo(
            repo_key="kelai141/astrbot_plugin_demo",
            repo="https://github.com/kelai141/astrbot_plugin_demo",
            ref=None,
            version="1.0.0",
            commit="abc123",
        )
        state = store.get_repo_state("kelai141/astrbot_plugin_demo")
        assert state["last_seen_version"] == "1.0.0"
        assert state["last_submitted_version"] is None

        store.record_outcome(_outcome())
        state = store.get_repo_state("kelai141/astrbot_plugin_demo")
        assert state["last_submitted_version"] == "1.0.0"
        assert state["last_status"] == "pending"
        assert state["consecutive_failures"] == 0

        assert store.was_submitted("kelai141/astrbot_plugin_demo", "1.0.0")
        assert not store.was_submitted("kelai141/astrbot_plugin_demo", "1.0.1")


def test_dry_run_outcomes_do_not_pollute_dedup(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        store.record_outcome(_outcome(dry_run=True, submitted=False, version="9.9.9"))
        assert not store.was_submitted("kelai141/astrbot_plugin_demo", "9.9.9")
        assert store.submissions_in_last_24h() == 0
        # The attempt is still auditable in the history.
        assert len(store.history()) == 1


def test_failure_increments_consecutive_failures(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        for _ in range(2):
            store.record_outcome(
                _outcome(submitted=False, status=SubmissionStatus.UNKNOWN, message="boom")
            )
        state = store.get_repo_state("kelai141/astrbot_plugin_demo")
        assert state["consecutive_failures"] == 2
        assert state["last_submitted_version"] is None

        store.record_outcome(_outcome(version="2.0.0"))
        state = store.get_repo_state("kelai141/astrbot_plugin_demo")
        assert state["consecutive_failures"] == 0


def test_rate_limit_window_and_reset(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        store.record_outcome(_outcome(version="1.0.0"))
        store.record_outcome(_outcome(version="1.0.1"))
        assert store.submissions_in_last_24h() == 2

        # Anything older than 24h must fall out of the window.
        old = utcnow() - timedelta(hours=25)
        store.record_outcome(_outcome(version="1.0.2", submitted_at=old))
        assert store.submissions_in_last_24h() == 2
        assert store.submissions_since(utcnow() - timedelta(hours=48)) == 3

        assert store.reset("kelai141/astrbot_plugin_demo") == 1
        assert store.get_repo_state("kelai141/astrbot_plugin_demo") is None


def test_status_counts(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        store.record_outcome(_outcome(version="1.0.0"))
        store.record_outcome(_outcome(version="1.0.1", status=SubmissionStatus.APPROVED))
        assert store.status_counts() == {"pending": 1, "approved": 1}
