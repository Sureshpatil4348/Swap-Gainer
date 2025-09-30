import csv
import os
import copy
import re
import sys
import uuid
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, Any, Optional, Sequence, Union
from zoneinfo import ZoneInfo

import tkinter as tk
from tkinter import ttk, messagebox

from multiprocessing import get_context
from mt5_worker import worker_main

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
        self.scroll_x = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll_y.set, xscrollcommand=self.scroll_x.set)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)
        self.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)
        self.inner.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scroll_y.grid(row=0, column=1, sticky="ns")
        self.scroll_x.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # Header
        for c, col in enumerate(columns):
            lbl = ttk.Label(self.inner, text=col, font=("Segoe UI", 9, "bold"))
            lbl.grid(row=0, column=c, sticky="nsew", padx=4, pady=(2, 6))
            self.inner.columnconfigure(c, weight=1)

        self._next_row = 1
        self._rows: Dict[str, Dict[str, Any]] = {}

    def _on_shift_mousewheel(self, event: tk.Event) -> str:
        delta = getattr(event, "delta", 0)
        if delta:
            self.canvas.xview_scroll(int(-1 * (delta / 120)), "units")
        return "break"

    def add_row(
        self,
        row_id: str,
        values: list[Any],
        dynamic_fields: Dict[str, int],
        close_callback,
    ) -> None:
        widgets = []
        dynamic_labels: Dict[str, ttk.Label] = {}
        index_to_key = {idx: key for key, idx in dynamic_fields.items()}

        for c, val in enumerate(values[:-1]):  # except last column (Close button)
            if c in index_to_key:
                lbl = ttk.Label(self.inner, text=str(val))
                lbl.grid(row=self._next_row, column=c, sticky="nsew", padx=4, pady=2)
                lbl.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)
                dynamic_labels[index_to_key[c]] = lbl
                widgets.append(lbl)
            else:
                w = ttk.Label(self.inner, text=str(val))
                w.grid(row=self._next_row, column=c, sticky="nsew", padx=4, pady=2)
                w.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)
                widgets.append(w)

        # Close button
        btn = ttk.Button(self.inner, text="Close", command=lambda: close_callback(row_id))
        btn.grid(row=self._next_row, column=len(values) - 1, sticky="nsew", padx=4, pady=2)
        btn.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)

        self._rows[row_id] = {
            "widgets": widgets,
            "dynamic_labels": dynamic_labels,
            "button": btn,
            "row_index": self._next_row,
        }
        self._next_row += 1

    def set_metrics(self, row_id: str, metrics: Dict[str, float]) -> None:
        row = self._rows.get(row_id)
        if not row:
            return
        dynamic_labels: Dict[str, ttk.Label] = row.get("dynamic_labels", {}) or {}
        for key, value in metrics.items():
            label = dynamic_labels.get(key)
            if label is not None:
                try:
                    label.configure(text=f"{float(value):.2f}")
                except Exception:
                    label.configure(text=str(value))

    def remove_row(self, row_id: str) -> None:
        row = self._rows.pop(row_id, None)
        if not row:
            return
        for w in row.get("widgets", []):
            w.destroy()
        for lbl in row.get("dynamic_labels", {}).values():
            try:
                lbl.destroy()
            except Exception:
                pass
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
                    state = self.app.update_state_snapshot(state)
                    self.persistence.save_state(state)
                    self.app.on_state_updated(state)
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

        self.persistence = Persistence(Path("automation_state.json"), Path("automation_config.json"))
        self.config = self.persistence.get_config()
        self.state = self.persistence.get_state()
        self.trade_history: list[Dict[str, Any]] = []
        self.trade_history_limit = 250
        self.history_csv_path = Path("trade_history.csv")
        self._history_export_lock = threading.Lock()
        for entry in getattr(self.state, "trade_history", []):
            if isinstance(entry, dict):
                self.trade_history.append(dict(entry))
        if len(self.trade_history) > self.trade_history_limit:
            self.trade_history = self.trade_history[-self.trade_history_limit:]
        self.automation_runner = AutomationRunner(self, self.persistence)

        # UI Vars
        self.terminal1_var = tk.StringVar(value=DEFAULT_TERMINAL_1)
        self.terminal2_var = tk.StringVar(value=DEFAULT_TERMINAL_2)
        primary_default = self.config.primary_threads[0] if self.config.primary_threads else _default_primary_threads()[0]
        self.pair1_var = tk.StringVar(value=primary_default.symbol1)
        self.lot1_var = tk.StringVar(value=str(primary_default.lot1))
        self.pair2_var = tk.StringVar(value=primary_default.symbol2)
        self.lot2_var = tk.StringVar(value=str(primary_default.lot2))
        self.account1_balance_var = tk.StringVar(value="Balance: --")
        self.account1_equity_var = tk.StringVar(value="Equity: --")
        self.account2_balance_var = tk.StringVar(value="Balance: --")
        self.account2_equity_var = tk.StringVar(value="Equity: --")

        self.automation_status_label = None
        self.schedule_tree = None
        self.config_tree = None
        self.trade_history_tree = None
        self._scroll_canvas = None
        self._scrollable_body = None

        self._build_ui()
        self._restore_active_trades()
        self._restore_trade_counter()
        self._refresh_schedule_overview(self.state)
        self._populate_trade_history_tree()
        self._schedule_profit_updates()

        self.automation_runner.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        top = ttk.LabelFrame(self.root, text="Connections")
        top.pack(fill="x", padx=12, pady=(12, 6))
        for col in range(4):
            top.columnconfigure(col, weight=1 if col == 1 else 0)

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

        self.automation_status_label = ttk.Label(top, text="", foreground="#555")
        self.automation_status_label.grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        account_summary = ttk.Frame(top)
        account_summary.grid(row=3, column=0, columnspan=4, sticky="ew", padx=6, pady=(0, 4))
        account_summary.columnconfigure(1, weight=1)
        account_summary.columnconfigure(4, weight=1)

        ttk.Label(account_summary, text="Account 1").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Label(account_summary, textvariable=self.account1_balance_var).grid(
            row=0, column=1, sticky="w", padx=(8, 0), pady=2
        )
        ttk.Label(account_summary, textvariable=self.account1_equity_var).grid(
            row=0, column=2, sticky="w", padx=(8, 0), pady=2
        )

        ttk.Label(account_summary, text="Account 2").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Label(account_summary, textvariable=self.account2_balance_var).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=2
        )
        ttk.Label(account_summary, textvariable=self.account2_equity_var).grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=2
        )

        body_container = ttk.Frame(self.root)
        body_container.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        canvas = tk.Canvas(body_container, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body_container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollable_body = ttk.Frame(canvas)
        scrollable_body.bind(
            "<Configure>",
            lambda event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=scrollable_body, anchor="nw")

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._scroll_canvas = canvas
        self._scrollable_body = scrollable_body

        def _on_mousewheel(event):
            delta = getattr(event, 'delta', 0)
            if delta:
                canvas.yview_scroll(int(-1 * (delta / 120)), "units")

        def _on_shift_mousewheel(event):
            delta = getattr(event, 'delta', 0)
            if delta:
                canvas.xview_scroll(int(-1 * (delta / 120)), "units")

        def _bind_to_mousewheel(widget):
            widget.bind("<Enter>", lambda _: (canvas.bind_all("<MouseWheel>", _on_mousewheel), canvas.bind_all("<Shift-MouseWheel>", _on_shift_mousewheel)))
            widget.bind("<Leave>", lambda _: (canvas.unbind_all("<MouseWheel>"), canvas.unbind_all("<Shift-MouseWheel>")))

        def _bind_horizontal_mousewheel(widget, xview_command):
            def _on_shift(event):
                delta = getattr(event, "delta", 0)
                if delta:
                    xview_command(int(-1 * (delta / 120)), "units")
                return "break"

            widget.bind("<Shift-MouseWheel>", _on_shift, add="+")

        _bind_to_mousewheel(scrollable_body)

        scrollable_body.columnconfigure(0, weight=1)
        scrollable_body.columnconfigure(1, weight=1)
        for row in range(3):
            scrollable_body.rowconfigure(row, weight=1 if row != 1 else 0)

        active_trades = ttk.LabelFrame(scrollable_body, text="Active Trades")
        active_trades.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        active_trades.columnconfigure(0, weight=1)
        active_trades.rowconfigure(0, weight=1)

        self.table = ScrollableTable(
            active_trades,
            columns=[
                "Trade ID",
                "Account 1: Pair",
                "Account 1: Lot",
                "Account 1: Entry Price",
                "Account 1: Entry Time",
                "Account 1: Commission",
                "Account 1: Swap",
                "Account 1: P/L",
                "Account 2: Pair",
                "Account 2: Lot",
                "Account 2: Entry Price",
                "Account 2: Entry Time",
                "Account 2: Commission",
                "Account 2: Swap",
                "Account 2: P/L",
                "Side (Buy/Sell)",
                "Combined Commission",
                "Combined Swap",
                "Combined Net Profit",
                "Close (both)",
            ],
        )
        self.table.grid(row=0, column=0, sticky="nsew")
        _bind_horizontal_mousewheel(self.table, self.table.canvas.xview_scroll)

        drives_frame = ttk.LabelFrame(scrollable_body, text="Active Drives")
        drives_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 12))
        drives_frame.columnconfigure(0, weight=1)
        drives_frame.rowconfigure(0, weight=1)
        drives_frame.rowconfigure(1, weight=0)

        schedule_columns = (
            "schedule",
            "status",
            "pairs",
            "lots",
            "direction",
            "window",
            "days",
            "next",
            "last",
        )
        self.schedule_tree = ttk.Treeview(
            drives_frame,
            columns=schedule_columns,
            show="headings",
            height=10,
        )
        headings = {
            "schedule": "Schedule",
            "status": "Status",
            "pairs": "Pairs",
            "lots": "Lots",
            "direction": "Direction",
            "window": "Entry Window",
            "days": "Days",
            "next": "Next Run",
            "last": "Last Run",
        }
        for col in schedule_columns:
            self.schedule_tree.heading(col, text=headings[col])
            width = 140
            if col == "pairs":
                width = 170
            elif col == "window":
                width = 160
            elif col == "schedule":
                width = 190
            elif col == "days":
                width = 110
            self.schedule_tree.column(col, width=width, stretch=col in {"schedule", "pairs", "window"})

        schedule_scroll = ttk.Scrollbar(drives_frame, orient="vertical", command=self.schedule_tree.yview)
        schedule_scroll_x = ttk.Scrollbar(drives_frame, orient="horizontal", command=self.schedule_tree.xview)
        self.schedule_tree.configure(yscrollcommand=schedule_scroll.set, xscrollcommand=schedule_scroll_x.set)
        self.schedule_tree.grid(row=0, column=0, sticky="nsew")
        schedule_scroll.grid(row=0, column=1, sticky="ns")
        schedule_scroll_x.grid(row=1, column=0, columnspan=2, sticky="ew")
        _bind_horizontal_mousewheel(self.schedule_tree, self.schedule_tree.xview_scroll)

        config_frame = ttk.LabelFrame(scrollable_body, text="Configuration Snapshot")
        config_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 12))
        config_frame.columnconfigure(0, weight=1)
        config_frame.rowconfigure(0, weight=1)
        config_frame.rowconfigure(1, weight=0)

        self.config_tree = ttk.Treeview(
            config_frame,
            columns=("value",),
            show="tree headings",
            selectmode="browse",
            height=10,
        )
        self.config_tree.heading("#0", text="Item", anchor="w")
        self.config_tree.heading("value", text="Details", anchor="w")
        self.config_tree.column("#0", width=220, stretch=True)
        self.config_tree.column("value", width=260, stretch=True)

        config_scroll = ttk.Scrollbar(config_frame, orient="vertical", command=self.config_tree.yview)
        config_scroll_x = ttk.Scrollbar(config_frame, orient="horizontal", command=self.config_tree.xview)
        self.config_tree.configure(yscrollcommand=config_scroll.set, xscrollcommand=config_scroll_x.set)
        self.config_tree.grid(row=0, column=0, sticky="nsew")
        config_scroll.grid(row=0, column=1, sticky="ns")
        config_scroll_x.grid(row=1, column=0, columnspan=2, sticky="ew")
        _bind_horizontal_mousewheel(self.config_tree, self.config_tree.xview_scroll)

        config_actions = ttk.Frame(config_frame)
        config_actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        config_actions.columnconfigure(0, weight=1)
        ttk.Button(
            config_actions,
            text="Reload Configuration",
            command=self._reload_config_from_disk,
        ).grid(row=0, column=1, sticky="e", padx=(4, 0))

        history_frame = ttk.LabelFrame(scrollable_body, text="Trade History")
        history_frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(0, weight=1)
        history_frame.rowconfigure(1, weight=0)

        history_columns = (
            "trade_id",
            "schedule",
            "opened",
            "closed",
            "p1",
            "p1_commission",
            "p1_swap",
            "p2",
            "p2_commission",
            "p2_swap",
            "combined_commission",
            "combined_swap",
            "combined",
        )
        self.trade_history_tree = ttk.Treeview(
            history_frame,
            columns=history_columns,
            show="headings",
            height=12,
        )
        history_headings = {
            "trade_id": "Trade ID",
            "schedule": "Schedule",
            "opened": "Opened At",
            "closed": "Closed At",
            "p1": "Account 1 P/L",
            "p1_commission": "Account 1 Commission",
            "p1_swap": "Account 1 Swap",
            "p2": "Account 2 P/L",
            "p2_commission": "Account 2 Commission",
            "p2_swap": "Account 2 Swap",
            "combined_commission": "Combined Commission",
            "combined_swap": "Combined Swap",
            "combined": "Combined P/L",
        }
        for col in history_columns:
            self.trade_history_tree.heading(col, text=history_headings[col])
            width = 130
            if col == "schedule":
                width = 200
            elif col in {"combined", "combined_commission", "combined_swap"}:
                width = 150
            self.trade_history_tree.column(col, width=width, stretch=col in {"schedule", "combined"})

        history_scroll = ttk.Scrollbar(history_frame, orient="vertical", command=self.trade_history_tree.yview)
        history_scroll_x = ttk.Scrollbar(history_frame, orient="horizontal", command=self.trade_history_tree.xview)
        self.trade_history_tree.configure(yscrollcommand=history_scroll.set, xscrollcommand=history_scroll_x.set)
        self.trade_history_tree.grid(row=0, column=0, sticky="nsew")
        history_scroll.grid(row=0, column=1, sticky="ns")
        history_scroll_x.grid(row=1, column=0, columnspan=2, sticky="ew")
        _bind_horizontal_mousewheel(self.trade_history_tree, self.trade_history_tree.xview_scroll)

        _bind_to_mousewheel(self.trade_history_tree)
        _bind_to_mousewheel(self.schedule_tree)
        _bind_to_mousewheel(self.config_tree)
        _bind_to_mousewheel(self.table)

        self._update_config_summary()

    def _populate_trade_history_tree(self) -> None:
        if not self.trade_history_tree:
            return

        def _fmt_profit(value) -> str:
            return f"{float(value):.2f}"

        rows = []
        for entry in self.trade_history:
            if not isinstance(entry, dict):
                continue
            trade_id = str(entry.get('trade_id', ''))
            schedule = str(entry.get('schedule', '')) or 'Manual'
            opened_at = self._fmt_time(int(float(entry.get('opened_at', 0)) or 0))
            closed_at = self._fmt_time(int(float(entry.get('closed_at', 0)) or 0))
            account1 = entry.get('account1', {}) if isinstance(entry.get('account1'), dict) else {}
            account2 = entry.get('account2', {}) if isinstance(entry.get('account2'), dict) else {}
            p1 = float(account1.get('profit', 0.0) or 0.0)
            p1_commission = float(account1.get('commission', 0.0) or 0.0)
            p1_swap = float(account1.get('swap', 0.0) or 0.0)
            p2 = float(account2.get('profit', 0.0) or 0.0)
            p2_commission = float(account2.get('commission', 0.0) or 0.0)
            p2_swap = float(account2.get('swap', 0.0) or 0.0)
            combined = float(entry.get('realized_combined_pnl', entry.get('combined_profit', p1 + p2)) or 0.0)
            combined_commission = float(entry.get('combined_commission', p1_commission + p2_commission) or 0.0)
            combined_swap = float(entry.get('combined_swap', p1_swap + p2_swap) or 0.0)
            rows.append(
                (
                    trade_id,
                    schedule,
                    opened_at,
                    closed_at,
                    _fmt_profit(p1),
                    _fmt_profit(p1_commission),
                    _fmt_profit(p1_swap),
                    _fmt_profit(p2),
                    _fmt_profit(p2_commission),
                    _fmt_profit(p2_swap),
                    _fmt_profit(combined_commission),
                    _fmt_profit(combined_swap),
                    _fmt_profit(combined),
                )
            )

        def _update() -> None:
            tree = self.trade_history_tree
            tree.delete(*tree.get_children())
            for values in reversed(rows):
                tree.insert('', 0, values=values)

        self._invoke_on_ui(_update)

    def _reload_config_from_disk(self) -> None:
        try:
            config = self.persistence.get_config()
        except Exception as exc:
            messagebox.showerror('Reload Failed', str(exc))
            return
        self.config = config
        primary_default = config.primary_threads[0] if config.primary_threads else _default_primary_threads()[0]
        self.pair1_var.set(primary_default.symbol1)
        self.lot1_var.set(self._format_number(primary_default.lot1))
        self.pair2_var.set(primary_default.symbol2)
        self.lot2_var.set(self._format_number(primary_default.lot2))
        self._update_config_summary()
        self._refresh_schedule_overview(self.state)
        self._set_automation_status('Configuration reloaded from automation_config.json.', ok=True)

    def _update_config_summary(self) -> None:
        if not self.config_tree:
            return

        def _add_thread(parent, thread) -> None:
            status = 'ENABLED' if thread.enabled else 'Disabled'
            node = self.config_tree.insert(parent, 'end', text=f"{thread.name} ({thread.thread_id})", values=(status,), open=False)
            self.config_tree.insert(node, 'end', text='Pairs', values=(f"{thread.symbol1 or '-'} / {thread.symbol2 or '-'}",))
            self.config_tree.insert(node, 'end', text='Lots', values=(f"{self._format_number(thread.lot1)} / {self._format_number(thread.lot2)}",))
            self.config_tree.insert(node, 'end', text='Direction', values=(self._direction_key_to_display(thread.direction),))
            self.config_tree.insert(node, 'end', text='Entry Window', values=(self._format_entry_window(thread),))
            self.config_tree.insert(node, 'end', text='Weekdays', values=(self._format_weekdays(thread.weekdays),))
            self.config_tree.insert(node, 'end', text='Max Entry Spread', values=(self._format_number(thread.max_entry_spread),))
            close_after = self._hours_from_minutes(thread.close_after_minutes)
            close_text = f"{close_after} h" if close_after != '0' else 'n/a'
            self.config_tree.insert(node, 'end', text='Close After', values=(close_text,))
            self.config_tree.insert(node, 'end', text='Max Exit Spread', values=(self._format_number(thread.max_exit_spread),))

        def _update() -> None:
            tree = self.config_tree
            tree.delete(*tree.get_children())
            tree.insert('', 'end', text='Timezone', values=(self.config.timezone or 'UTC',))
            risk_status = 'Enabled' if self.config.risk.drawdown_enabled else 'Disabled'
            risk_node = tree.insert('', 'end', text='Risk Controls', values=(risk_status,), open=True)
            if self.config.risk.drawdown_enabled:
                tree.insert(risk_node, 'end', text='Drawdown Stop (%)', values=(self._format_number(self.config.risk.drawdown_stop),))
            exit_cfg = self._current_exit_config()
            mode_label = exit_cfg.close_logic_mode.replace('_', ' ').title()
            exit_node = tree.insert('', 'end', text='Exit Strategy', values=(mode_label,), open=True)
            tree.insert(exit_node, 'end', text='Close Logic Mode', values=(mode_label,))
            tree.insert(exit_node, 'end', text='Net PnL Threshold', values=(self._format_number(exit_cfg.net_pnl_threshold),))
            tree.insert(exit_node, 'end', text='Check Start (min)', values=(str(exit_cfg.close_start_minutes),))
            tree.insert(exit_node, 'end', text='Check Stop (min)', values=(str(exit_cfg.close_stop_minutes),))
            primary_root = tree.insert('', 'end', text='Primary Threads', values=('',), open=True)
            for thread in self.config.primary_threads:
                _add_thread(primary_root, thread)
            wednesday_root = tree.insert('', 'end', text='Wednesday Threads', values=('',), open=True)
            for thread in self.config.wednesday_threads:
                _add_thread(wednesday_root, thread)

        self._invoke_on_ui(_update)

    def _add_trade_to_table(self, trade_id: str, entry: Dict[str, Any]) -> None:
        if not getattr(self, "table", None):
            return

        account1 = dict(entry.get("account1", {}) or {})
        account2 = dict(entry.get("account2", {}) or {})

        symbol1 = str(account1.get("symbol", ""))
        symbol2 = str(account2.get("symbol", ""))

        try:
            lot1 = float(account1.get("lot", 0.0) or 0.0)
        except Exception:
            lot1 = 0.0
        try:
            lot2 = float(account2.get("lot", 0.0) or 0.0)
        except Exception:
            lot2 = 0.0
        account1["lot"] = lot1
        account2["lot"] = lot2

        def _parse_price(value: Any) -> Optional[float]:
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except Exception:
                    return None
            return None

        price1 = _parse_price(account1.get("entry_price"))
        price2 = _parse_price(account2.get("entry_price"))
        if price1 is not None:
            account1["entry_price"] = price1
        if price2 is not None:
            account2["entry_price"] = price2

        def _parse_time_value(value: Any) -> int:
            try:
                return int(float(value or 0))
            except Exception:
                return 0

        entry_time1 = _parse_time_value(account1.get("entry_time"))
        entry_time2 = _parse_time_value(account2.get("entry_time"))
        account1["entry_time"] = entry_time1
        account2["entry_time"] = entry_time2

        def _parse_float(value: Any) -> float:
            try:
                return float(value or 0.0)
            except Exception:
                return 0.0

        commission1 = _parse_float(account1.get("commission", account1.get("last_commission", 0.0)))
        commission2 = _parse_float(account2.get("commission", account2.get("last_commission", 0.0)))
        swap1 = _parse_float(account1.get("swap", account1.get("last_swap", 0.0)))
        swap2 = _parse_float(account2.get("swap", account2.get("last_swap", 0.0)))
        profit1 = _parse_float(account1.get("last_profit", account1.get("profit", 0.0)))
        profit2 = _parse_float(account2.get("last_profit", account2.get("profit", 0.0)))

        account1["commission"] = commission1
        account2["commission"] = commission2
        account1["last_commission"] = commission1
        account2["last_commission"] = commission2
        account1["swap"] = swap1
        account2["swap"] = swap2
        account1["last_swap"] = swap1
        account2["last_swap"] = swap2
        account1["last_profit"] = profit1
        account2["last_profit"] = profit2

        side1 = str(account1.get("side", "") or "").lower()
        side2 = str(account2.get("side", "") or "").lower()
        if side1 and side2:
            side_label = side1.upper() if side1 == side2 else f"{side1.upper()}/{side2.upper()}"
        else:
            side_label = (side1 or side2).upper()

        combined_commission = commission1 + commission2
        combined_swap = swap1 + swap2
        combined_profit = profit1 + profit2

        entry["account1"] = account1
        entry["account2"] = account2

        self.table.add_row(
            trade_id,
            [
                trade_id,
                symbol1,
                lot1,
                f"{price1:.5f}" if isinstance(price1, float) else "",
                self._fmt_time(entry_time1),
                f"{commission1:.2f}",
                f"{swap1:.2f}",
                f"{profit1:.2f}",
                symbol2,
                lot2,
                f"{price2:.5f}" if isinstance(price2, float) else "",
                self._fmt_time(entry_time2),
                f"{commission2:.2f}",
                f"{swap2:.2f}",
                f"{profit2:.2f}",
                side_label,
                f"{combined_commission:.2f}",
                f"{combined_swap:.2f}",
                f"{combined_profit:.2f}",
                "Close",
            ],
            dynamic_fields={
                "p1_commission": 5,
                "p1_swap": 6,
                "p1_profit": 7,
                "p2_commission": 12,
                "p2_swap": 13,
                "p2_profit": 14,
                "combined_commission": 16,
                "combined_swap": 17,
                "combined_profit": 18,
            },
            close_callback=self._on_close_pair,
        )

        self.table.set_metrics(
            trade_id,
            {
                "p1_commission": commission1,
                "p1_swap": swap1,
                "p1_profit": profit1,
                "p2_commission": commission2,
                "p2_swap": swap2,
                "p2_profit": profit2,
                "combined_commission": combined_commission,
                "combined_swap": combined_swap,
                "combined_profit": combined_profit,
            },
        )

    def _current_exit_config(self) -> ExitConfig:
        exit_cfg = getattr(self.config, "exit", None)
        if isinstance(exit_cfg, ExitConfig):
            return exit_cfg
        if isinstance(exit_cfg, dict):
            try:
                return ExitConfig.from_dict(exit_cfg)
            except Exception:
                pass
        return ExitConfig()

    def _ensure_trade_exit_defaults(self, info: Dict[str, Any]) -> None:
        exit_cfg = self._current_exit_config()
        info.setdefault("close_logic_mode", exit_cfg.close_logic_mode)
        info.setdefault("net_pnl_threshold", float(exit_cfg.net_pnl_threshold))
        info.setdefault("close_start_minutes", int(exit_cfg.close_start_minutes))
        info.setdefault("close_stop_minutes", int(exit_cfg.close_stop_minutes))
        info.setdefault("exit_checking_active", False)
        info.setdefault("exit_condition_met_time", 0.0)
        info.setdefault("last_close_attempt_ts", 0.0)
        info.setdefault("force_closed_at_stop", False)
        info.setdefault("exit_mode_used", None)
        info.setdefault("exit_condition_value", None)
        info.setdefault("exit_trigger_time", None)

    def _update_trade_exit_info(self, trade_id: str, **updates: Any) -> bool:
        changed = False
        with self._trade_lock:
            info = self.paired_trades.get(trade_id)
            if not info:
                return False
            for key, value in updates.items():
                if info.get(key) != value:
                    info[key] = value
                    changed = True
        return changed

    def _snapshot_active_trades(self) -> list[Dict[str, Any]]:
        snapshot: list[Dict[str, Any]] = []
        with self._trade_lock:
            for trade_id, info in self.paired_trades.items():
                entry = {"trade_id": str(trade_id)}
                entry.update(copy.deepcopy(info))
                snapshot.append(entry)
        return snapshot

    def _update_state_snapshot(self, state: Optional[AutomationState] = None) -> AutomationState:
        target = state or getattr(self, "state", None)
        if target is None:
            target = self.persistence.get_state()

        history_copy: list[Dict[str, Any]] = []
        for item in self.trade_history:
            if isinstance(item, dict):
                history_copy.append(dict(item))
        target.trade_history = history_copy
        target.active_trades = self._snapshot_active_trades()
        self.state = target
        return target

    def update_state_snapshot(self, state: Optional[AutomationState] = None) -> AutomationState:
        return self._update_state_snapshot(state)

    def _save_state(self) -> None:
        state = self._update_state_snapshot()
        self.persistence.save_state(state)

    def _restore_active_trades(self) -> None:
        active = getattr(self.state, "active_trades", [])
        if not isinstance(active, list):
            return

        restored = 0
        for raw in active:
            if not isinstance(raw, dict):
                continue
            trade_id = str(raw.get("trade_id") or "").strip()
            if not trade_id:
                continue
            info = {k: copy.deepcopy(v) for k, v in raw.items() if k != "trade_id"}
            self._ensure_trade_exit_defaults(info)
            with self._trade_lock:
                self.paired_trades[trade_id] = info
            self._add_trade_to_table(trade_id, info)
            restored += 1

        if restored:
            self._set_automation_status(f"Restored {restored} active trade(s) from previous session.", ok=True)

        self._update_state_snapshot(self.state)

    def _restore_trade_counter(self) -> None:
        highest = 0

        for trade_id in list(self.paired_trades.keys()):
            seq = self._extract_trade_sequence(trade_id)
            if seq > highest:
                highest = seq

        for entry in self.trade_history:
            if isinstance(entry, dict):
                seq = self._extract_trade_sequence(entry.get("trade_id"))
                if seq > highest:
                    highest = seq

        if highest >= self.trade_counter:
            self.trade_counter = highest + 1

    @staticmethod
    def _extract_trade_sequence(trade_id: Optional[str]) -> int:
        if not isinstance(trade_id, str):
            return 0
        match = re.search(r"(\d+)$", trade_id.strip())
        if not match:
            return 0
        try:
            return int(match.group(1))
        except Exception:
            return 0

    def _record_trade_history(self, entry: Dict[str, Any]) -> None:
        cleaned = dict(entry)
        cleaned.setdefault('recorded_at', time.time())
        self.trade_history.append(cleaned)
        if len(self.trade_history) > self.trade_history_limit:
            self.trade_history = self.trade_history[-self.trade_history_limit:]
        self._save_state()
        self._populate_trade_history_tree()
        self._export_trade_history_csv()

    def _export_trade_history_csv(self) -> None:
        headers = [
            "trade_id",
            "schedule",
            "thread_id",
            "opened_at",
            "closed_at",
            "close_logic_mode",
            "net_pnl_threshold",
            "close_start_minutes",
            "close_stop_minutes",
            "exit_trigger_time",
            "exit_mode_used",
            "exit_condition_value",
            "realized_combined_pnl",
            "force_closed_at_stop",
            "account1_symbol",
            "account1_lot",
            "account1_side",
            "account1_entry_price",
            "account1_entry_time",
            "account1_profit",
            "account1_commission",
            "account1_swap",
            "account2_symbol",
            "account2_lot",
            "account2_side",
            "account2_entry_price",
            "account2_entry_time",
            "account2_profit",
            "account2_commission",
            "account2_swap",
            "combined_profit",
            "combined_commission",
            "combined_swap",
        ]

        def _fmt_ts(ts_value: Any) -> str:
            try:
                ts_float = float(ts_value)
            except Exception:
                return ""
            if ts_float <= 0:
                return ""
            try:
                dt = datetime.fromtimestamp(ts_float)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return ""

        rows: list[Dict[str, Any]] = []
        for entry in self.trade_history:
            if not isinstance(entry, dict):
                continue
            account1 = entry.get('account1', {}) if isinstance(entry.get('account1'), dict) else {}
            account2 = entry.get('account2', {}) if isinstance(entry.get('account2'), dict) else {}
            rows.append({
                "trade_id": entry.get('trade_id', ''),
                "schedule": entry.get('schedule', ''),
                "thread_id": entry.get('thread_id', ''),
                "opened_at": _fmt_ts(entry.get('opened_at', 0.0)),
                "closed_at": _fmt_ts(entry.get('closed_at', 0.0)),
                "close_logic_mode": entry.get('close_logic_mode', ''),
                "net_pnl_threshold": entry.get('net_pnl_threshold', 0.0),
                "close_start_minutes": entry.get('close_start_minutes', 0),
                "close_stop_minutes": entry.get('close_stop_minutes', 0),
                "exit_trigger_time": _fmt_ts(entry.get('exit_trigger_time', 0.0)),
                "exit_mode_used": entry.get('exit_mode_used', ''),
                "exit_condition_value": entry.get('exit_condition_value', ''),
                "realized_combined_pnl": entry.get('realized_combined_pnl', entry.get('combined_profit', 0.0)),
                "force_closed_at_stop": entry.get('force_closed_at_stop', False),
                "account1_symbol": account1.get('symbol', ''),
                "account1_lot": account1.get('lot', ''),
                "account1_side": account1.get('side', ''),
                "account1_entry_price": account1.get('entry_price', ''),
                "account1_entry_time": _fmt_ts(account1.get('entry_time', 0.0)),
                "account1_profit": account1.get('profit', 0.0),
                "account1_commission": account1.get('commission', 0.0),
                "account1_swap": account1.get('swap', 0.0),
                "account2_symbol": account2.get('symbol', ''),
                "account2_lot": account2.get('lot', ''),
                "account2_side": account2.get('side', ''),
                "account2_entry_price": account2.get('entry_price', ''),
                "account2_entry_time": _fmt_ts(account2.get('entry_time', 0.0)),
                "account2_profit": account2.get('profit', 0.0),
                "account2_commission": account2.get('commission', 0.0),
                "account2_swap": account2.get('swap', 0.0),
                "combined_profit": entry.get('combined_profit', 0.0),
                "combined_commission": entry.get('combined_commission', 0.0),
                "combined_swap": entry.get('combined_swap', 0.0),
            })

        try:
            with self._history_export_lock:
                parent = self.history_csv_path.parent
                if parent not in (None, Path('.')):
                    parent.mkdir(parents=True, exist_ok=True)
                with self.history_csv_path.open('w', newline='', encoding='utf-8') as fh:
                    writer = csv.DictWriter(fh, fieldnames=headers)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow(row)
        except Exception as exc:
            print(f"Failed to export trade history CSV: {exc}", file=sys.stderr)

    def _update_trade_profit_cache(
        self,
        trade_id: str,
        profit1: float,
        commission1: float,
        swap1: float,
        profit2: float,
        commission2: float,
        swap2: float,
    ) -> None:
        with self._trade_lock:
            info = self.paired_trades.get(trade_id)
            if not info:
                return
            if isinstance(info.get('account1'), dict):
                account1 = info['account1']
                account1['last_profit'] = float(profit1)
                account1['last_commission'] = float(commission1)
                account1['last_swap'] = float(swap1)
                account1['commission'] = float(commission1)
                account1['swap'] = float(swap1)
            if isinstance(info.get('account2'), dict):
                account2 = info['account2']
                account2['last_profit'] = float(profit2)
                account2['last_commission'] = float(commission2)
                account2['last_swap'] = float(swap2)
                account2['commission'] = float(commission2)
                account2['swap'] = float(swap2)
            info['last_combined_profit'] = float(profit1 + profit2)

    @staticmethod
    def _direction_key_to_display(key: str) -> str:
        key = (key or "buy_sell").lower()
        return key.replace("_", "/").upper()

    @staticmethod
    def _direction_display_to_key(value: str) -> str:
        value = (value or "BUY/SELL").lower()
        return value.replace("/", "_")

    @staticmethod
    def _hours_from_minutes(minutes: int) -> str:
        if minutes <= 0:
            return "0"
        hours = minutes / 60.0
        if abs(hours - round(hours)) < 1e-6:
            return str(int(round(hours)))
        return f"{hours:.2f}"

    @staticmethod
    def _minutes_from_hours(value: str) -> int:
        value = (value or "0").strip()
        if not value:
            return 0
        hours = float(value)
        return max(0, int(round(hours * 60)))

    @staticmethod
    def _format_number(value: float) -> str:
        text = f"{value:.4f}"
        text = text.rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _format_money(value: Any) -> str:
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return "--"

    def _refresh_schedule_overview(self, state: Optional[AutomationState] = None) -> None:
        if not hasattr(self, "schedule_tree"):
            return
        if state is None:
            state = getattr(self, 'state', None)
            if state is None:
                state = self.persistence.get_state()
        tz_name = self.config.timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        schedules = [*self.config.primary_threads, *self.config.wednesday_threads]
        rows = [self._schedule_overview_row(schedule, state, now) for schedule in schedules]

        def _update_tree() -> None:
            self.schedule_tree.delete(*self.schedule_tree.get_children())
            for values in rows:
                self.schedule_tree.insert("", "end", values=values)

        self._invoke_on_ui(_update_tree)

    def on_state_updated(self, state: AutomationState) -> None:
        self.state = state
        incoming_history = []
        for entry in getattr(state, 'trade_history', []):
            if isinstance(entry, dict):
                incoming_history.append(dict(entry))
        if incoming_history and incoming_history != self.trade_history:
            if len(incoming_history) > self.trade_history_limit:
                incoming_history = incoming_history[-self.trade_history_limit:]
            self.trade_history = incoming_history
            self._populate_trade_history_tree()
        self._refresh_schedule_overview(state)

    def _schedule_overview_row(
        self, schedule: ThreadSchedule, state: AutomationState, now: datetime
    ) -> tuple[str, str, str, str, str, str, str, str, str]:
        status = "ENABLED" if schedule.enabled else "Disabled"
        pair_desc = f"{schedule.symbol1 or '-'} / {schedule.symbol2 or '-'}"
        lots = f"{self._format_number(schedule.lot1)} / {self._format_number(schedule.lot2)}"
        direction = self._direction_key_to_display(schedule.direction)
        window = self._format_entry_window(schedule)
        days = self._format_weekdays(schedule.weekdays)
        last_run_iso = state.last_runs.get(schedule.thread_id)
        last_run_date = self._parse_iso_date(last_run_iso)
        last_run_display = last_run_date.strftime("%Y-%m-%d") if last_run_date else "Never"
        next_run_dt = self._next_schedule_time(schedule, now, last_run_date)
        if isinstance(next_run_dt, datetime):
            if abs((next_run_dt - now).total_seconds()) < 1:
                next_run_display = "Window active"
            else:
                next_run_display = self._format_datetime(next_run_dt)
        else:
            next_run_display = next_run_dt or "—"
        return (
            f"{schedule.name} ({schedule.thread_id})",
            status,
            pair_desc,
            lots,
            direction,
            window,
            days,
            next_run_display,
            last_run_display,
        )

    @staticmethod
    def _format_entry_window(schedule: ThreadSchedule) -> str:
        if schedule.entry_start and schedule.entry_end:
            return f"{schedule.entry_start} - {schedule.entry_end}"
        if schedule.entry_start:
            return f"from {schedule.entry_start}"
        if schedule.entry_end:
            return f"until {schedule.entry_end}"
        return "Configure window"

    @staticmethod
    def _format_weekdays(weekdays: Sequence[int]) -> str:
        if not weekdays:
            return "All days"
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        try:
            ordered = sorted({int(day) % 7 for day in weekdays})
        except Exception:
            return "All days"
        return ", ".join(names[day] for day in ordered)

    @staticmethod
    def _parse_iso_date(value: Optional[str]) -> Optional[date]:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except Exception:
            return None

    @staticmethod
    def _format_datetime(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M")

    def _next_schedule_time(
        self, schedule: ThreadSchedule, now: datetime, last_run: Optional[date]
    ) -> Optional[Union[datetime, str]]:
        if not schedule.enabled:
            return None
        start_time = parse_time_string(schedule.entry_start)
        if start_time is None:
            return "Set entry time"
        end_time = parse_time_string(schedule.entry_end) if schedule.entry_end else None
        weekdays = list(schedule.weekdays) if schedule.weekdays else list(range(7))

        for offset in range(14):
            candidate_date = now.date() + timedelta(days=offset)
            if weekdays and candidate_date.weekday() not in weekdays:
                continue
            start_dt = datetime.combine(candidate_date, start_time, tzinfo=now.tzinfo)
            end_dt = None
            if end_time:
                end_dt = datetime.combine(candidate_date, end_time, tzinfo=now.tzinfo)
                if end_time <= start_time:
                    end_dt += timedelta(days=1)
            if offset == 0:
                if last_run and last_run == candidate_date:
                    if end_dt and now <= end_dt:
                        continue
                    if not end_dt and now <= start_dt:
                        continue
                if start_dt <= now and end_dt and now <= end_dt:
                    if not last_run or last_run != candidate_date:
                        return now
                    continue
                if now <= start_dt and (not last_run or last_run != candidate_date):
                    return start_dt
                continue
            if last_run and last_run == candidate_date:
                continue
            return start_dt
        return None

    @staticmethod
    def _direction_key_to_sides(key: str) -> Sequence[str]:
        mapping = {
            "buy_sell": ("buy", "sell"),
            "sell_buy": ("sell", "buy"),
            "buy_buy": ("buy", "buy"),
            "sell_sell": ("sell", "sell"),
        }
        return mapping.get((key or "buy_sell").lower(), ("buy", "sell"))

    def _set_automation_status(self, message: str, ok: bool = True) -> None:
        color = "#070" if ok else "#b00"
        label = getattr(self, 'automation_status_label', None)
        if not label:
            return

        def _update() -> None:
            label.configure(text=message, foreground=color)

        self._invoke_on_ui(_update)

    def _invoke_on_ui(self, func) -> None:
        try:
            self.root.after(0, func)
        except Exception:
            try:
                func()
            except Exception:
                pass

    def _execute_schedule_trade(self, schedule: ThreadSchedule) -> None:
        sides = self._direction_key_to_sides(schedule.direction)
        try:
            symbol1 = schedule.symbol1 or self.pair1_var.get().strip()
            symbol2 = schedule.symbol2 or self.pair2_var.get().strip()
            lot1 = float(schedule.lot1)
            lot2 = float(schedule.lot2)
            self._open_trade_pair(
                symbol1,
                lot1,
                sides[0],
                symbol2,
                lot2,
                sides[1],
                schedule_name=schedule.name,
                schedule_thread_id=schedule.thread_id,
            )
            self._set_automation_status(
                f"Scheduled trade executed for {schedule.name} ({schedule.thread_id}).",
                ok=True,
            )
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
        schedule_thread_id: Optional[str] = None,
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
            "account1": {"symbol": symbol1, "lot": float(lot1), "side": side1, "position": pos1, "magic": magic1, "last_profit": 0.0},
            "account2": {"symbol": symbol2, "lot": float(lot2), "side": side2, "position": pos2, "magic": magic2, "last_profit": 0.0},
            "schedule": schedule_name or "manual",
            "thread_id": schedule_thread_id,
            "opened_at": time.time(),
        }
        self._ensure_trade_exit_defaults(entry)
        with self._trade_lock:
            self.paired_trades[trade_id] = entry

        eprice1 = r1.get("entry_price")
        eprice2 = r2.get("entry_price")
        etime1 = r1.get("entry_time") or 0
        etime2 = r2.get("entry_time") or 0
        commission1 = float(r1.get("commission", 0.0) or 0.0)
        commission2 = float(r2.get("commission", 0.0) or 0.0)
        swap1 = float(r1.get("swap", 0.0) or 0.0)
        swap2 = float(r2.get("swap", 0.0) or 0.0)

        if isinstance(eprice1, (int, float)):
            entry["account1"]["entry_price"] = float(eprice1)
        if isinstance(eprice2, (int, float)):
            entry["account2"]["entry_price"] = float(eprice2)
        entry["account1"]["entry_time"] = int(etime1) if etime1 else 0
        entry["account2"]["entry_time"] = int(etime2) if etime2 else 0
        entry["account1"]["commission"] = commission1
        entry["account2"]["commission"] = commission2
        entry["account1"]["swap"] = swap1
        entry["account2"]["swap"] = swap2
        entry["account1"]["last_commission"] = commission1
        entry["account2"]["last_commission"] = commission2
        entry["account1"]["last_swap"] = swap1
        entry["account2"]["last_swap"] = swap2

        self._add_trade_to_table(trade_id, entry)
        self._save_state()
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

    def _gather_active_trades(
        self,
        now: datetime,
        config: AppConfig,
    ) -> tuple[list[TrackedTrade], list[tuple[Optional[WorkerClient], str]]]:
        trades: list[TrackedTrade] = []
        requests: list[tuple[Optional[WorkerClient], str]] = []
        thread_map: Dict[str, ThreadSchedule] = {
            thread.thread_id: thread
            for thread in (*config.primary_threads, *config.wednesday_threads)
        }
        with self._trade_lock:
            for trade_id, info in self.paired_trades.items():
                self._ensure_trade_exit_defaults(info)
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
                thread_id = info.get("thread_id")
                schedule = thread_map.get(thread_id)
                close_after = schedule.close_after_minutes if schedule else 0
                max_exit = schedule.max_exit_spread if schedule else 0.0
                try:
                    profit1 = float(account1.get("last_profit", account1.get("profit", 0.0)) or 0.0)
                except Exception:
                    profit1 = 0.0
                try:
                    profit2 = float(account2.get("last_profit", account2.get("profit", 0.0)) or 0.0)
                except Exception:
                    profit2 = 0.0
                combined_profit = profit1 + profit2
                info["last_combined_profit"] = combined_profit
                mode_raw = str(info.get("close_logic_mode", "spread") or "spread").strip().lower()
                info["close_logic_mode"] = mode_raw
                net_threshold = float(info.get("net_pnl_threshold", 0.0) or 0.0)
                start_minutes = int(info.get("close_start_minutes", 0) or 0)
                stop_minutes = int(info.get("close_stop_minutes", 0) or 0)
                info["net_pnl_threshold"] = net_threshold
                info["close_start_minutes"] = start_minutes
                info["close_stop_minutes"] = stop_minutes
                exit_checking_active = bool(info.get("exit_checking_active", False))
                exit_condition_ts = float(info.get("exit_condition_met_time", 0.0) or 0.0)
                exit_condition_dt: Optional[datetime]
                if exit_condition_ts > 0:
                    try:
                        exit_condition_dt = datetime.fromtimestamp(exit_condition_ts, tz=now.tzinfo)
                    except Exception:
                        exit_condition_dt = datetime.utcfromtimestamp(exit_condition_ts).replace(tzinfo=now.tzinfo)
                else:
                    exit_condition_dt = None
                force_closed = bool(info.get("force_closed_at_stop", False))
                trades.append(
                    TrackedTrade(
                        trade_id,
                        opened_dt,
                        tuple(symbols),
                        close_after,
                        max_exit,
                        close_logic_mode=mode_raw,
                        net_pnl_threshold=net_threshold,
                        close_start_minutes=start_minutes,
                        close_stop_minutes=stop_minutes,
                        combined_profit=combined_profit,
                        exit_checking_active=exit_checking_active,
                        exit_condition_met_time=exit_condition_dt,
                        force_closed_at_stop=force_closed,
                    )
                )
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
            all_threads = [*config.primary_threads, *config.wednesday_threads]
            for schedule in all_threads:
                if not schedule_should_trigger(schedule, now, state):
                    continue
                symbols = [s for s in (schedule.symbol1, schedule.symbol2) if s]
                requests = []
                if schedule.symbol1:
                    requests.append((self.worker1, schedule.symbol1))
                if schedule.symbol2:
                    requests.append((self.worker2, schedule.symbol2))
                spreads = self._fetch_spreads(requests)
                if not spreads_within_entry_limit(symbols, spreads, schedule.max_entry_spread):
                    self._set_automation_status(
                        f"{schedule.name} ({schedule.thread_id}) skipped due to spread limit.",
                        ok=False,
                    )
                    continue
                self._invoke_on_ui(lambda sch=schedule: self._execute_schedule_trade(sch))
                mark_schedule_triggered(state, schedule, now)
                changed = True

        trades, requests = self._gather_active_trades(now, config)
        trade_map = {trade.trade_id: trade for trade in trades}
        if trades and connected:
            spread_trades = [trade for trade in trades if trade.close_logic_mode != "net_pnl_threshold"]
            net_trades = [trade for trade in trades if trade.close_logic_mode == "net_pnl_threshold"]
            spreads: Dict[str, float] = {}
            if spread_trades and requests:
                spreads = self._fetch_spreads(requests)
                due_close = trades_due_for_close(spread_trades, now, spreads)
                if due_close:
                    self._set_automation_status(
                        f"Auto-close triggered for {len(due_close)} trade(s).", ok=False
                    )
                for trade_id in due_close:
                    trade = trade_map.get(trade_id)
                    now_ts = time.time()
                    updates = {
                        "exit_mode_used": "spread",
                        "exit_condition_value": trade.max_exit_spread if trade else None,
                        "exit_trigger_time": now_ts,
                        "last_close_attempt_ts": now_ts,
                        "force_closed_at_stop": False,
                    }
                    if self._update_trade_exit_info(trade_id, **updates):
                        changed = True
                    self._close_pair_threadsafe(trade_id)

            if net_trades:
                for trade in net_trades:
                    minutes_open = max(0.0, (now - trade.opened_at).total_seconds() / 60.0)
                    start_minutes = max(0, trade.close_start_minutes)
                    stop_minutes = max(0, trade.close_stop_minutes)
                    threshold = trade.net_pnl_threshold
                    if minutes_open >= start_minutes:
                        if self._update_trade_exit_info(trade.trade_id, exit_checking_active=True):
                            changed = True
                        checking_active = True
                    else:
                        checking_active = False
                    condition_met = checking_active and trade.combined_profit >= threshold
                    if condition_met:
                        event_ts = time.time()
                        trigger_ts = (
                            trade.exit_condition_met_time.timestamp()
                            if trade.exit_condition_met_time
                            else event_ts
                        )
                        updates = {
                            "exit_mode_used": "net_pnl_threshold",
                            "exit_condition_value": threshold,
                            "exit_trigger_time": trigger_ts,
                            "last_close_attempt_ts": event_ts,
                            "force_closed_at_stop": False,
                        }
                        if not trade.exit_condition_met_time:
                            updates["exit_condition_met_time"] = event_ts
                            self._set_automation_status(
                                f"Net PnL target met for {trade.trade_id}. Closing trade.",
                                ok=True,
                            )
                        if self._update_trade_exit_info(trade.trade_id, **updates):
                            changed = True
                        self._close_pair_threadsafe(trade.trade_id)
                        continue

                    stop_due = stop_minutes > 0 and minutes_open >= stop_minutes
                    if stop_due:
                        event_ts = time.time()
                        show_message = not trade.force_closed_at_stop
                        updates = {
                            "exit_mode_used": "net_pnl_threshold_stop",
                            "exit_condition_value": threshold,
                            "exit_trigger_time": event_ts,
                            "force_closed_at_stop": True,
                            "last_close_attempt_ts": event_ts,
                            "exit_checking_active": True,
                        }
                        if self._update_trade_exit_info(trade.trade_id, **updates):
                            changed = True
                        if show_message:
                            self._set_automation_status(
                                f"Net PnL window expired for {trade.trade_id}. Forcing close.",
                                ok=False,
                            )
                        self._close_pair_threadsafe(trade.trade_id)

        if connected:
            accounts = self._fetch_accounts()
            if accounts and drawdown_breached(config.risk, accounts):
                if trades:
                    self._set_automation_status("Drawdown stop triggered. Closing all trades.", ok=False)
                    now_ts = time.time()
                    for trade in trades:
                        if self._update_trade_exit_info(
                            trade.trade_id,
                            exit_mode_used="drawdown_stop",
                            exit_condition_value=config.risk.drawdown_stop,
                            exit_trigger_time=now_ts,
                            last_close_attempt_ts=now_ts,
                        ):
                            changed = True
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
            login1 = d1.get('login') or 'Account 1'
            login2 = d2.get('login') or 'Account 2'
            server1 = d1.get('server') or ''
            server2 = d2.get('server') or ''
            msg = f"Connected: {login1}{'@' + server1 if server1 else ''} | {login2}{'@' + server2 if server2 else ''}"
            self._set_automation_status(msg, ok=True)
            self._refresh_account_summaries()
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

        account1_src = info.get('account1', {}) or {}
        account2_src = info.get('account2', {}) or {}
        account1 = dict(account1_src)
        account2 = dict(account2_src)

        p1_profit = float(account1.get('last_profit', 0.0) or 0.0)
        p2_profit = float(account2.get('last_profit', 0.0) or 0.0)
        p1_commission = float(account1.get('last_commission', account1.get('commission', 0.0)) or 0.0)
        p2_commission = float(account2.get('last_commission', account2.get('commission', 0.0)) or 0.0)
        p1_swap = float(account1.get('last_swap', account1.get('swap', 0.0)) or 0.0)
        p2_swap = float(account2.get('last_swap', account2.get('swap', 0.0)) or 0.0)
        if self.worker1 and account1_src.get('position'):
            try:
                res1 = self.worker1.get_profit(account1_src['position'])
                p1_profit = float(res1.get('profit', p1_profit))
                p1_commission = float(res1.get('commission', p1_commission))
                p1_swap = float(res1.get('swap', p1_swap))
            except Exception:
                pass
        if self.worker2 and account2_src.get('position'):
            try:
                res2 = self.worker2.get_profit(account2_src['position'])
                p2_profit = float(res2.get('profit', p2_profit))
                p2_commission = float(res2.get('commission', p2_commission))
                p2_swap = float(res2.get('swap', p2_swap))
            except Exception:
                pass

        account1.pop('last_profit', None)
        account2.pop('last_profit', None)
        account1.pop('last_commission', None)
        account2.pop('last_commission', None)
        account1.pop('last_swap', None)
        account2.pop('last_swap', None)
        close_time = time.time()
        account1['profit'] = p1_profit
        account2['profit'] = p2_profit
        account1['commission'] = p1_commission
        account2['commission'] = p2_commission
        account1['swap'] = p1_swap
        account2['swap'] = p2_swap

        opened_at_ts = float(info.get('opened_at', 0.0) or 0.0)
        total_minutes_open = max(0.0, (close_time - opened_at_ts) / 60.0) if opened_at_ts else 0.0
        exit_mode = info.get('exit_mode_used') or 'manual'
        exit_condition_value = info.get('exit_condition_value')
        exit_trigger_time = float(info.get('exit_trigger_time') or info.get('exit_condition_met_time') or 0.0)
        close_logic_mode = str(info.get('close_logic_mode', self._current_exit_config().close_logic_mode) or 'spread')
        net_threshold = float(info.get('net_pnl_threshold', 0.0) or 0.0)
        close_start_minutes = int(info.get('close_start_minutes', 0) or 0)
        close_stop_minutes = int(info.get('close_stop_minutes', 0) or 0)
        force_closed_at_stop = bool(info.get('force_closed_at_stop', False))
        realized_combined = p1_profit + p2_profit

        history_entry = {
            'trade_id': trade_id,
            'schedule': info.get('schedule'),
            'thread_id': info.get('thread_id'),
            'opened_at': float(info.get('opened_at', 0.0) or 0.0),
            'closed_at': close_time,
            'account1': account1,
            'account2': account2,
            'combined_profit': realized_combined,
            'combined_commission': p1_commission + p2_commission,
            'combined_swap': p1_swap + p2_swap,
            'close_logic_mode': close_logic_mode,
            'net_pnl_threshold': net_threshold,
            'close_start_minutes': close_start_minutes,
            'close_stop_minutes': close_stop_minutes,
            'exit_mode_used': exit_mode,
            'exit_condition_value': exit_condition_value,
            'exit_trigger_time': exit_trigger_time,
            'realized_combined_pnl': realized_combined,
            'force_closed_at_stop': force_closed_at_stop,
            'total_minutes_open': total_minutes_open,
        }

        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                futures = []
                if self.worker1 and account1_src.get('position'):
                    futures.append(ex.submit(
                        self.worker1.close,
                        account1_src.get('position'),
                        account1_src.get('symbol'),
                        account1_src.get('side'),
                        account1_src.get('lot'),
                        account1_src.get('magic'),
                    ))
                if self.worker2 and account2_src.get('position'):
                    futures.append(ex.submit(
                        self.worker2.close,
                        account2_src.get('position'),
                        account2_src.get('symbol'),
                        account2_src.get('side'),
                        account2_src.get('lot'),
                        account2_src.get('magic'),
                    ))
                for future in futures:
                    future.result(timeout=20)
            self.table.remove_row(trade_id)
            with self._trade_lock:
                self.paired_trades.pop(trade_id, None)
            self._record_trade_history(history_entry)
        except Exception as e:
            messagebox.showerror('Close Error', str(e))


    def _schedule_profit_updates(self) -> None:
        self.root.after(800, self._update_profits)

    def _update_profits(self) -> None:
        try:
            with self._trade_lock:
                snapshot = {tid: dict(info) for tid, info in self.paired_trades.items()}
            for trade_id, info in snapshot.items():
                a1 = info.get("account1", {}) or {}
                a2 = info.get("account2", {}) or {}
                p1: Optional[Dict[str, Any]] = None
                if self.worker1 and self.connected1 and a1.get("position"):
                    try:
                        p1 = self.worker1.get_profit(a1.get("position"))
                    except Exception:
                        p1 = None
                p2: Optional[Dict[str, Any]] = None
                if self.worker2 and self.connected2 and a2.get("position"):
                    try:
                        p2 = self.worker2.get_profit(a2.get("position"))
                    except Exception:
                        p2 = None

                p1_profit = float((p1 or {}).get("profit", a1.get("last_profit", a1.get("profit", 0.0))) or 0.0)
                p2_profit = float((p2 or {}).get("profit", a2.get("last_profit", a2.get("profit", 0.0))) or 0.0)
                p1_commission = float(
                    (p1 or {}).get("commission", a1.get("last_commission", a1.get("commission", 0.0))) or 0.0
                )
                p1_swap = float((p1 or {}).get("swap", a1.get("last_swap", a1.get("swap", 0.0))) or 0.0)
                p2_commission = float(
                    (p2 or {}).get("commission", a2.get("last_commission", a2.get("commission", 0.0))) or 0.0
                )
                p2_swap = float((p2 or {}).get("swap", a2.get("last_swap", a2.get("swap", 0.0))) or 0.0)

                p1_open = True if p1 is None else bool(p1.get("open", True))
                p2_open = True if p2 is None else bool(p2.get("open", True))

                total = p1_profit + p2_profit
                combined_commission = p1_commission + p2_commission
                combined_swap = p1_swap + p2_swap

                self._update_trade_profit_cache(
                    trade_id,
                    p1_profit,
                    p1_commission,
                    p1_swap,
                    p2_profit,
                    p2_commission,
                    p2_swap,
                )
                self.table.set_metrics(
                    trade_id,
                    {
                        "p1_profit": p1_profit,
                        "p1_commission": p1_commission,
                        "p1_swap": p1_swap,
                        "p2_profit": p2_profit,
                        "p2_commission": p2_commission,
                        "p2_swap": p2_swap,
                        "combined_profit": total,
                        "combined_commission": combined_commission,
                        "combined_swap": combined_swap,
                    },
                )

                if not p1_open and not p2_open:
                    with self._trade_lock:
                        original = self.paired_trades.pop(trade_id, None)
                    self.table.remove_row(trade_id)
                    if original:
                        account1_entry = dict(original.get("account1", {}) or {})
                        account2_entry = dict(original.get("account2", {}) or {})
                        profit1 = float(account1_entry.get("last_profit", p1_profit) or 0.0)
                        profit2 = float(account2_entry.get("last_profit", p2_profit) or 0.0)
                        commission1 = float(account1_entry.get("last_commission", p1_commission) or 0.0)
                        commission2 = float(account2_entry.get("last_commission", p2_commission) or 0.0)
                        swap1 = float(account1_entry.get("last_swap", p1_swap) or 0.0)
                        swap2 = float(account2_entry.get("last_swap", p2_swap) or 0.0)
                        account1_entry.pop("last_profit", None)
                        account2_entry.pop("last_profit", None)
                        account1_entry.pop("last_commission", None)
                        account2_entry.pop("last_commission", None)
                        account1_entry.pop("last_swap", None)
                        account2_entry.pop("last_swap", None)
                        account1_entry["profit"] = profit1
                        account2_entry["profit"] = profit2
                        account1_entry["commission"] = commission1
                        account2_entry["commission"] = commission2
                        account1_entry["swap"] = swap1
                        account2_entry["swap"] = swap2
                        history_entry = {
                            "trade_id": trade_id,
                            "schedule": original.get("schedule"),
                            "thread_id": original.get("thread_id"),
                            "opened_at": float(original.get("opened_at", 0.0) or 0.0),
                            "closed_at": time.time(),
                            "account1": account1_entry,
                            "account2": account2_entry,
                            "combined_profit": account1_entry["profit"] + account2_entry["profit"],
                            "combined_commission": commission1 + commission2,
                            "combined_swap": swap1 + swap2,
                        }
                        self._record_trade_history(history_entry)
        finally:
            self._refresh_account_summaries()
            self._schedule_profit_updates()

    def _refresh_account_summaries(self) -> None:
        info1: Dict[str, Any] = {}
        info2: Dict[str, Any] = {}

        if self.worker1 and self.connected1:
            try:
                info1 = self.worker1.get_account_info() or {}
            except Exception:
                info1 = {}
        if self.worker2 and self.connected2:
            try:
                info2 = self.worker2.get_account_info() or {}
            except Exception:
                info2 = {}

        balance1 = self._format_money(info1.get("balance")) if info1 else "--"
        equity1 = self._format_money(info1.get("equity")) if info1 else "--"
        balance2 = self._format_money(info2.get("balance")) if info2 else "--"
        equity2 = self._format_money(info2.get("equity")) if info2 else "--"

        self.account1_balance_var.set(f"Balance: {balance1}")
        self.account1_equity_var.set(f"Equity: {equity1}")
        self.account2_balance_var.set(f"Balance: {balance2}")
        self.account2_equity_var.set(f"Equity: {equity2}")

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
        self.status1.configure(text="disconnected", foreground="#b00")
        self.status2.configure(text="disconnected", foreground="#b00")
        self.account1_balance_var.set("Balance: --")
        self.account1_equity_var.set("Equity: --")
        self.account2_balance_var.set("Balance: --")
        self.account2_equity_var.set("Equity: --")
        self._set_automation_status("Disconnected from terminals.", ok=False)

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


