"""Offline tests for budget-state rollover and persistence."""

import json
import tempfile
from datetime import datetime as RealDateTime
from pathlib import Path
from unittest.mock import patch

from agent.budget import Budget


class FixedDateTime:
    @classmethod
    def now(cls, tz=None):
        return RealDateTime(2026, 10, 22, 12, 0, tzinfo=tz)


def make_budget(path: Path) -> Budget:
    with patch.object(Budget, "_state_path", path), patch("agent.budget.datetime", FixedDateTime):
        return Budget(rpm=5, tpm=250_000, rpd=20, max_calls=3)


def test_stale_budget_state_is_reset_and_persisted_immediately():
    with tempfile.TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / ".budget.json"
        state_path.write_text(json.dumps({
            "date": "2026-10-21",
            "used": {"gemini-3.1-flash-lite": 19},
            "dead": [],
        }))

        budget = make_budget(state_path)
        saved = json.loads(state_path.read_text())

        assert budget.state["date"] == "2026-10-22"
        assert budget.state["used"] == {}
        assert saved["date"] == "2026-10-22"
        assert saved["used"] == {}
        assert saved["timezone"] == "America/Los_Angeles"


def test_current_day_usage_is_preserved_and_timezone_is_recorded():
    with tempfile.TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / ".budget.json"
        state_path.write_text(json.dumps({
            "date": "2026-10-22",
            "used": {"gemini-3.1-flash-lite": 7},
            "dead": [],
        }))

        budget = make_budget(state_path)
        saved = json.loads(state_path.read_text())

        assert budget.state["used"] == {"gemini-3.1-flash-lite": 7}
        assert saved["used"] == {"gemini-3.1-flash-lite": 7}
        assert saved["timezone"] == "America/Los_Angeles"


if __name__ == "__main__":
    test_stale_budget_state_is_reset_and_persisted_immediately()
    test_current_day_usage_is_preserved_and_timezone_is_recorded()
    print("Budget rollover tests passed; no API or network calls were made.")
