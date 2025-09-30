import os
import time
import traceback
from typing import Any, Dict, Optional, Tuple

try:
    import MetaTrader5 as MT5
except Exception as exc:  # pragma: no cover
    MT5 = None  # type: ignore

try:
    from win32com.client import Dispatch  # type: ignore
except Exception:  # pragma: no cover
    Dispatch = None  # type: ignore


def _resolve_terminal(path: str) -> Tuple[str, bool]:
    """Resolve terminal path and whether '/portable' should be used.

    Returns (executable_path, portable_flag).
    """
    if not path:
        return path, False
    path = os.path.expandvars(path)
    portable = False
    if path.lower().endswith(".lnk") and Dispatch is not None:
        try:
            shell = Dispatch("WScript.Shell")
            # CreateShortcut is the canonical method name
            shortcut = shell.CreateShortcut(path)
            target = shortcut.Targetpath
            args = (getattr(shortcut, "Arguments", "") or "")
            if isinstance(args, str) and "/portable" in args.lower():
                portable = True
            if target and os.path.exists(target):
                return target, portable
        except Exception:
            pass
    return path, portable


def _ensure_symbol_selected(symbol: str) -> Tuple[bool, Optional[str]]:
    info = MT5.symbol_info(symbol)
    if info is None:
        return False, f"Symbol not found: {symbol}"
    if not info.visible:
        if not MT5.symbol_select(symbol, True):
            return False, f"Failed to select symbol: {symbol}"
    return True, None


def _pick_filling_mode(symbol: str) -> int:
    info = MT5.symbol_info(symbol)
    if info is None:
        return MT5.ORDER_FILLING_FOK
    # Prefer the broker-supported filling mode if provided; fallback to FOK
    if getattr(info, "filling_mode", None) in (
        MT5.ORDER_FILLING_FOK,
        MT5.ORDER_FILLING_IOC,
        MT5.ORDER_FILLING_RETURN,
    ):
        return int(info.filling_mode)
    return MT5.ORDER_FILLING_FOK


def _order_send_with_filling(request_base: Dict[str, Any]):
    """Try sending with multiple filling modes to avoid 10030 errors.

    Returns (ok: bool, result_or_error: Any)
    """
    symbol = request_base.get("symbol")
    info = MT5.symbol_info(symbol)

    candidates = []
    # Start with symbol's declared filling mode if valid
    fm = getattr(info, "filling_mode", None) if info else None
    for mode in [fm, MT5.ORDER_FILLING_FOK, MT5.ORDER_FILLING_IOC, MT5.ORDER_FILLING_RETURN]:
        if mode in (MT5.ORDER_FILLING_FOK, MT5.ORDER_FILLING_IOC, MT5.ORDER_FILLING_RETURN):
            m = int(mode)
            if m not in candidates:
                candidates.append(m)

    last_error = None
    for mode in candidates:
        req = dict(request_base)
        req["type_filling"] = mode
        result = MT5.order_send(req)
        if result is None:
            last_error = {"error": "order_send returned None"}
            continue
        if result.retcode == MT5.TRADE_RETCODE_DONE:
            return True, result
        comment = getattr(result, "comment", "") or ""
        if int(result.retcode) in (10030, getattr(MT5, "TRADE_RETCODE_INVALID_FILL", 10031)) or "filling" in comment.lower():
            last_error = {"error": f"Unsupported filling mode {mode}: {result.retcode} {comment}"}
            continue
        # Different error - abort
        return False, {"error": f"Order rejected {result.retcode}: {comment}"}

    return False, last_error or {"error": "All filling modes rejected"}


def _find_position_ticket(symbol: str, magic: int, comment_substr: str, retries: int = 10) -> int:
    for _ in range(retries):
        positions = MT5.positions_get(symbol=symbol)
        if positions:
            for pos in positions:
                pos_comment = getattr(pos, "comment", "") or ""
                if int(getattr(pos, "magic", 0)) == int(magic) and comment_substr in pos_comment:
                    return int(pos.ticket)
        time.sleep(0.05)
    return 0


def _submit_market_order(
    symbol: str,
    side: str,
    volume: float,
    comment: str,
    magic: int,
    deviation: int = 20,
) -> Tuple[bool, Dict[str, Any]]:
    ok, err = _ensure_symbol_selected(symbol)
    if not ok:
        return False, {"error": err}

    tick = MT5.symbol_info_tick(symbol)
    if tick is None:
        return False, {"error": f"No tick data for symbol: {symbol}"}

    order_type = MT5.ORDER_TYPE_BUY if side.lower() == "buy" else MT5.ORDER_TYPE_SELL
    price = float(tick.ask) if order_type == MT5.ORDER_TYPE_BUY else float(tick.bid)

    request_base = {
        "action": MT5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": price,
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": comment,
        "type_time": MT5.ORDER_TIME_GTC,
    }

    ok_send, send_res = _order_send_with_filling(request_base)
    if not ok_send:
        return False, send_res
    result = send_res

    position_ticket = int(getattr(result, "position", 0) or 0)
    if position_ticket == 0:
        # Fallback: locate by comment and magic
        position_ticket = _find_position_ticket(symbol, magic, comment)

    if position_ticket == 0:
        return False, {"error": "Opened but failed to determine position ticket"}

    # Fetch position details for entry price/time
    entry_price = None
    entry_time = None
    commission = 0.0
    swap = 0.0
    try:
        pos_det = MT5.positions_get(ticket=int(position_ticket))
        if pos_det:
            pos0 = pos_det[0]
            entry_price = float(getattr(pos0, "price_open", 0.0) or 0.0)
            entry_time = int(getattr(pos0, "time", 0) or 0)
            commission = float(getattr(pos0, "commission", 0.0) or 0.0)
            swap = float(getattr(pos0, "swap", 0.0) or 0.0)
    except Exception:
        pass

    return True, {
        "position_ticket": position_ticket,
        "symbol": symbol,
        "side": side.lower(),
        "volume": float(volume),
        "entry_price": entry_price,
        "entry_time": entry_time,
        "commission": commission,
        "swap": swap,
    }


