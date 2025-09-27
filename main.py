import json
import os
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional

import tkinter as tk
from tkinter import ttk, messagebox

from multiprocessing import get_context
from mt5_worker import worker_main


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
        self._state_dir = os.path.join(os.path.expanduser("~"), ".swap_gainer")
        self._state_path = os.path.join(self._state_dir, "state.json")

        # UI Vars
        self.terminal1_var = tk.StringVar(value=DEFAULT_TERMINAL_1)
        self.terminal2_var = tk.StringVar(value=DEFAULT_TERMINAL_2)
        self.pair1_var = tk.StringVar()
        self.lot1_var = tk.StringVar()
        self.pair2_var = tk.StringVar()
        self.lot2_var = tk.StringVar()

        self._build_ui()
        self._load_and_restore_state()
        self._schedule_profit_updates()

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

    def _on_connect(self) -> None:
        path1 = self.terminal1_var.get().strip()
        path2 = self.terminal2_var.get().strip()
        if not path1 or not path2:
            messagebox.showerror("Error", "Please provide both terminal paths.")
            return

        if self._connect_workers(path1, path2):
            self._save_state()

    def _on_place(self, side: str) -> None:
        # Backwards-compatible: same side on both accounts
        return self._on_place_mixed(side, side)

    def _on_place_mixed(self, side1: str, side2: str) -> None:
        if not (self.connected1 and self.connected2 and self.worker1 and self.worker2):
            messagebox.showerror("Error", "Connect both terminals first.")
            return

        symbol1 = self.pair1_var.get().strip()
        symbol2 = self.pair2_var.get().strip()
        try:
            lot1 = float(self.lot1_var.get().strip())
            lot2 = float(self.lot2_var.get().strip())
        except Exception:
            messagebox.showerror("Error", "Invalid lot sizes.")
            return
        if not symbol1 or not symbol2 or lot1 <= 0 or lot2 <= 0:
            messagebox.showerror("Error", "Provide valid symbols and positive lot sizes.")
            return

        trade_id = f"T{self.trade_counter:05d}"
        self.trade_counter += 1
        magic1 = self.MAGIC_BASE + 1
        magic2 = self.MAGIC_BASE + 2

        try:
            # Issue both orders concurrently to minimize latency delta
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(
                    self.worker1.buy if side1 == "buy" else self.worker1.sell,
                    symbol1,
                    lot1,
                    trade_id,
                    magic1,
                )
                f2 = ex.submit(
                    self.worker2.buy if side2 == "buy" else self.worker2.sell,
                    symbol2,
                    lot2,
                    trade_id,
                    magic2,
                )
                r1 = f1.result(timeout=20)
                r2 = f2.result(timeout=20)

            pos1 = int(r1.get("position_ticket"))
            pos2 = int(r2.get("position_ticket"))
            if pos1 <= 0 or pos2 <= 0:
                raise RuntimeError("Failed to obtain position tickets for both accounts.")

            info = {
                "account1": {
                    "symbol": symbol1,
                    "lot": float(lot1),
                    "side": side1,
                    "position": pos1,
                    "magic": magic1,
                    "entry_price": float(r1.get("entry_price"))
                    if isinstance(r1.get("entry_price"), (int, float))
                    else None,
                    "entry_time": int(r1.get("entry_time") or 0),
                },
                "account2": {
                    "symbol": symbol2,
                    "lot": float(lot2),
                    "side": side2,
                    "position": pos2,
                    "magic": magic2,
                    "entry_price": float(r2.get("entry_price"))
                    if isinstance(r2.get("entry_price"), (int, float))
                    else None,
                    "entry_time": int(r2.get("entry_time") or 0),
                },
            }

            self.paired_trades[trade_id] = info
            self._add_trade_row(trade_id, info, 0.0, 0.0)
            self._save_state()

        except Exception as e:
            messagebox.showerror("Trade Error", str(e))

    def _on_close_pair(self, trade_id: str) -> None:
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
            self.paired_trades.pop(trade_id, None)
            self._save_state()
        except Exception as e:
            messagebox.showerror("Close Error", str(e))

    def _schedule_profit_updates(self) -> None:
        self.root.after(800, self._update_profits)

    def _update_profits(self) -> None:
        state_changed = False
        try:
            for trade_id, info in list(self.paired_trades.items()):
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
                    self.paired_trades.pop(trade_id, None)
                    state_changed = True
        finally:
            if state_changed:
                self._save_state()
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
        self.buy1_sell2_btn.configure(state="disabled")
        self.sell1_buy2_btn.configure(state="disabled")
        self.status1.configure(text="disconnected", foreground="#b00")
        self.status2.configure(text="disconnected", foreground="#b00")

    def on_close(self) -> None:
        self._save_state()
        self._cleanup_workers()
        self.root.destroy()

    def _connect_workers(self, path1: str, path2: str, show_errors: bool = True) -> bool:
        self.terminal1_var.set(path1)
        self.terminal2_var.set(path2)
        self._cleanup_workers()
        try:
            self.worker1 = WorkerClient("A1", path1)
            self.worker2 = WorkerClient("A2", path2)
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(self.worker1.connect, path1)
                f2 = ex.submit(self.worker2.connect, path2)
                f1.result(timeout=25)
                f2.result(timeout=25)
            self.connected1 = True
            self.connected2 = True
            self.status1.configure(text="connected", foreground="#070")
            self.status2.configure(text="connected", foreground="#070")
            self.buy_btn.configure(state="normal")
            self.sell_btn.configure(state="normal")
            self.buy1_sell2_btn.configure(state="normal")
            self.sell1_buy2_btn.configure(state="normal")
            return True
        except Exception as e:
            if show_errors:
                messagebox.showerror("Connection Failed", str(e))
            self._cleanup_workers()
            return False

    def _format_time(self, ts: int) -> str:
        if not ts:
            return ""
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
        except Exception:
            return str(ts)

    def _add_trade_row(self, trade_id: str, info: Dict[str, Any], profit1: float, profit2: float) -> None:
        a1 = info.get("account1", {})
        a2 = info.get("account2", {})
        side1 = (a1.get("side") or "").upper()
        side2 = (a2.get("side") or "").upper()
        if side1 and side2 and side1 != side2:
            side_label = f"{side1}/{side2}"
        else:
            side_label = side1 or side2

        values = [
            trade_id,
            a1.get("symbol", ""),
            a1.get("lot", ""),
            f"{a1.get('entry_price', 0.0):.5f}" if isinstance(a1.get("entry_price"), (int, float)) else "",
            self._format_time(int(a1.get("entry_time") or 0)),
            f"{profit1:.2f}",
            a2.get("symbol", ""),
            a2.get("lot", ""),
            f"{a2.get('entry_price', 0.0):.5f}" if isinstance(a2.get("entry_price"), (int, float)) else "",
            self._format_time(int(a2.get("entry_time") or 0)),
            f"{profit2:.2f}",
            side_label,
            f"{(profit1 + profit2):.2f}",
            "Close",
        ]

        self.table.add_row(
            trade_id,
            values,
            dynamic_indices={"p1": 5, "p2": 10, "combined": 12},
            close_callback=self._on_close_pair,
        )
        self.table.set_profits(trade_id, profit1, profit2, profit1 + profit2)

    def _load_state(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self._state_path):
            return {}
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as e:
            messagebox.showerror("Persistence Error", f"Failed to load saved state:\n{e}")
            return None

    def _save_state(self) -> None:
        data = {
            "trade_counter": self.trade_counter,
            "terminal_paths": {
                "terminal1": self.terminal1_var.get(),
                "terminal2": self.terminal2_var.get(),
            },
            "paired_trades": self.paired_trades,
        }

        try:
            os.makedirs(self._state_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=self._state_dir, prefix="state_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp_path, self._state_path)
        except Exception as e:
            messagebox.showerror("Persistence Error", f"Failed to save app state:\n{e}")

    def _normalize_trade_info(self, info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        for key in ("account1", "account2"):
            account = info.get(key)
            if not isinstance(account, dict):
                return None
            try:
                position = int(account.get("position", 0))
                magic = int(account.get("magic", 0))
            except Exception:
                return None
            if position <= 0:
                return None
            entry_price = account.get("entry_price")
            if isinstance(entry_price, (int, float)):
                ep = float(entry_price)
            else:
                ep = None
            entry_time_raw = account.get("entry_time") or 0
            try:
                entry_time = int(entry_time_raw)
            except Exception:
                entry_time = 0
            normalized[key] = {
                "symbol": account.get("symbol", ""),
                "lot": float(account.get("lot", 0.0)),
                "side": account.get("side", ""),
                "position": position,
                "magic": magic,
                "entry_price": ep,
                "entry_time": entry_time,
            }
        return normalized

    def _load_and_restore_state(self) -> None:
        state = self._load_state()
        if state is None:
            return

        try:
            self.trade_counter = max(int(state.get("trade_counter", 1)), 1)
        except Exception:
            self.trade_counter = 1

        terminals = state.get("terminal_paths") or {}
        path1 = terminals.get("terminal1") or self.terminal1_var.get()
        path2 = terminals.get("terminal2") or self.terminal2_var.get()
        if path1:
            self.terminal1_var.set(path1)
        if path2:
            self.terminal2_var.set(path2)

        saved_trades = state.get("paired_trades") or {}
        if not saved_trades:
            return

        if not path1 or not path2:
            messagebox.showwarning(
                "State Restore",
                "Saved trades were found but terminal paths were missing. They were not restored.",
            )
            return

        if not self._connect_workers(path1, path2, show_errors=False):
            messagebox.showerror(
                "State Restore",
                "Could not reconnect to both terminals using the saved paths. Saved trades could not be validated.",
            )
            return

        restored = 0
        dropped = []
        for trade_id, raw_info in saved_trades.items():
            normalized = self._normalize_trade_info(raw_info)
            if not normalized:
                dropped.append(trade_id)
                continue
            try:
                p1 = self.worker1.get_profit(normalized["account1"]["position"])
                p2 = self.worker2.get_profit(normalized["account2"]["position"])
            except Exception:
                dropped.append(trade_id)
                continue

            if not (p1.get("open") and p2.get("open")):
                dropped.append(trade_id)
                continue

            self.paired_trades[trade_id] = normalized
            profit1 = float(p1.get("profit", 0.0))
            profit2 = float(p2.get("profit", 0.0))
            self._add_trade_row(trade_id, normalized, profit1, profit2)
            restored += 1

        if dropped:
            messagebox.showinfo(
                "State Restore",
                f"Discarded {len(dropped)} saved trade(s) that were no longer open or were invalid.",
            )
        if restored:
            messagebox.showinfo(
                "State Restore",
                f"Restored {restored} paired trade(s) from the previous session.",
            )
        else:
            if not dropped:
                messagebox.showinfo(
                    "State Restore",
                    "Saved trades were found but none could be restored.",
                )

        # Persist normalized state (removes stale entries from disk)
        self._save_state()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    # Windows spawn safety
    from multiprocessing import freeze_support

    freeze_support()
    main()


