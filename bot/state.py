"""
State Manager - Thread-safe in-memory state shared between the bot and the dashboard API.
"""
import json
import threading
import logging
from datetime import datetime
from collections import deque
from pathlib import Path

logger = logging.getLogger("kronos.state")

TRADES_FILE = Path("/app/data/trades.json")
TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)


class StateManager:
    def __init__(self, initial_balance: float = 0.0):
        self._lock = threading.Lock()
        self._status       = "stopped"
        self._balance      = initial_balance
        self._start_balance= initial_balance
        self._prices       = {}       # pair -> float
        self._forecasts    = {}       # pair -> dict
        self._positions    = {}       # pair -> trade dict
        self._trades       = self._load_trades()
        self._events       = deque(maxlen=200)

    # ---- Status --------------------------------------------------------

    def set_status(self, status: str):
        with self._lock:
            self._status = status
            self._log_event_internal("status", status)

    def get_status(self) -> str:
        with self._lock:
            return self._status

    # ---- Balance -------------------------------------------------------

    def update_balance(self, balance: float):
        with self._lock:
            self._balance = balance

    def get_balance(self) -> float:
        with self._lock:
            return self._balance

    # ---- Prices / Forecasts -------------------------------------------

    def update_price(self, pair: str, price: float):
        with self._lock:
            self._prices[pair] = price

    def update_forecast(self, pair: str, data: dict):
        with self._lock:
            self._forecasts[pair] = data

    # ---- Positions -----------------------------------------------------

    def get_position(self, pair: str):
        with self._lock:
            return self._positions.get(pair)

    def open_position(self, pair: str, trade: dict):
        with self._lock:
            self._positions[pair] = trade

    def close_position(self, pair: str):
        with self._lock:
            self._positions.pop(pair, None)

    # ---- Trades --------------------------------------------------------

    def log_trade(self, trade: dict):
        with self._lock:
            self._trades.append(trade)
            self._save_trades_internal()
            self._log_event_internal("trade", json.dumps(trade))

    def get_trades(self):
        with self._lock:
            return list(self._trades)

    # ---- Events --------------------------------------------------------

    def log_event(self, kind: str, message: str):
        with self._lock:
            self._log_event_internal(kind, message)

    def _log_event_internal(self, kind: str, message: str):
        self._events.append({
            "kind": kind,
            "message": message,
            "timestamp": datetime.utcnow().isoformat(),
        })

    # ---- Snapshot for dashboard ----------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            trades = list(self._trades)
            total_pnl = sum(t.get("pnl_usdt", 0) for t in trades if t.get("side") == "SELL")
            wins  = sum(1 for t in trades if t.get("side") == "SELL" and t.get("pnl_usdt", 0) > 0)
            total_closed = sum(1 for t in trades if t.get("side") == "SELL")
            win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

            return {
                "status":      self._status,
                "balance":     round(self._balance, 4),
                "total_pnl":   round(total_pnl, 4),
                "win_rate":    round(win_rate, 1),
                "trade_count": total_closed,
                "prices":      dict(self._prices),
                "forecasts":   dict(self._forecasts),
                "positions":   dict(self._positions),
                "recent_trades": list(reversed(trades[-50:])),
                "events":      list(reversed(list(self._events)[-30:])),
            }

    # ---- Persistence ---------------------------------------------------

    def _load_trades(self):
        try:
            if TRADES_FILE.exists():
                data = json.loads(TRADES_FILE.read_text())
                logger.info(f"Loaded {len(data)} historical trades")
                return data
        except Exception as e:
            logger.warning(f"Could not load trades: {e}")
        return []

    def _save_trades_internal(self):
        try:
            TRADES_FILE.write_text(json.dumps(self._trades, indent=2))
        except Exception as e:
            logger.warning(f"Could not save trades: {e}")
