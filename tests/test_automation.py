from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from automation import (
    AppConfig,
    AutomationState,
    ExitConfig,
    RiskConfig,
    ThreadSchedule,
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

    def test_state_active_trades_round_trip(self) -> None:
        state = AutomationState(
            last_runs={"primary-1": "2024-05-06"},
            trade_history=[{"trade_id": "T00001"}],
            active_trades=[
                {
                    "trade_id": "T00002",
                    "account1": {"symbol": "EURUSD", "lot": 0.01},
                    "account2": {"symbol": "USDJPY", "lot": 0.02},
                }
            ],
        )

        data = state.to_dict()
        restored = AutomationState.from_dict(data)

        self.assertEqual(restored.active_trades, state.active_trades)

    def test_state_active_trades_missing_defaults_to_empty(self) -> None:
        restored = AutomationState.from_dict({"last_runs": {}, "trade_history": []})
        self.assertEqual(restored.active_trades, [])

    def test_exit_config_defaults(self) -> None:
        config = AppConfig.from_dict({})
        self.assertEqual(config.exit.close_logic_mode, "spread")
        self.assertEqual(config.exit.net_pnl_threshold, 0.0)
        self.assertEqual(config.exit.close_start_minutes, 60)
        self.assertEqual(config.exit.close_stop_minutes, 90)

    def test_exit_config_custom_values(self) -> None:
        data = {
            "exit": {
                "close_logic_mode": "net_pnl_threshold",
                "net_pnl_threshold": 12.5,
                "close_start_minutes": 45,
                "close_stop_minutes": 90,
            }
        }
        config = AppConfig.from_dict(data)
        self.assertEqual(config.exit.close_logic_mode, "net_pnl_threshold")
        self.assertEqual(config.exit.net_pnl_threshold, 12.5)
        self.assertEqual(config.exit.close_start_minutes, 45)
        self.assertEqual(config.exit.close_stop_minutes, 90)

    def test_schedule_triggers_once_per_day(self) -> None:
        schedule = ThreadSchedule(
            thread_id="primary-1",
            name="Primary Set 1",
            enabled=True,
            entry_start="09:15",
            entry_end="09:45",
            weekdays=[0],
        )

        now = datetime(2024, 5, 6, 9, 10, tzinfo=timezone.utc)
        self.assertFalse(schedule_should_trigger(schedule, now, self.state))

        now = datetime(2024, 5, 6, 9, 20, tzinfo=timezone.utc)
        self.assertTrue(schedule_should_trigger(schedule, now, self.state))
        mark_schedule_triggered(self.state, schedule, now)
        self.assertFalse(schedule_should_trigger(schedule, now, self.state))

        # Next occurrence allowed
        next_day = now + timedelta(days=7)
        self.assertTrue(schedule_should_trigger(schedule, next_day, self.state))

    def test_trades_due_for_close_by_duration(self) -> None:
        opened = self.now - timedelta(minutes=65)
        trade = TrackedTrade("T1", opened, ("EURUSD", "USDJPY"), 60, 0.0)
        result = trades_due_for_close([trade], self.now, {"EURUSD": 2.0})
        self.assertEqual(result, ["T1"])

    def test_trades_due_for_close_by_spread(self) -> None:
        opened = self.now - timedelta(minutes=10)
        trade = TrackedTrade("T2", opened, ("EURUSD", "USDJPY"), 0, 0.5)
        spreads = {"EURUSD": 0.4, "USDJPY": 0.3}
        self.assertEqual(trades_due_for_close([trade], self.now, spreads), ["T2"])

    def test_trade_waits_for_hold_time_before_spread_exit(self) -> None:
        opened = self.now - timedelta(minutes=10)
        trade = TrackedTrade("T3", opened, ("EURUSD", "USDJPY"), 60, 0.5)
        spreads = {"EURUSD": 0.4, "USDJPY": 0.3}
        # Still within hold period, should not close
        self.assertEqual(trades_due_for_close([trade], self.now, spreads), [])

        later = self.now + timedelta(minutes=60)
        self.assertEqual(trades_due_for_close([trade], later, spreads), ["T3"])

    def test_drawdown_detection(self) -> None:
        risk = RiskConfig(drawdown_enabled=True, drawdown_stop=5.0)
        accounts = [{"balance": 1000, "equity": 930}, {"balance": 2000, "equity": 1980}]
        # Combined equity 2910 vs balance 3000 => -3%, not breached
        self.assertFalse(drawdown_breached(risk, accounts))
        accounts = [{"balance": 1000, "equity": 900}, {"balance": 2000, "equity": 1800}]
        self.assertTrue(drawdown_breached(risk, accounts))

    def test_spread_entry_check(self) -> None:
        spreads = {"EURUSD": 0.6, "USDJPY": 0.7}
        self.assertTrue(spreads_within_entry_limit(["EURUSD", "USDJPY"], spreads, 0.8))
        spreads["USDJPY"] = 1.0
        self.assertFalse(spreads_within_entry_limit(["EURUSD", "USDJPY"], spreads, 0.8))

    def test_duplicate_thread_ids_become_unique(self) -> None:
        data = {
            "timezone": "UTC",
            "primary_threads": [
                {"thread_id": "primary-dup", "name": "Primary One", "enabled": True, "symbol1": "EURUSD"},
                {"thread_id": "primary-dup", "name": "Primary Two", "enabled": True, "symbol1": "USDJPY"},
            ],
            "wednesday_threads": [
                {"thread_id": "wed-dup", "name": "Wed One", "enabled": True, "symbol1": "GBPUSD"},
                {"thread_id": "wed-dup", "name": "Wed Two", "enabled": False, "symbol1": "AUDUSD"},
            ],
        }

        config = AppConfig.from_dict(data)
        primary_ids = [thread.thread_id for thread in config.primary_threads]
        wednesday_ids = [thread.thread_id for thread in config.wednesday_threads]

        self.assertEqual(len(primary_ids), len(set(primary_ids)))
        self.assertEqual(len(wednesday_ids), len(set(wednesday_ids)))
        self.assertTrue(all(thread.thread_id for thread in config.primary_threads))
        self.assertTrue(all(thread.thread_id for thread in config.wednesday_threads))

    def test_config_respects_custom_weekdays(self) -> None:
        data = {
            "timezone": "UTC",
            "primary_threads": [
                {
                    "thread_id": "primary-1",
                    "name": "Primary One",
                    "enabled": True,
                    "symbol1": "EURUSD",
                    "weekdays": [1],
                }
            ],
            "wednesday_threads": [
                {
                    "thread_id": "wednesday-1",
                    "name": "Wed One",
                    "enabled": True,
                    "symbol1": "GBPUSD",
                    "weekdays": [2],
                }
            ],
        }

        config = AppConfig.from_dict(data)
        self.assertEqual(config.primary_threads[0].weekdays, [1])
        self.assertEqual(config.wednesday_threads[0].weekdays, [2])


if __name__ == "__main__":
    unittest.main()

