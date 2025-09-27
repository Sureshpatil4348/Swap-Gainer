from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Sequence


def _default_primary_weekdays() -> List[int]:
    # Monday-Friday
    return [0, 1, 2, 3, 4]


def _default_wednesday() -> List[int]:
    return [2]


@dataclass
class ScheduleModel:
    name: str
    enabled: bool = False
    time_str: str = "09:00"
    symbol1: str = ""
    symbol2: str = ""
    lot1: float = 0.01
    lot2: float = 0.01
    direction: str = "buy_sell"
    weekdays: List[int] = field(default_factory=_default_primary_weekdays)

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "time_str": self.time_str,
            "symbol1": self.symbol1,
            "symbol2": self.symbol2,
            "lot1": self.lot1,
            "lot2": self.lot2,
            "direction": self.direction,
            "weekdays": list(self.weekdays),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, object]], *, default_name: str, weekdays: Optional[Sequence[int]] = None) -> "ScheduleModel":
        data = data or {}
        wd = list(weekdays) if weekdays is not None else list(data.get("weekdays", [])) or _default_primary_weekdays()
        return cls(
            name=str(data.get("name") or default_name),
            enabled=bool(data.get("enabled", False)),
            time_str=str(data.get("time_str") or "09:00"),
            symbol1=str(data.get("symbol1") or ""),
            symbol2=str(data.get("symbol2") or ""),
            lot1=float(data.get("lot1", 0.01) or 0.01),
            lot2=float(data.get("lot2", 0.01) or 0.01),
            direction=str(data.get("direction") or "buy_sell"),
            weekdays=wd,
        )


@dataclass
class RiskConfig:
    close_after_minutes: int = 120
    max_entry_spread: float = 1.5
    max_exit_spread: float = 1.0
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    drawdown_enabled: bool = False
    drawdown_stop: float = 5.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "close_after_minutes": self.close_after_minutes,
            "max_entry_spread": self.max_entry_spread,
            "max_exit_spread": self.max_exit_spread,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "drawdown_enabled": self.drawdown_enabled,
            "drawdown_stop": self.drawdown_stop,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, object]]) -> "RiskConfig":
        data = data or {}
        return cls(
            close_after_minutes=int(data.get("close_after_minutes", 120) or 0),
            max_entry_spread=float(data.get("max_entry_spread", 1.5) or 0.0),
            max_exit_spread=float(data.get("max_exit_spread", 1.0) or 0.0),
            start_date=str(data.get("start_date")) if data.get("start_date") else None,
            end_date=str(data.get("end_date")) if data.get("end_date") else None,
            drawdown_enabled=bool(data.get("drawdown_enabled", False)),
            drawdown_stop=float(data.get("drawdown_stop", 5.0) or 0.0),
        )


@dataclass
class AppConfig:
    timezone: str = "UTC"
    primary: ScheduleModel = field(default_factory=lambda: ScheduleModel("primary"))
    wednesday: ScheduleModel = field(
        default_factory=lambda: ScheduleModel("wednesday", weekdays=_default_wednesday())
    )
    risk: RiskConfig = field(default_factory=RiskConfig)

    def to_dict(self) -> Dict[str, object]:
        return {
            "timezone": self.timezone,
            "primary": self.primary.to_dict(),
            "wednesday": self.wednesday.to_dict(),
            "risk": self.risk.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, object]]) -> "AppConfig":
        data = data or {}
        timezone = str(data.get("timezone") or "UTC")
        primary = ScheduleModel.from_dict(data.get("primary"), default_name="primary")
        wednesday = ScheduleModel.from_dict(
            data.get("wednesday"), default_name="wednesday", weekdays=_default_wednesday()
        )
        risk = RiskConfig.from_dict(data.get("risk"))
        return cls(timezone=timezone, primary=primary, wednesday=wednesday, risk=risk)


@dataclass
class AutomationState:
    last_runs: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {"last_runs": dict(self.last_runs)}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, object]]) -> "AutomationState":
        data = data or {}
        lr = data.get("last_runs") or {}
        return cls(last_runs={str(k): str(v) for k, v in lr.items()})


@dataclass
class TrackedTrade:
    trade_id: str
    opened_at: datetime
    symbols: Sequence[str]


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


def is_within_date_range(target: date, risk: RiskConfig) -> bool:
    start = None
    end = None
    if risk.start_date:
        try:
            start = datetime.strptime(risk.start_date, "%Y-%m-%d").date()
        except Exception:
            start = None
    if risk.end_date:
        try:
            end = datetime.strptime(risk.end_date, "%Y-%m-%d").date()
        except Exception:
            end = None
    if start and target < start:
        return False
    if end and target > end:
        return False
    return True


def schedule_should_trigger(
    schedule: ScheduleModel,
    now: datetime,
    risk: RiskConfig,
    state: AutomationState,
) -> bool:
    if not schedule.enabled:
        return False
    if schedule.weekdays and now.weekday() not in schedule.weekdays:
        return False
    if not is_within_date_range(now.date(), risk):
        return False
    trigger_at = parse_time_string(schedule.time_str)
    if trigger_at is None:
        return False
    if now.time() < trigger_at:
        return False
    last_key = state.last_runs.get(schedule.name)
    if last_key == now.date().isoformat():
        return False
    return True


def mark_schedule_triggered(state: AutomationState, schedule: ScheduleModel, when: datetime) -> None:
    state.last_runs[schedule.name] = when.date().isoformat()


def trades_due_for_close(
    trades: Iterable[TrackedTrade],
    now: datetime,
    risk: RiskConfig,
    spreads: Dict[str, float],
) -> List[str]:
    to_close: List[str] = []
    hold_delta = timedelta(minutes=max(risk.close_after_minutes, 0)) if risk.close_after_minutes > 0 else None
    for trade in trades:
        should_close = False
        if hold_delta is not None and now - trade.opened_at >= hold_delta:
            should_close = True
        if not should_close and risk.max_exit_spread > 0:
            spreads_ok = []
            for sym in trade.symbols:
                spread = spreads.get(sym)
                if spread is None:
                    spreads_ok.append(False)
                else:
                    spreads_ok.append(spread <= risk.max_exit_spread)
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
    risk: RiskConfig,
) -> bool:
    if risk.max_entry_spread <= 0:
        return True
    for sym in symbols:
        spread = spreads.get(sym)
        if spread is None:
            return False
        if spread > risk.max_entry_spread:
            return False
    return True


