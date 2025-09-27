import os
import sys
import uuid
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Sequence
from zoneinfo import ZoneInfo

import tkinter as tk
from tkinter import ttk, messagebox

from multiprocessing import get_context
from mt5_worker import worker_main

from automation import (
    AppConfig,
    AutomationState,
    RiskConfig,
    ScheduleModel,
    TrackedTrade,
    drawdown_breached,
    mark_schedule_triggered,
    schedule_should_trigger,
    spreads_within_entry_limit,
    trades_due_for_close,
)
from persistence import Persistence


DEFAULT_TERMINAL_1 = r"C:\Users\Public\Desktop\XM Global MT5.lnk"
DEFAULT_TERMINAL_2 = r"C:\Users\Public\Desktop\Tickmill MT5 Terminal.lnk"


class WorkerClient:
    def __init__(self, name: str, terminal_path: str) -> None:
        self.name = name
        self.ctx = get_context("spawn")
        self.req_q = self.ctx.Queue()
        self.res_q = self.ctx.Queue()
        self.proc = self.ctx.Process(
            target=worker_main,
            args=(self.req_q, self.res_q, terminal_path, name),
            daemon=True,
        )
        self.proc.start()
        self._lock = threading.Lock()
        self._connected = False

    def _rpc(self, cmd: str, params: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
        request_id = str(uuid.uuid4())
        payload = {"id": request_id, "cmd": cmd, "params": params}

        with self._lock:
            self.req_q.put(payload)
            end_time = time.time() + timeout
            while True:
                remaining = max(0.0, end_time - time.time())
                if remaining == 0.0:
                    raise TimeoutError(f"Timeout waiting for response to {cmd}")
                res = self.res_q.get(timeout=remaining)
                if res.get("id") == request_id:
                    if res.get("status") == "ok":
                        return res.get("data") or {}
                    raise RuntimeError(res.get("error") or "Unknown error")
                # Single outstanding call per worker; ignore mismatched (shouldn't happen)

    def connect(self, path: str) -> Dict[str, Any]:
        data = self._rpc("connect", {"path": path})
        self._connected = True
        return data

    def buy(self, symbol: str, volume: float, pair_id: str, magic: int) -> Dict[str, Any]:
        return self._rpc("buy", {"symbol": symbol, "volume": volume, "pair_id": pair_id, "magic": magic})

    def sell(self, symbol: str, volume: float, pair_id: str, magic: int) -> Dict[str, Any]:
        return self._rpc("sell", {"symbol": symbol, "volume": volume, "pair_id": pair_id, "magic": magic})

    def get_profit(self, position_ticket: int) -> Dict[str, Any]:
        return self._rpc("get_profit", {"position_ticket": int(position_ticket)})

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        return self._rpc("get_quote", {"symbol": symbol})

    def get_account_info(self) -> Dict[str, Any]:
        return self._rpc("get_account_info", {})

    def close(self, position_ticket: int, symbol: str, side: str, volume: float, magic: int) -> Dict[str, Any]:
        return self._rpc(
            "close",
            {
                "position_ticket": int(position_ticket),
                "symbol": symbol,
                "side": side,
                "volume": float(volume),
                "magic": int(magic),
            },
        )

    def shutdown(self) -> None:
        try:
            self._rpc("shutdown", {})
        except Exception:
            pass
        try:
            if self.proc.is_alive():
                self.proc.terminate()
        except Exception:
            pass


class ScrollableTable(ttk.Frame):
    def __init__(self, master: tk.Misc, columns: list[str]) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.scroll_y = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll_y.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scroll_y.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # Header
        for c, col in enumerate(columns):
            lbl = ttk.Label(self.inner, text=col, font=("Segoe UI", 9, "bold"))
            lbl.grid(row=0, column=c, sticky="nsew", padx=4, pady=(2, 6))
            self.inner.columnconfigure(c, weight=1)

        self._next_row = 1
        self._rows: Dict[str, Dict[str, Any]] = {}

    def add_row(self, row_id: str, values: list[Any], dynamic_indices: Dict[str, int], close_callback) -> None:
        widgets = []
        p1_idx = dynamic_indices.get("p1", -1)
        p2_idx = dynamic_indices.get("p2", -1)
        combined_idx = dynamic_indices.get("combined", -1)

        p1_label = None
        p2_label = None
        combined_label = None

        for c, val in enumerate(values[:-1]):  # except last column (Close button)
            if c in (p1_idx, p2_idx, combined_idx):
                lbl = ttk.Label(self.inner, text=str(val))
                lbl.grid(row=self._next_row, column=c, sticky="nsew", padx=4, pady=2)
                if c == p1_idx:
                    p1_label = lbl
                elif c == p2_idx:
                    p2_label = lbl
                else:
                    combined_label = lbl
                widgets.append(lbl)
            else:
                w = ttk.Label(self.inner, text=str(val))
                w.grid(row=self._next_row, column=c, sticky="nsew", padx=4, pady=2)
                widgets.append(w)

        # Close button
        btn = ttk.Button(self.inner, text="Close", command=lambda: close_callback(row_id))
        btn.grid(row=self._next_row, column=len(values) - 1, sticky="nsew", padx=4, pady=2)

        self._rows[row_id] = {
            "widgets": widgets,
            "p1_label": p1_label,
            "p2_label": p2_label,
            "combined_label": combined_label,
            "button": btn,
            "row_index": self._next_row,
        }
        self._next_row += 1

    def set_profits(self, row_id: str, p1: float, p2: float, combined: float) -> None:
        row = self._rows.get(row_id)
        if not row:
            return
        if row.get("p1_label"):
            row["p1_label"].configure(text=f"{p1:.2f}")
        if row.get("p2_label"):
            row["p2_label"].configure(text=f"{p2:.2f}")
        if row.get("combined_label"):
            row["combined_label"].configure(text=f"{combined:.2f}")

    def remove_row(self, row_id: str) -> None:
        row = self._rows.pop(row_id, None)
        if not row:
            return
        for w in row.get("widgets", []):
            w.destroy()
        if row.get("profit_label"):
            row["profit_label"].destroy()
        if row.get("button"):
            row["button"].destroy()


class AutomationRunner:
    def __init__(self, app: "App", persistence: Persistence) -> None:
        self.app = app
        self.persistence = persistence
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="AutomationRunner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                config = self.persistence.get_config()
                state = self.persistence.get_state()
                tz = config.timezone or "UTC"
                try:
                    now = datetime.now(ZoneInfo(tz))
                except Exception:
                    now = datetime.utcnow()
                changed = self.app.evaluate_automation(now, config, state)
                if changed:
                    self.persistence.save_state(state)
            except Exception as exc:
                print(f"Automation loop error: {exc}", file=sys.stderr)
            finally:
                if self._stop_event.wait(1.0):
                    break


class App:
    MAGIC_BASE = 973451000

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Dual MT5 Bridge - Simultaneous Trading")
        self.root.geometry("980x560")

        # State
        self.worker1: Optional[WorkerClient] = None
        self.worker2: Optional[WorkerClient] = None
        self.connected1 = False
        self.connected2 = False
        self.trade_counter = 1
        self.paired_trades: Dict[str, Dict[str, Any]] = {}
        self._trade_lock = threading.Lock()

        self.persistence = Persistence(Path("automation_state.json"))
        self.config = self.persistence.get_config()
        self.automation_runner = AutomationRunner(self, self.persistence)

        # UI Vars
        self.terminal1_var = tk.StringVar(value=DEFAULT_TERMINAL_1)
        self.terminal2_var = tk.StringVar(value=DEFAULT_TERMINAL_2)
        self.pair1_var = tk.StringVar(value=self.config.primary.symbol1)
        self.lot1_var = tk.StringVar(value=str(self.config.primary.lot1))
        self.pair2_var = tk.StringVar(value=self.config.primary.symbol2)
        self.lot2_var = tk.StringVar(value=str(self.config.primary.lot2))

        self.market_timezone_var = tk.StringVar(value=self.config.timezone)
        self.primary_enabled_var = tk.BooleanVar(value=self.config.primary.enabled)
        self.primary_time_var = tk.StringVar(value=self.config.primary.time_str)
        self.primary_symbol1_var = tk.StringVar(value=self.config.primary.symbol1)
        self.primary_symbol2_var = tk.StringVar(value=self.config.primary.symbol2)
        self.primary_lot1_var = tk.StringVar(value=str(self.config.primary.lot1))
        self.primary_lot2_var = tk.StringVar(value=str(self.config.primary.lot2))
        self.primary_direction_var = tk.StringVar(value=self._direction_key_to_display(self.config.primary.direction))

        self.wed_enabled_var = tk.BooleanVar(value=self.config.wednesday.enabled)
        self.wed_time_var = tk.StringVar(value=self.config.wednesday.time_str)
        self.wed_symbol1_var = tk.StringVar(value=self.config.wednesday.symbol1)
        self.wed_symbol2_var = tk.StringVar(value=self.config.wednesday.symbol2)
        self.wed_lot1_var = tk.StringVar(value=str(self.config.wednesday.lot1))
        self.wed_lot2_var = tk.StringVar(value=str(self.config.wednesday.lot2))
        self.wed_direction_var = tk.StringVar(value=self._direction_key_to_display(self.config.wednesday.direction))

        self.close_after_var = tk.StringVar(value=str(self.config.risk.close_after_minutes))
        self.max_entry_spread_var = tk.StringVar(value=str(self.config.risk.max_entry_spread))
        self.max_exit_spread_var = tk.StringVar(value=str(self.config.risk.max_exit_spread))
        self.start_date_var = tk.StringVar(value=self.config.risk.start_date or "")
        self.end_date_var = tk.StringVar(value=self.config.risk.end_date or "")
        self.drawdown_enabled_var = tk.BooleanVar(value=self.config.risk.drawdown_enabled)
        self.drawdown_stop_var = tk.StringVar(value=str(self.config.risk.drawdown_stop))

        self._build_ui()
        self._schedule_profit_updates()

        self.automation_runner.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=8)

        # Terminal Paths
        ttk.Label(top, text="Terminal 1 Path").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.terminal1_var, width=60).grid(row=0, column=1, sticky="ew", **pad)
        self.status1 = ttk.Label(top, text="disconnected", foreground="#b00")
        self.status1.grid(row=0, column=2, sticky="w", **pad)

        ttk.Label(top, text="Terminal 2 Path").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.terminal2_var, width=60).grid(row=1, column=1, sticky="ew", **pad)
        self.status2 = ttk.Label(top, text="disconnected", foreground="#b00")
        self.status2.grid(row=1, column=2, sticky="w", **pad)

        connect_btn = ttk.Button(top, text="Connect Both", command=self._on_connect)
        connect_btn.grid(row=0, column=3, rowspan=2, sticky="nsew", **pad)

        # Trade inputs
        ttk.Label(top, text="Pair (Account 1)").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.pair1_var, width=16).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(top, text="Lot Size (Account 1)").grid(row=2, column=2, sticky="e", **pad)
        ttk.Entry(top, textvariable=self.lot1_var, width=12).grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(top, text="Pair (Account 2)").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.pair2_var, width=16).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(top, text="Lot Size (Account 2)").grid(row=3, column=2, sticky="e", **pad)
        ttk.Entry(top, textvariable=self.lot2_var, width=12).grid(row=3, column=3, sticky="w", **pad)

        # Action buttons
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=8)
        self.buy_btn = ttk.Button(actions, text="BUY (Simultaneous)", command=lambda: self._on_place("buy"), state="disabled")
        self.buy_btn.pack(side="left", padx=6, pady=4)
        self.sell_btn = ttk.Button(actions, text="SELL (Simultaneous)", command=lambda: self._on_place("sell"), state="disabled")
        self.sell_btn.pack(side="left", padx=6, pady=4)

        # Mixed direction buttons
        self.buy1_sell2_btn = ttk.Button(
            actions,
            text="BUY A1 / SELL A2",
            command=lambda: self._on_place_mixed("buy", "sell"),
            state="disabled",
        )
        self.buy1_sell2_btn.pack(side="left", padx=6, pady=4)

        self.sell1_buy2_btn = ttk.Button(
            actions,
            text="SELL A1 / BUY A2",
            command=lambda: self._on_place_mixed("sell", "buy"),
            state="disabled",
        )
        self.sell1_buy2_btn.pack(side="left", padx=6, pady=4)

        # Table
        self.table = ScrollableTable(
            self.root,
            columns=[
                "Trade ID",
                "Account 1: Pair",
                "Account 1: Lot",
                "Account 1: Entry Price",
                "Account 1: Entry Time",
                "Account 1: P/L",
                "Account 2: Pair",
                "Account 2: Lot",
                "Account 2: Entry Price",
                "Account 2: Entry Time",
                "Account 2: P/L",
                "Side (Buy/Sell)",
                "Combined Net Profit",
                "Close (both)",
            ],
        )
        self.table.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_automation_ui()

    def _build_automation_ui(self) -> None:
        automation = ttk.LabelFrame(self.root, text="Automation Settings")
        automation.pack(fill="x", padx=8, pady=(0, 10))
        automation.columnconfigure(1, weight=1)

        ttk.Label(automation, text="Market Timezone").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(automation, textvariable=self.market_timezone_var, width=24).grid(row=0, column=1, sticky="w", padx=6, pady=4)

        direction_options = [
            self._direction_key_to_display(key) for key in ("buy_sell", "sell_buy", "buy_buy", "sell_sell")
        ]

        primary_frame = ttk.LabelFrame(automation, text="Primary Trades")
        primary_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=4)
        for i in range(6):
            primary_frame.columnconfigure(i, weight=1 if i in (1, 3, 5) else 0)
        ttk.Checkbutton(primary_frame, text="Enable", variable=self.primary_enabled_var).grid(
            row=0, column=0, sticky="w", padx=4, pady=2
        )
        ttk.Label(primary_frame, text="Time (HH:MM)").grid(row=0, column=1, sticky="e", padx=4, pady=2)
        ttk.Entry(primary_frame, textvariable=self.primary_time_var, width=10).grid(
            row=0, column=2, sticky="w", padx=4, pady=2
        )
        ttk.Label(primary_frame, text="Symbol A1").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(primary_frame, textvariable=self.primary_symbol1_var, width=12).grid(
            row=1, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Label(primary_frame, text="Lot A1").grid(row=1, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(primary_frame, textvariable=self.primary_lot1_var, width=10).grid(
            row=1, column=3, sticky="w", padx=4, pady=2
        )
        ttk.Label(primary_frame, text="Symbol A2").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(primary_frame, textvariable=self.primary_symbol2_var, width=12).grid(
            row=2, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Label(primary_frame, text="Lot A2").grid(row=2, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(primary_frame, textvariable=self.primary_lot2_var, width=10).grid(
            row=2, column=3, sticky="w", padx=4, pady=2
        )
        ttk.Label(primary_frame, text="Direction").grid(row=3, column=0, sticky="e", padx=4, pady=2)
        ttk.Combobox(
            primary_frame,
            textvariable=self.primary_direction_var,
            values=direction_options,
            state="readonly",
            width=12,
        ).grid(row=3, column=1, sticky="w", padx=4, pady=2)

        wed_frame = ttk.LabelFrame(automation, text="Wednesday Specials")
        wed_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=4)
        for i in range(6):
            wed_frame.columnconfigure(i, weight=1 if i in (1, 3, 5) else 0)
        ttk.Checkbutton(wed_frame, text="Enable", variable=self.wed_enabled_var).grid(
            row=0, column=0, sticky="w", padx=4, pady=2
        )
        ttk.Label(wed_frame, text="Time (HH:MM)").grid(row=0, column=1, sticky="e", padx=4, pady=2)
        ttk.Entry(wed_frame, textvariable=self.wed_time_var, width=10).grid(
            row=0, column=2, sticky="w", padx=4, pady=2
        )
        ttk.Label(wed_frame, text="Symbol A1").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(wed_frame, textvariable=self.wed_symbol1_var, width=12).grid(
            row=1, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Label(wed_frame, text="Lot A1").grid(row=1, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(wed_frame, textvariable=self.wed_lot1_var, width=10).grid(
            row=1, column=3, sticky="w", padx=4, pady=2
        )
        ttk.Label(wed_frame, text="Symbol A2").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(wed_frame, textvariable=self.wed_symbol2_var, width=12).grid(
            row=2, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Label(wed_frame, text="Lot A2").grid(row=2, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(wed_frame, textvariable=self.wed_lot2_var, width=10).grid(
            row=2, column=3, sticky="w", padx=4, pady=2
        )
        ttk.Label(wed_frame, text="Direction").grid(row=3, column=0, sticky="e", padx=4, pady=2)
        ttk.Combobox(
            wed_frame,
            textvariable=self.wed_direction_var,
            values=direction_options,
            state="readonly",
            width=12,
        ).grid(row=3, column=1, sticky="w", padx=4, pady=2)

        risk_frame = ttk.LabelFrame(automation, text="Risk Controls")
        risk_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=6, pady=4)
        for i in range(4):
            risk_frame.columnconfigure(i, weight=1 if i % 2 == 1 else 0)
        ttk.Label(risk_frame, text="Close After (min)").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(risk_frame, textvariable=self.close_after_var, width=10).grid(
            row=0, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Label(risk_frame, text="Max Entry Spread").grid(row=0, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(risk_frame, textvariable=self.max_entry_spread_var, width=10).grid(
            row=0, column=3, sticky="w", padx=4, pady=2
        )
        ttk.Label(risk_frame, text="Max Exit Spread").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(risk_frame, textvariable=self.max_exit_spread_var, width=10).grid(
            row=1, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Label(risk_frame, text="Start Date (YYYY-MM-DD)").grid(
            row=1, column=2, sticky="e", padx=4, pady=2
        )
        ttk.Entry(risk_frame, textvariable=self.start_date_var, width=12).grid(
            row=1, column=3, sticky="w", padx=4, pady=2
        )
        ttk.Label(risk_frame, text="End Date (YYYY-MM-DD)").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(risk_frame, textvariable=self.end_date_var, width=12).grid(
            row=2, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Checkbutton(risk_frame, text="Enable Drawdown Stop", variable=self.drawdown_enabled_var).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4, pady=2
        )
        ttk.Label(risk_frame, text="Drawdown Stop (%)").grid(row=2, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(risk_frame, textvariable=self.drawdown_stop_var, width=8).grid(
            row=2, column=3, sticky="w", padx=4, pady=2
        )

        save_frame = ttk.Frame(automation)
        save_frame.grid(row=4, column=0, columnspan=2, sticky="ew", padx=4, pady=6)
        save_frame.columnconfigure(0, weight=1)
        self.automation_status = ttk.Label(save_frame, text="", foreground="#555")
        self.automation_status.grid(row=0, column=0, sticky="w", padx=4)
        ttk.Button(save_frame, text="Save Automation Settings", command=self._save_config).grid(
            row=0, column=1, sticky="e", padx=4
        )
        self.automation_status.configure(text="Loaded saved automation settings.")

    @staticmethod
    def _direction_key_to_display(key: str) -> str:
        key = (key or "buy_sell").lower()
        return key.replace("_", "/").upper()

    @staticmethod
    def _direction_display_to_key(value: str) -> str:
        value = (value or "BUY/SELL").lower()
        return value.replace("/", "_")

    @staticmethod
    def _direction_key_to_sides(key: str) -> Sequence[str]:
        mapping = {
            "buy_sell": ("buy", "sell"),
            "sell_buy": ("sell", "buy"),
            "buy_buy": ("buy", "buy"),
            "sell_sell": ("sell", "sell"),
        }
        return mapping.get((key or "buy_sell").lower(), ("buy", "sell"))

    def _collect_config_from_ui(self) -> Optional[AppConfig]:
        try:
            primary = ScheduleModel(
                name="primary",
                enabled=bool(self.primary_enabled_var.get()),
                time_str=self.primary_time_var.get().strip() or "09:00",
                symbol1=self.primary_symbol1_var.get().strip(),
                symbol2=self.primary_symbol2_var.get().strip(),
                lot1=float(self.primary_lot1_var.get().strip() or 0.0),
                lot2=float(self.primary_lot2_var.get().strip() or 0.0),
                direction=self._direction_display_to_key(self.primary_direction_var.get()),
                weekdays=[0, 1, 2, 3, 4],
            )
            wednesday = ScheduleModel(
                name="wednesday",
                enabled=bool(self.wed_enabled_var.get()),
                time_str=self.wed_time_var.get().strip() or "09:00",
                symbol1=self.wed_symbol1_var.get().strip(),
                symbol2=self.wed_symbol2_var.get().strip(),
                lot1=float(self.wed_lot1_var.get().strip() or 0.0),
                lot2=float(self.wed_lot2_var.get().strip() or 0.0),
                direction=self._direction_display_to_key(self.wed_direction_var.get()),
                weekdays=[2],
            )
            risk = RiskConfig(
                close_after_minutes=int(float(self.close_after_var.get().strip() or 0)),
                max_entry_spread=float(self.max_entry_spread_var.get().strip() or 0.0),
                max_exit_spread=float(self.max_exit_spread_var.get().strip() or 0.0),
                start_date=self.start_date_var.get().strip() or None,
                end_date=self.end_date_var.get().strip() or None,
                drawdown_enabled=bool(self.drawdown_enabled_var.get()),
                drawdown_stop=float(self.drawdown_stop_var.get().strip() or 0.0),
            )
        except ValueError as exc:
            self._set_automation_status(f"Invalid automation settings: {exc}", ok=False)
            return None

        tz = self.market_timezone_var.get().strip() or "UTC"
        config = AppConfig(timezone=tz, primary=primary, wednesday=wednesday, risk=risk)
        return config

    def _save_config(self) -> None:
        config = self._collect_config_from_ui()
        if not config:
            return
        self.config = config
        self.persistence.save_config(config)
        self._set_automation_status("Automation settings saved.", ok=True)
        # Refresh manual entry defaults
        self.pair1_var.set(config.primary.symbol1)
        self.lot1_var.set(str(config.primary.lot1))
        self.pair2_var.set(config.primary.symbol2)
        self.lot2_var.set(str(config.primary.lot2))

    def _set_automation_status(self, message: str, ok: bool = True) -> None:
        color = "#070" if ok else "#b00"
        self._invoke_on_ui(lambda: self.automation_status.configure(text=message, foreground=color))

    def _invoke_on_ui(self, func) -> None:
        try:
            self.root.after(0, func)
        except Exception:
            try:
                func()
            except Exception:
                pass

    def _execute_schedule_trade(self, schedule: ScheduleModel) -> None:
        sides = self._direction_key_to_sides(schedule.direction)
        try:
            symbol1 = schedule.symbol1 or self.pair1_var.get().strip()
            symbol2 = schedule.symbol2 or self.pair2_var.get().strip()
            lot1 = float(schedule.lot1)
            lot2 = float(schedule.lot2)
            self._open_trade_pair(symbol1, lot1, sides[0], symbol2, lot2, sides[1], schedule_name=schedule.name)
            self._set_automation_status(f"Scheduled trade executed for {schedule.name}.", ok=True)
        except Exception as exc:
            self._set_automation_status(f"Failed to execute {schedule.name}: {exc}", ok=False)

    @staticmethod
    def _fmt_time(ts: int) -> str:
        if not ts:
            return ""
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
        except Exception:
            return str(ts)

    def _open_trade_pair(
        self,
        symbol1: str,
        lot1: float,
        side1: str,
        symbol2: str,
        lot2: float,
        side2: str,
        schedule_name: Optional[str] = None,
    ) -> str:
        if not (self.connected1 and self.connected2 and self.worker1 and self.worker2):
            raise RuntimeError("Connect both terminals first.")
        if not symbol1 or not symbol2:
            raise ValueError("Symbols required")
        if lot1 <= 0 or lot2 <= 0:
            raise ValueError("Lot sizes must be positive")

        trade_id = f"T{self.trade_counter:05d}"
        self.trade_counter += 1
        magic1 = self.MAGIC_BASE + 1
        magic2 = self.MAGIC_BASE + 2

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(
                self.worker1.buy if side1.lower() == "buy" else self.worker1.sell,
                symbol1,
                float(lot1),
                trade_id,
                magic1,
            )
            f2 = ex.submit(
                self.worker2.buy if side2.lower() == "buy" else self.worker2.sell,
                symbol2,
                float(lot2),
                trade_id,
                magic2,
            )
            r1 = f1.result(timeout=20)
            r2 = f2.result(timeout=20)

        pos1 = int(r1.get("position_ticket", 0))
        pos2 = int(r2.get("position_ticket", 0))
        if pos1 <= 0 or pos2 <= 0:
            raise RuntimeError("Failed to obtain position tickets for both accounts.")

        entry = {
            "account1": {"symbol": symbol1, "lot": float(lot1), "side": side1, "position": pos1, "magic": magic1},
            "account2": {"symbol": symbol2, "lot": float(lot2), "side": side2, "position": pos2, "magic": magic2},
            "schedule": schedule_name or "manual",
            "opened_at": time.time(),
        }
        with self._trade_lock:
            self.paired_trades[trade_id] = entry

        side_label = f"{side1.upper()}/{side2.upper()}" if side1.lower() != side2.lower() else side1.upper()
        eprice1 = r1.get("entry_price")
        eprice2 = r2.get("entry_price")
        etime1 = r1.get("entry_time") or 0
        etime2 = r2.get("entry_time") or 0

        self.table.add_row(
            trade_id,
            [
                trade_id,
                symbol1,
                lot1,
                f"{eprice1:.5f}" if isinstance(eprice1, (int, float)) else "",
                self._fmt_time(int(etime1)),
                "0.00",
                symbol2,
                lot2,
                f"{eprice2:.5f}" if isinstance(eprice2, (int, float)) else "",
                self._fmt_time(int(etime2)),
                "0.00",
                side_label,
                "0.00",
                "Close",
            ],
            dynamic_indices={"p1": 5, "p2": 10, "combined": 12},
            close_callback=self._on_close_pair,
        )
        return trade_id

    def _fetch_spreads(self, requests: Sequence[tuple[Optional[WorkerClient], str]]) -> Dict[str, float]:
        spreads: Dict[str, float] = {}
        seen: set[str] = set()
        for worker, symbol in requests:
            symbol = (symbol or "").strip()
            if not symbol or symbol in seen or worker is None:
                continue
            try:
                quote = worker.get_quote(symbol)
                spreads[symbol] = float(quote.get("spread", 0.0))
                seen.add(symbol)
            except Exception:
                continue
        return spreads

    def _gather_active_trades(self, now: datetime) -> tuple[list[TrackedTrade], list[tuple[Optional[WorkerClient], str]]]:
        trades: list[TrackedTrade] = []
        requests: list[tuple[Optional[WorkerClient], str]] = []
        with self._trade_lock:
            for trade_id, info in self.paired_trades.items():
                opened_ts = float(info.get("opened_at", time.time()))
                try:
                    opened_dt = datetime.fromtimestamp(opened_ts, tz=now.tzinfo)
                except Exception:
                    opened_dt = datetime.utcfromtimestamp(opened_ts).replace(tzinfo=now.tzinfo)
                symbols: list[str] = []
                account1 = info.get("account1", {})
                account2 = info.get("account2", {})
                sym1 = account1.get("symbol")
                sym2 = account2.get("symbol")
                if sym1:
                    symbols.append(sym1)
                    requests.append((self.worker1, sym1))
                if sym2:
                    symbols.append(sym2)
                    requests.append((self.worker2, sym2))
                trades.append(TrackedTrade(trade_id, opened_dt, tuple(symbols)))
        return trades, requests

    def _close_pair_threadsafe(self, trade_id: str) -> None:
        self._invoke_on_ui(lambda tid=trade_id: self._on_close_pair(tid))

    def _close_all_pairs_threadsafe(self) -> None:
        self._invoke_on_ui(self._close_all_pairs)

    def _close_all_pairs(self) -> None:
        with self._trade_lock:
            trade_ids = list(self.paired_trades.keys())
        for trade_id in trade_ids:
            self._on_close_pair(trade_id)

    def _fetch_accounts(self) -> list[Dict[str, float]]:
        accounts: list[Dict[str, float]] = []
        for worker in (self.worker1, self.worker2):
            if worker is None:
                continue
            try:
                accounts.append(worker.get_account_info())
            except Exception:
                continue
        return accounts

    def evaluate_automation(self, now: datetime, config: AppConfig, state: AutomationState) -> bool:
        changed = False
        connected = bool(self.worker1 and self.worker2 and self.connected1 and self.connected2)

        if connected:
            for schedule in (config.primary, config.wednesday):
                if not schedule_should_trigger(schedule, now, config.risk, state):
                    continue
                symbols = [s for s in (schedule.symbol1, schedule.symbol2) if s]
                requests = []
                if schedule.symbol1:
                    requests.append((self.worker1, schedule.symbol1))
                if schedule.symbol2:
                    requests.append((self.worker2, schedule.symbol2))
                spreads = self._fetch_spreads(requests)
                if not spreads_within_entry_limit(symbols, spreads, config.risk):
                    self._set_automation_status(
                        f"{schedule.name.title()} skipped due to spread limit.", ok=False
                    )
                    continue
                self._invoke_on_ui(lambda sch=schedule: self._execute_schedule_trade(sch))
                mark_schedule_triggered(state, schedule, now)
                changed = True

        trades, requests = self._gather_active_trades(now)
        if trades and connected:
            spreads = self._fetch_spreads(requests)
            due_close = trades_due_for_close(trades, now, config.risk, spreads)
            if due_close:
                self._set_automation_status(
                    f"Auto-close triggered for {len(due_close)} trade(s).", ok=False
                )
            for trade_id in due_close:
                self._close_pair_threadsafe(trade_id)

        if connected:
            accounts = self._fetch_accounts()
            if accounts and drawdown_breached(config.risk, accounts):
                if trades:
                    self._set_automation_status("Drawdown stop triggered. Closing all trades.", ok=False)
                self._close_all_pairs_threadsafe()

        return changed

    def _on_connect(self) -> None:
        path1 = self.terminal1_var.get().strip()
        path2 = self.terminal2_var.get().strip()
        if not path1 or not path2:
            messagebox.showerror("Error", "Please provide both terminal paths.")
            return

        try:
            self.worker1 = WorkerClient("A1", path1)
            self.worker2 = WorkerClient("A2", path2)
            # Connect in parallel
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(self.worker1.connect, path1)
                f2 = ex.submit(self.worker2.connect, path2)
                d1 = f1.result(timeout=25)
                d2 = f2.result(timeout=25)
            self.connected1 = True
            self.connected2 = True
            self.status1.configure(text="connected", foreground="#070")
            self.status2.configure(text="connected", foreground="#070")
            self.buy_btn.configure(state="normal")
            self.sell_btn.configure(state="normal")
            self.buy1_sell2_btn.configure(state="normal")
            self.sell1_buy2_btn.configure(state="normal")
        except Exception as e:
            messagebox.showerror("Connection Failed", str(e))
            self._cleanup_workers()

    def _on_place(self, side: str) -> None:
        # Backwards-compatible: same side on both accounts
        return self._on_place_mixed(side, side)

    def _on_place_mixed(self, side1: str, side2: str) -> None:
        symbol1 = self.pair1_var.get().strip()
        symbol2 = self.pair2_var.get().strip()
        try:
            lot1 = float(self.lot1_var.get().strip())
            lot2 = float(self.lot2_var.get().strip())
        except Exception:
            messagebox.showerror("Error", "Invalid lot sizes.")
            return
        try:
            self._open_trade_pair(symbol1, lot1, side1, symbol2, lot2, side2)
        except Exception as e:
            messagebox.showerror("Trade Error", str(e))

    def _on_close_pair(self, trade_id: str) -> None:
        with self._trade_lock:
            info = self.paired_trades.get(trade_id)
        if not info:
            return
        a1 = info["account1"]
        a2 = info["account2"]

        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(self.worker1.close, a1["position"], a1["symbol"], a1["side"], a1["lot"], a1["magic"])
                f2 = ex.submit(self.worker2.close, a2["position"], a2["symbol"], a2["side"], a2["lot"], a2["magic"])
                f1.result(timeout=20)
                f2.result(timeout=20)
            # Remove UI row and internal state
            self.table.remove_row(trade_id)
            with self._trade_lock:
                self.paired_trades.pop(trade_id, None)
        except Exception as e:
            messagebox.showerror("Close Error", str(e))

    def _schedule_profit_updates(self) -> None:
        self.root.after(800, self._update_profits)

    def _update_profits(self) -> None:
        try:
            with self._trade_lock:
                snapshot = {tid: dict(info) for tid, info in self.paired_trades.items()}
            for trade_id, info in snapshot.items():
                a1 = info["account1"]
                a2 = info["account2"]
                try:
                    p1 = self.worker1.get_profit(a1["position"]) if self.worker1 else {"open": False, "profit": 0.0}
                    p2 = self.worker2.get_profit(a2["position"]) if self.worker2 else {"open": False, "profit": 0.0}
                except Exception:
                    p1 = {"open": False, "profit": 0.0}
                    p2 = {"open": False, "profit": 0.0}

                total = float(p1.get("profit", 0.0)) + float(p2.get("profit", 0.0))
                self.table.set_profits(
                    trade_id,
                    float(p1.get("profit", 0.0)),
                    float(p2.get("profit", 0.0)),
                    total,
                )

                # If both closed externally, remove row
                if not p1.get("open") and not p2.get("open"):
                    self.table.remove_row(trade_id)
                    with self._trade_lock:
                        self.paired_trades.pop(trade_id, None)
        finally:
            self._schedule_profit_updates()

    def _cleanup_workers(self) -> None:
        for w in (self.worker1, self.worker2):
            if w is not None:
                try:
                    w.shutdown()
                except Exception:
                    pass
        self.worker1 = None
        self.worker2 = None
        self.connected1 = False
        self.connected2 = False
        self.buy_btn.configure(state="disabled")
        self.sell_btn.configure(state="disabled")
        self.status1.configure(text="disconnected", foreground="#b00")
        self.status2.configure(text="disconnected", foreground="#b00")

    def on_close(self) -> None:
        self.automation_runner.stop()
        self._cleanup_workers()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    # Windows spawn safety
    from multiprocessing import freeze_support

    freeze_support()
    main()