def _close_position_by_ticket(
    position_ticket: int,
    symbol: str,
    side: str,
    volume: float,
    magic: int,
    deviation: int = 20,
) -> Tuple[bool, Dict[str, Any]]:
    ok, err = _ensure_symbol_selected(symbol)
    if not ok:
        return False, {"error": err}

    tick = MT5.symbol_info_tick(symbol)
    if tick is None:
        return False, {"error": f"No tick data for symbol: {symbol}"}

    # Opposite side to close
    closing_type = MT5.ORDER_TYPE_SELL if side.lower() == "buy" else MT5.ORDER_TYPE_BUY
    price = float(tick.bid) if closing_type == MT5.ORDER_TYPE_SELL else float(tick.ask)

    request_base = {
        "action": MT5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": closing_type,
        "position": int(position_ticket),
        "price": price,
        "deviation": int(deviation),
        "magic": int(magic),
        "type_time": MT5.ORDER_TIME_GTC,
    }

    ok_send, result = _order_send_with_filling(request_base)
    if not ok_send:
        return False, result

    # Confirm the position is no longer open; retry briefly if required
    for _ in range(10):
        try:
            remaining = MT5.positions_get(ticket=int(position_ticket))
        except Exception:
            remaining = None
        if not remaining:
            return True, {"closed": True}
        time.sleep(0.1)

    return False, {"error": "Position still open after close attempt"}


def _get_profit_by_ticket(position_ticket: int) -> Tuple[bool, Dict[str, Any]]:
    positions = MT5.positions_get(ticket=int(position_ticket))
    if positions:
        pos = positions[0]
        return True, {
            "open": True,
            "profit": float(getattr(pos, "profit", 0.0) or 0.0),
            "volume": float(getattr(pos, "volume", 0.0) or 0.0),
            "entry_price": float(getattr(pos, "price_open", 0.0) or 0.0),
            "entry_time": int(getattr(pos, "time", 0) or 0),
            "commission": float(getattr(pos, "commission", 0.0) or 0.0),
            "swap": float(getattr(pos, "swap", 0.0) or 0.0),
        }
    return True, {"open": False, "profit": 0.0}


def _get_quote(symbol: str) -> Tuple[bool, Dict[str, Any]]:
    if not symbol:
        return False, {"error": "Symbol required"}
    ok, err = _ensure_symbol_selected(symbol)
    if not ok:
        return False, {"error": err}
    tick = MT5.symbol_info_tick(symbol)
    if tick is None:
        return False, {"error": f"No tick data for symbol: {symbol}"}
    bid = float(getattr(tick, "bid", 0.0) or 0.0)
    ask = float(getattr(tick, "ask", 0.0) or 0.0)
    return True, {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "spread": max(0.0, ask - bid),
        "time": int(getattr(tick, "time", 0) or 0),
    }


def _get_account_overview() -> Tuple[bool, Dict[str, Any]]:
    info = MT5.account_info()
    if info is None:
        return False, {"error": "Account not available"}
    return True, {
        "balance": float(getattr(info, "balance", 0.0) or 0.0),
        "equity": float(getattr(info, "equity", 0.0) or 0.0),
        "margin": float(getattr(info, "margin", 0.0) or 0.0),
        "login": int(getattr(info, "login", 0) or 0),
    }


