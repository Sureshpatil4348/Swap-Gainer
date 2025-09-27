from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from automation import (
    AppConfig,
    AutomationState,
    RiskConfig,
    ScheduleModel,
    TrackedTrade,
    drawdown_breached,
    mark_schedule_triggered,
    parse_time_string,
    schedule_should_trigger,
    spreads_within_entry_limit,
    trades_due_for_close,
)


class AutomationLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig()
        self.state = AutomationState()
        self.now = datetime(2024, 5, 6, 9, 30, tzinfo=timezone.utc)

    def test_parse_time_string_invalid(self) -> None:
        self.assertIsNone(parse_time_string("bad"))
        self.assertIsNone(parse_time_string(""))

    def test_schedule_triggers_once_per_day(self) -> None:
        schedule = ScheduleModel("primary", enabled=True, time_str="09:15", weekdays=[0])
        risk = RiskConfig()

        now = datetime(2024, 5, 6, 9, 10, tzinfo=timezone.utc)
        self.assertFalse(schedule_should_trigger(schedule, now, risk, self.state))

        now = datetime(2024, 5, 6, 9, 16, tzinfo=timezone.utc)
        self.assertTrue(schedule_should_trigger(schedule, now, risk, self.state))
        mark_schedule_triggered(self.state, schedule, now)
        self.assertFalse(schedule_should_trigger(schedule, now, risk, self.state))

        # Next day allowed
        next_day = now + timedelta(days=7)
        self.assertTrue(schedule_should_trigger(schedule, next_day, risk, self.state))

    def test_trades_due_for_close_by_duration(self) -> None:
        risk = RiskConfig(close_after_minutes=60, max_exit_spread=0)
        opened = self.now - timedelta(minutes=65)
        trade = TrackedTrade("T1", opened, ("EURUSD", "USDJPY"))
        result = trades_due_for_close([trade], self.now, risk, {"EURUSD": 2.0})
        self.assertEqual(result, ["T1"])

    def test_trades_due_for_close_by_spread(self) -> None:
        risk = RiskConfig(close_after_minutes=0, max_exit_spread=0.5)
        opened = self.now - timedelta(minutes=10)
        trade = TrackedTrade("T2", opened, ("EURUSD", "USDJPY"))
        spreads = {"EURUSD": 0.4, "USDJPY": 0.3}
        self.assertEqual(trades_due_for_close([trade], self.now, risk, spreads), ["T2"])

    def test_drawdown_detection(self) -> None:
        risk = RiskConfig(drawdown_enabled=True, drawdown_stop=5.0)
        accounts = [{"balance": 1000, "equity": 930}, {"balance": 2000, "equity": 1980}]
        # Combined equity 2910 vs balance 3000 => -3%, not breached
        self.assertFalse(drawdown_breached(risk, accounts))
        accounts = [{"balance": 1000, "equity": 900}, {"balance": 2000, "equity": 1800}]
        self.assertTrue(drawdown_breached(risk, accounts))

    def test_spread_entry_check(self) -> None:
        risk = RiskConfig(max_entry_spread=0.8)
        spreads = {"EURUSD": 0.6, "USDJPY": 0.7}
        self.assertTrue(spreads_within_entry_limit(["EURUSD", "USDJPY"], spreads, risk))
        spreads["USDJPY"] = 1.0
        self.assertFalse(spreads_within_entry_limit(["EURUSD", "USDJPY"], spreads, risk))


if __name__ == "__main__":
    unittest.main()

