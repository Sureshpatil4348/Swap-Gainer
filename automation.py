from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence


def _default_primary_weekdays() -> List[int]:
    # Monday-Friday
    return [0, 1, 2, 3, 4]


def _default_wednesday() -> List[int]:
    return [2]


@dataclass
class ThreadSchedule:
    thread_id: str
    name: str
    enabled: bool = False
    entry_start: str = "09:00"
    entry_end: str = ""
    symbol1: str = ""
    symbol2: str = ""
    lot1: float = 0.01
    lot2: float = 0.01
    direction: str = "buy_sell"
    max_entry_spread: float = 1.5
    close_after_minutes: int = 120
    max_exit_spread: float = 1.0
    weekdays: List[int] = field(default_factory=_default_primary_weekdays)

    def to_dict(self) -> Dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "name": self.name,
            "enabled": self.enabled,
            "entry_start": self.entry_start,
            "entry_end": self.entry_end,
            "symbol1": self.symbol1,
            "symbol2": self.symbol2,
            "lot1": self.lot1,
            "lot2": self.lot2,
            "direction": self.direction,
            "max_entry_spread": self.max_entry_spread,
            "close_after_minutes": self.close_after_minutes,
            "max_exit_spread": self.max_exit_spread,
            "weekdays": list(self.weekdays),
        }

    @classmethod
    def from_dict(
        cls,
        data: Optional[Dict[str, object]],
        *,
        default_id: str,
        default_name: str,
        weekdays: Optional[Sequence[int]] = None,
    ) -> "ThreadSchedule":
        data = data or {}
        wd = list(weekdays) if weekdays is not None else list(data.get("weekdays", [])) or _default_primary_weekdays()
        return cls(
            thread_id=str(data.get("thread_id") or default_id),
            name=str(data.get("name") or default_name),
            enabled=bool(data.get("enabled", False)),
            entry_start=str(data.get("entry_start") or "09:00"),
            entry_end=str(data.get("entry_end") or ""),
            symbol1=str(data.get("symbol1") or ""),
            symbol2=str(data.get("symbol2") or ""),
            lot1=float(data.get("lot1", 0.01) or 0.01),
            lot2=float(data.get("lot2", 0.01) or 0.01),
            direction=str(data.get("direction") or "buy_sell"),
            max_entry_spread=float(data.get("max_entry_spread", 1.5) or 0.0),
            close_after_minutes=int(data.get("close_after_minutes", 120) or 0),
            max_exit_spread=float(data.get("max_exit_spread", 1.0) or 0.0),
            weekdays=wd,
        )


@dataclass
class RiskConfig:
    drawdown_enabled: bool = False
    drawdown_stop: float = 5.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "drawdown_enabled": self.drawdown_enabled,
            "drawdown_stop": self.drawdown_stop,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, object]]) -> "RiskConfig":
        data = data or {}
        return cls(
            drawdown_enabled=bool(data.get("drawdown_enabled", False)),
            drawdown_stop=float(data.get("drawdown_stop", 5.0) or 0.0),
        )


def _default_primary_threads() -> List[ThreadSchedule]:
    return [
        ThreadSchedule(
            thread_id="primary-1",
            name="Primary Set 1",
            weekdays=_default_primary_weekdays(),
        ),
        ThreadSchedule(
            thread_id="primary-2",
            name="Primary Set 2",
            weekdays=_default_primary_weekdays(),
        ),
    ]


def _default_wednesday_threads() -> List[ThreadSchedule]:
    return [
        ThreadSchedule(
            thread_id="wednesday-1",
            name="Wednesday Set 1",
            weekdays=_default_wednesday(),
        ),
        ThreadSchedule(
            thread_id="wednesday-2",
            name="Wednesday Set 2",
            weekdays=_default_wednesday(),
        ),
        ThreadSchedule(
            thread_id="wednesday-3",
            name="Wednesday Set 3",
            weekdays=_default_wednesday(),
        ),
    ]


@dataclass
class AppConfig:
    timezone: str = "UTC"
    primary_threads: List[ThreadSchedule] = field(default_factory=_default_primary_threads)
    wednesday_threads: List[ThreadSchedule] = field(default_factory=_default_wednesday_threads)
    risk: RiskConfig = field(default_factory=RiskConfig)

    def to_dict(self) -> Dict[str, object]:
        return {
            "timezone": self.timezone,
            "primary_threads": [thread.to_dict() for thread in self.primary_threads],
            "wednesday_threads": [thread.to_dict() for thread in self.wednesday_threads],
            "risk": self.risk.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, object]]) -> "AppConfig":
        data = data or {}
        timezone = str(data.get("timezone") or "UTC")

        def _parse_threads(
            raw: Optional[Sequence[Dict[str, object]]],
            defaults: List[ThreadSchedule],
            prefix: str,
            weekdays: Sequence[int],
        ) -> List[ThreadSchedule]:
            threads: List[ThreadSchedule] = []
            if raw:
                for idx, item in enumerate(raw, start=1):
                    default_id = f"{prefix}-{idx}"
                    default_name = f"{prefix.title()} Set {idx}"
                    threads.append(
                        ThreadSchedule.from_dict(
                            item,
                            default_id=default_id,
                            default_name=default_name,
                            weekdays=weekdays,
                        )
                    )
            if not threads:
                threads = [thread for thread in defaults]

            # Ensure thread IDs are unique to avoid automation collisions when
            # users duplicate configuration blocks without changing the ID.
            used_ids: dict[str, int] = {}
            for idx, thread in enumerate(threads, start=1):
                base_id = (thread.thread_id or f"{prefix}-{idx}").strip() or f"{prefix}-{idx}"
                candidate = base_id
                suffix = 1
                while candidate in used_ids:
                    suffix += 1
                    candidate = f"{base_id}-{suffix}"
                used_ids[candidate] = 1
                if candidate != thread.thread_id:
                    thread.thread_id = candidate
            return threads

        if "primary_threads" in data or "wednesday_threads" in data:
            primary_threads = _parse_threads(
                data.get("primary_threads"),
                _default_primary_threads(),
                "primary",
                _default_primary_weekdays(),
            )
            wednesday_threads = _parse_threads(
                data.get("wednesday_threads"),
                _default_wednesday_threads(),
                "wednesday",
                _default_wednesday(),
            )
        else:
            # Backwards compatibility with single schedule configuration
            primary_schedule = ThreadSchedule.from_dict(
                data.get("primary"),
                default_id="primary-1",
                default_name="Primary Set 1",
                weekdays=_default_primary_weekdays(),
            )
            wednesday_schedule = ThreadSchedule.from_dict(
                data.get("wednesday"),
                default_id="wednesday-1",
                default_name="Wednesday Set 1",
                weekdays=_default_wednesday(),
            )
            primary_threads = _default_primary_threads()
            wednesday_threads = _default_wednesday_threads()
            primary_threads[0] = primary_schedule
            wednesday_threads[0] = wednesday_schedule

        # Ensure consistent number of threads (2 primary, 3 wednesday)
        primary_threads = (primary_threads + _default_primary_threads())[:2]
        wednesday_threads = (wednesday_threads + _default_wednesday_threads())[:3]

        risk = RiskConfig.from_dict(data.get("risk"))
        return cls(
            timezone=timezone,
            primary_threads=primary_threads,
            wednesday_threads=wednesday_threads,
            risk=risk,
        )