def worker_main(request_queue, response_queue, terminal_path: Optional[str] = None, label: str = "") -> None:
    """Worker process entrypoint. One worker per MT5 terminal.

    Communications protocol:
      Req: {id, cmd, params}
      Res: {id, status: 'ok'|'error', data?, error?}
    """
    def respond(req_id: str, status: str, data: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
        response_queue.put({"id": req_id, "status": status, "data": data, "error": error})

    try:
        if MT5 is None:
            raise RuntimeError("MetaTrader5 module not available. Install 'MetaTrader5'.")

        connected = False
        resolved_path, resolved_portable = _resolve_terminal(terminal_path or "")

        while True:
            req = request_queue.get()
            if req is None:
                break
            req_id = req.get("id", "")
            cmd = (req.get("cmd") or "").lower()
            params = req.get("params") or {}

            try:
                if cmd == "connect":
                    path_arg, portable_flag = _resolve_terminal(params.get("path") or resolved_path)
                    if not path_arg:
                        respond(req_id, "error", error="Terminal path is required")
                        continue
                    if not os.path.exists(path_arg):
                        respond(req_id, "error", error=f"Terminal executable not found: {path_arg}")
                        continue
                    # Try initialize with portable flag if supported, then fallback
                    ok = False
                    last_err = None
                    try:
                        ok = MT5.initialize(path=path_arg, portable=bool(portable_flag))
                    except TypeError:
                        ok = MT5.initialize(path=path_arg)
                    if not ok:
                        try:
                            last_err = MT5.last_error()
                        except Exception:
                            last_err = None
                        # One more attempt toggling portable in case of data dir conflict
                        try:
                            MT5.shutdown()
                        except Exception:
                            pass
                        try:
                            ok = MT5.initialize(path=path_arg, portable=not bool(portable_flag))
                        except TypeError:
                            ok = MT5.initialize(path=path_arg)
                    if not ok:
                        try:
                            last_err = MT5.last_error()
                        except Exception:
                            pass
                        respond(
                            req_id,
                            "error",
                            error=f"initialize() failed for: {path_arg} | last_error={last_err}",
                        )
                        continue
                    # Validate account session
                    acc = MT5.account_info()
                    if acc is None or int(getattr(acc, "login", 0) or 0) == 0:
                        try:
                            err = MT5.last_error()
                        except Exception:
                            err = None
                        respond(
                            req_id,
                            "error",
                            error=(
                                "Terminal started but not logged in. Please open the terminal, log in to the account, then try Connect again. "
                                f"details last_error={err}"
                            ),
                        )
                        continue

                    connected = True
                    ver = MT5.version()
                    respond(
                        req_id,
                        "ok",
                        data={
                            "connected": True,
                            "version": ver,
                            "path": path_arg,
                            "portable": bool(portable_flag),
                            "login": int(getattr(acc, "login", 0) or 0),
                            "server": getattr(acc, "server", ""),
                        },
                    )

                elif cmd in ("buy", "sell"):
                    if not connected:
                        respond(req_id, "error", error="Not connected")
                        continue
                    symbol = params.get("symbol")
                    volume = float(params.get("volume", 0))
                    pair_id = str(params.get("pair_id"))
                    magic = int(params.get("magic", 0))
                    deviation = int(params.get("deviation", 20))
                    if not symbol or volume <= 0:
                        respond(req_id, "error", error="Invalid symbol or volume")
                        continue
                    ok, data = _submit_market_order(
                        symbol=symbol,
                        side=cmd,
                        volume=volume,
                        comment=f"PAIR:{pair_id}",
                        magic=magic,
                        deviation=deviation,
                    )
                    if not ok:
                        respond(req_id, "error", error=str(data.get("error")))
                    else:
                        respond(req_id, "ok", data=data)

                elif cmd == "get_profit":
                    position_ticket = int(params.get("position_ticket", 0))
                    ok, data = _get_profit_by_ticket(position_ticket)
                    if not ok:
                        respond(req_id, "error", error=str(data.get("error")))
                    else:
                        respond(req_id, "ok", data=data)

                elif cmd == "get_quote":
                    symbol = params.get("symbol")
                    ok, data = _get_quote(str(symbol or ""))
                    if not ok:
                        respond(req_id, "error", error=str(data.get("error")))
                    else:
                        respond(req_id, "ok", data=data)

                elif cmd == "get_account_info":
                    ok, data = _get_account_overview()
                    if not ok:
                        respond(req_id, "error", error=str(data.get("error")))
                    else:
                        respond(req_id, "ok", data=data)

                elif cmd == "close":
                    if not connected:
                        respond(req_id, "error", error="Not connected")
                        continue
                    position_ticket = int(params.get("position_ticket", 0))
                    symbol = params.get("symbol")
                    side = params.get("side")
                    volume = float(params.get("volume", 0))
                    magic = int(params.get("magic", 0))
                    deviation = int(params.get("deviation", 20))
                    if position_ticket <= 0 or not symbol or volume <= 0:
                        respond(req_id, "error", error="Invalid close params")
                        continue
                    ok, data = _close_position_by_ticket(
                        position_ticket=position_ticket,
                        symbol=symbol,
                        side=str(side or ""),
                        volume=volume,
                        magic=magic,
                        deviation=deviation,
                    )
                    if not ok:
                        respond(req_id, "error", error=str(data.get("error")))
                    else:
                        respond(req_id, "ok", data=data)

                elif cmd == "shutdown":
                    respond(req_id, "ok", data={"shutdown": True})
                    break

                else:
                    respond(req_id, "error", error=f"Unknown cmd: {cmd}")

            except Exception as e:  # pragma: no cover
                respond(req_id, "error", error=f"{e}\n{traceback.format_exc()}")

    finally:
        try:
            if MT5 is not None:
                MT5.shutdown()
        except Exception:
            pass


