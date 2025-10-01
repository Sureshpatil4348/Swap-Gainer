from __future__ import annotations

import sys
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from automation import (
    AppConfig,
    AutomationState,
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
from main import App


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
        result = trades_due_for_close(
            [trade],
            self.now,
            {"EURUSD": 2.0},
            {"T1": 0.0},
        )
        self.assertEqual(result, [("T1", "spread")])

    def test_trades_due_for_close_by_spread(self) -> None:
        opened = self.now - timedelta(minutes=10)
        trade = TrackedTrade("T2", opened, ("EURUSD", "USDJPY"), 0, 0.5)
        spreads = {"EURUSD": 0.4, "USDJPY": 0.3}
        self.assertEqual(
            trades_due_for_close([trade], self.now, spreads, {"T2": 0.0}),
            [("T2", "spread")],
        )

    def test_trade_waits_for_hold_time_before_spread_exit(self) -> None:
        opened = self.now - timedelta(minutes=10)
        trade = TrackedTrade("T3", opened, ("EURUSD", "USDJPY"), 60, 0.5)
        spreads = {"EURUSD": 0.4, "USDJPY": 0.3}
        # Still within hold period, should not close
        self.assertEqual(trades_due_for_close([trade], self.now, spreads, {"T3": 0.0}), [])

        later = self.now + timedelta(minutes=60)
        self.assertEqual(
            trades_due_for_close([trade], later, spreads, {"T3": 0.0}),
            [("T3", "spread")],
        )

    def test_trades_due_for_close_profit_condition(self) -> None:
        opened = self.now - timedelta(minutes=90)
        trade = TrackedTrade(
            "T4",
            opened,
            ("EURUSD", "USDJPY"),
            30,
            0.4,
            "profit",
            12.0,
        )
        spreads = {"EURUSD": 0.2, "USDJPY": 0.3}
        self.assertEqual(
            trades_due_for_close([trade], self.now, spreads, {"T4": 11.0}),
            [],
        )
        self.assertEqual(
            trades_due_for_close([trade], self.now, spreads, {"T4": 12.5}),
            [("T4", "profit")],
        )

    def test_gather_active_trades_uses_running_profit(self) -> None:
        schedule = ThreadSchedule(
            thread_id="primary-1",
            name="Primary",
            enabled=True,
            symbol1="EURUSD",
            symbol2="USDJPY",
            close_condition="profit",
            min_combined_profit=10.0,
        )
        config = AppConfig(
            timezone="UTC",
            primary_threads=[schedule],
            wednesday_threads=[],
            risk=RiskConfig(),
        )
        app = App.__new__(App)
        app._trade_lock = threading.Lock()
        now = datetime(2024, 5, 6, 12, 0, tzinfo=timezone.utc)
        app.paired_trades = {
            "T100": {
                "opened_at": now.timestamp(),
                "thread_id": "primary-1",
                "account1": {
                    "symbol": "EURUSD",
                    "last_profit": 8.0,
                    "last_commission": -1.0,
                    "last_swap": -0.5,
                },
                "account2": {
                    "symbol": "USDJPY",
                    "last_profit": 5.0,
                    "last_commission": -0.75,
                    "last_swap": 0.25,
                },
            }
        }
        app.worker1 = None
        app.worker2 = None

        trades, requests, profits = app._gather_active_trades(now, config)
        self.assertEqual(len(trades), 1)
        self.assertEqual(len(requests), 2)
        # Combined profit should use the running PnL values only.
        expected_profit = 8.0 + 5.0
        self.assertAlmostEqual(profits["T100"], expected_profit)

    def test_trades_due_for_close_respects_close_window(self) -> None:
        opened = self.now - timedelta(minutes=180)
        trade = TrackedTrade(
            "T5",
            opened,
            ("EURUSD", "USDJPY"),
            60,
            0.2,
            "spread",
            0.0,
            time(10, 0),
            time(12, 0),
        )
        spreads = {"EURUSD": 0.1, "USDJPY": 0.15}
        before_window = datetime(2024, 5, 6, 9, 30, tzinfo=timezone.utc)
        inside_window = datetime(2024, 5, 6, 10, 30, tzinfo=timezone.utc)
        self.assertEqual(
            trades_due_for_close([trade], before_window, spreads, {"T5": 5.0}),
            [],
        )
        self.assertEqual(
            trades_due_for_close([trade], inside_window, spreads, {"T5": 5.0}),
            [("T5", "spread")],
        )

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

    def test_thread_schedule_close_condition_defaults(self) -> None:
        schedule = ThreadSchedule.from_dict(
            {
                "thread_id": "x1",
                "close_condition": "unknown",
                "close_window_start": "08:00",
                "close_window_end": "09:30",
                "min_combined_profit": 5,
            },
            default_id="x1",
            default_name="Test",
            weekdays=[0],
        )
        self.assertEqual(schedule.close_condition, "spread")
        self.assertEqual(schedule.close_window_start, "08:00")
        self.assertEqual(schedule.close_window_end, "09:30")

        schedule_profit = ThreadSchedule.from_dict(
            {
                "thread_id": "x2",
                "close_condition": "profit",
                "min_combined_profit": 7.5,
            },
            default_id="x2",
            default_name="Test2",
            weekdays=[0],
        )
        self.assertEqual(schedule_profit.close_condition, "profit")
        self.assertAlmostEqual(schedule_profit.min_combined_profit, 7.5)


if __name__ == "__main__":
    unittest.main()