@dataclass
class AutomationState:
    last_runs: Dict[str, str] = field(default_factory=dict)
    trade_history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {"last_runs": dict(self.last_runs), "trade_history": [dict(entry) for entry in self.trade_history]}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, object]]) -> "AutomationState":
        data = data or {}
        lr = data.get("last_runs") or {}
        raw_history = data.get("trade_history") or []
        history: List[Dict[str, Any]] = []
        if isinstance(raw_history, list):
            for item in raw_history:
                if isinstance(item, dict):
                    history.append({str(k): item[k] for k in item.keys()})
        return cls(
            last_runs={str(k): str(v) for k, v in lr.items()},
            trade_history=history,
        )


@dataclass
class TrackedTrade:
    trade_id: str
    opened_at: datetime
    symbols: Sequence[str]
    close_after_minutes: int
    max_exit_spread: float


def parse_time_string(value: str) -> Optional[time]:
    if not value:
        return None
    try:
        parts = value.split(":")
        if len(parts) < 2:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
        return time(hour=hour, minute=minute, second=second)
    except Exception:
        return None


def _time_in_window(target: time, start: Optional[time], end: Optional[time]) -> bool:
    if start and end:
        if start <= end:
            return start <= target <= end
        # Overnight window (e.g. 23:00 - 01:00)
        return target >= start or target <= end
    if start:
        return target >= start
    if end:
        return target <= end
    return True


def schedule_should_trigger(
    schedule: ThreadSchedule,
    now: datetime,
    state: AutomationState,
) -> bool:
    if not schedule.enabled:
        return False
    if schedule.weekdays and now.weekday() not in schedule.weekdays:
        return False
    start_at = parse_time_string(schedule.entry_start)
    end_at = parse_time_string(schedule.entry_end) if schedule.entry_end else None
    if start_at is None and end_at is None:
        return False
    if not _time_in_window(now.time(), start_at, end_at):
        return False
    last_key = state.last_runs.get(schedule.thread_id)
    if last_key == now.date().isoformat():
        return False
    return True


def mark_schedule_triggered(state: AutomationState, schedule: ThreadSchedule, when: datetime) -> None:
    state.last_runs[schedule.thread_id] = when.date().isoformat()


def trades_due_for_close(
    trades: Iterable[TrackedTrade],
    now: datetime,
    spreads: Dict[str, float],
) -> List[str]:
    to_close: List[str] = []
    for trade in trades:
        hold_delta = (
            timedelta(minutes=max(trade.close_after_minutes, 0))
            if trade.close_after_minutes > 0
            else None
        )
        should_close = False
        if hold_delta is not None and now - trade.opened_at >= hold_delta:
            should_close = True
        if not should_close and trade.max_exit_spread > 0:
            spreads_ok = []
            for sym in trade.symbols:
                spread = spreads.get(sym)
                if spread is None:
                    spreads_ok.append(False)
                else:
                    spreads_ok.append(spread <= trade.max_exit_spread)
            if spreads_ok and all(spreads_ok):
                should_close = True
        if should_close:
            to_close.append(trade.trade_id)
    return to_close


def drawdown_breached(risk: RiskConfig, accounts: Sequence[Dict[str, float]]) -> bool:
    if not risk.drawdown_enabled:
        return False
    total_balance = 0.0
    total_equity = 0.0
    for acc in accounts:
        total_balance += float(acc.get("balance", 0.0) or 0.0)
        total_equity += float(acc.get("equity", 0.0) or 0.0)
    if total_balance <= 0:
        return False
    drawdown_pct = ((total_equity - total_balance) / total_balance) * 100.0
    return drawdown_pct <= -abs(risk.drawdown_stop)


def spreads_within_entry_limit(
    symbols: Sequence[str],
    spreads: Dict[str, float],
    max_spread: float,
) -> bool:
    if max_spread <= 0:
        return True
    for sym in symbols:
        spread = spreads.get(sym)
        if spread is None:
            return False
        if spread > max_spread:
            return False
    return True


