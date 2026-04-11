"""
Kronos Trading Engine — Binance Live Trading
Uses Kronos foundation model for K-line forecasting and executes trades on Binance.
"""

import os
import time
import json
import logging
import threading
import traceback
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger("TradingEngine")


class KronosSignalGenerator:
    """
    Wraps the Kronos model to produce BUY/SELL/HOLD signals.
    Falls back to a simple momentum strategy if model is unavailable.
    """

    def __init__(self, use_model: bool = True):
        self.model = None
        self.tokenizer = None
        self.predictor = None
        self.use_model = use_model
        self._load_model()

    def _load_model(self):
        if not self.use_model:
            logger.info("Model disabled — using momentum fallback strategy")
            return
        try:
            from model import Kronos, KronosTokenizer, KronosPredictor
            logger.info("Loading Kronos tokenizer...")
            self.tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
            logger.info("Loading Kronos-small model...")
            self.model = Kronos.from_pretrained("NeoQuasar/Kronos-base")
            self.predictor = KronosPredictor(self.model, self.tokenizer, max_context=512)
            logger.info("Kronos model loaded successfully")
        except Exception as e:
            logger.warning(f"Kronos model unavailable ({e}) — falling back to momentum strategy")
            traceback.print_exc()  # <-- Print full error
            self.predictor = None
    def generate_signal(self, df: pd.DataFrame, symbol: str, pred_len: int = 12) -> dict:
        """
        Generate a trading signal from recent OHLCV data.
        Returns: {signal: BUY/SELL/HOLD, confidence: float, forecast_close: float}
        """
        if df is None or len(df) < 50:
            return {"signal": "HOLD", "confidence": 0.0, "forecast_close": None, "method": "insufficient_data"}

        if self.predictor is not None:
            return self._kronos_signal(df, pred_len)
        else:
            return self._momentum_signal(df)

    def _kronos_signal(self, df: pd.DataFrame, pred_len: int) -> dict:
        """Use Kronos model to forecast and derive signal."""
        try:
            lookback = min(400, len(df))
            x_df = df.tail(lookback)[['open', 'high', 'low', 'close', 'volume']].copy()
            x_df.columns = ['open', 'high', 'low', 'close', 'volume']

            now = df.index[-1]
            freq = pd.infer_freq(df.index) or '5min'
            x_timestamp = x_df.index.to_series()
            y_timestamp = pd.date_range(start=now, periods=pred_len + 1, freq=freq)[1:]

            pred_df = self.predictor.predict(
                df=x_df.reset_index(drop=True),
                x_timestamp=x_timestamp.reset_index(drop=True),
                y_timestamp=pd.Series(y_timestamp),
                pred_len=pred_len,
                T=0.8,
                top_p=0.9,
                sample_count=3
            )

            current_close = float(df['close'].iloc[-1])
            forecast_close = float(pred_df['close'].iloc[-1])
            pct_change = (forecast_close - current_close) / current_close * 100

            if pct_change > 0.05:
                signal = "BUY"
                confidence = min(abs(pct_change) / 0.1, 1.0)
            elif pct_change < -0.05:
                signal = "SELL"
                confidence = min(abs(pct_change) / 0.1, 1.0)
            else:
                signal = "HOLD"
                confidence = 0.3

            return {
                "signal": signal,
                "confidence": confidence,
                "forecast_close": forecast_close,
                "pct_change": pct_change,
                "method": "kronos"
            }
        except Exception as e:
            logger.error(f"Kronos signal error: {e}")
            return self._momentum_signal(df)

    def _momentum_signal(self, df: pd.DataFrame) -> dict:
        """Simple EMA crossover + RSI fallback strategy."""
        close = df['close'].astype(float)
        ema_fast = close.ewm(span=9).mean()
        ema_slow = close.ewm(span=21).mean()

        # RSI
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(span=14).mean()
        loss = (-delta.clip(upper=0)).ewm(span=14).mean()
        rs = gain / (loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))

        current_rsi = float(rsi.iloc[-1])
        cross_up = float(ema_fast.iloc[-1]) > float(ema_slow.iloc[-1]) and \
                   float(ema_fast.iloc[-2]) <= float(ema_slow.iloc[-2])
        cross_down = float(ema_fast.iloc[-1]) < float(ema_slow.iloc[-1]) and \
                     float(ema_fast.iloc[-2]) >= float(ema_slow.iloc[-2])

        if cross_up and current_rsi < 70:
            signal = "BUY"
            confidence = 0.6
        elif cross_down and current_rsi > 30:
            signal = "SELL"
            confidence = 0.6
        elif current_rsi < 30:
            signal = "BUY"
            confidence = 0.5
        elif current_rsi > 70:
            signal = "SELL"
            confidence = 0.5
        else:
            signal = "HOLD"
            confidence = 0.3

        return {
            "signal": signal,
            "confidence": confidence,
            "forecast_close": None,
            "rsi": current_rsi,
            "method": "momentum_ema_rsi"
        }


class RiskManager:
    """Position sizing and risk controls for small account."""

    def __init__(self, config: dict):
        self.max_position_pct = config.get("max_position_pct", 0.30)   # max 30% per trade
        self.stop_loss_pct = config.get("stop_loss_pct", 0.02)          # 2% stop loss
        self.take_profit_pct = config.get("take_profit_pct", 0.04)      # 4% take profit
        self.min_confidence = config.get("min_confidence", 0.5)
        self.max_daily_loss_pct = config.get("max_daily_loss_pct", 0.05) # 5% daily loss limit
        self.daily_loss = 0.0
        self.daily_reset_date = datetime.now(timezone.utc).date()
        self.min_notional = 5.0  # Default, updated by TradingBot
        self.fee_buffer = 0.002  # 0.2% buffer to cover Binance fees (0.1%) and slippage

    def reset_daily_if_needed(self):
        today = datetime.now(timezone.utc).date()
        if today != self.daily_reset_date:
            self.daily_loss = 0.0
            self.daily_reset_date = today

    def can_trade(self, signal: dict, balance_usdt: float) -> bool:
        self.reset_daily_if_needed()
        if signal["confidence"] < self.min_confidence:
            logger.info(f"Signal confidence {signal['confidence']:.2f} below threshold {self.min_confidence}")
            return False
        if self.daily_loss >= self.max_daily_loss_pct * balance_usdt:
            logger.warning("Daily loss limit reached — no new trades today")
            return False
        if balance_usdt < self.min_notional:
            logger.warning(f"Balance too low: ${balance_usdt:.2f} USDT")
            return False
        return True

    def position_size_usdt(self, balance_usdt: float, confidence: float) -> float:
        """Size position based on confidence and risk limits."""
        # Apply a fee buffer so we don't exceed available balance
        usable_balance = balance_usdt * (1 - self.fee_buffer)
        base_size = usable_balance * self.max_position_pct
        sized = base_size * confidence
        return max(self.min_notional, min(sized, usable_balance * self.max_position_pct))

    def record_loss(self, amount: float):
        self.daily_loss += abs(amount)


class BinanceTrader:
    """Handles all Binance API interactions."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        from binance.client import Client
        self.client = Client(api_key, api_secret, testnet=testnet)
        self.testnet = testnet
        if testnet:
            self.client.API_URL = 'https://testnet.binance.vision/api'
        logger.info(f"Binance client initialized (testnet={testnet})")

    def get_balance(self, asset: str = "USDT") -> float:
        info = self.client.get_asset_balance(asset=asset)
        return float(info['free']) if info else 0.0

    def get_symbol_info(self, symbol: str) -> dict:
        info = self.client.get_symbol_info(symbol)
        filters = {f['filterType']: f for f in info['filters']}
        return {
            "min_qty": float(filters['LOT_SIZE']['minQty']),
            "step_size": float(filters['LOT_SIZE']['stepSize']),
            "min_notional": float(filters.get('MIN_NOTIONAL', {}).get('minNotional', 5.0)),
        }

    def get_current_price(self, symbol: str) -> float:
        ticker = self.client.get_symbol_ticker(symbol=symbol)
        return float(ticker['price'])

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 500) -> pd.DataFrame:
        klines = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df[['open', 'high', 'low', 'close', 'volume']]

    def round_qty(self, qty: float, step_size: float) -> float:
        precision = len(str(step_size).rstrip('0').split('.')[-1])
        return round(qty // step_size * step_size, precision)

    def place_market_buy(self, symbol: str, usdt_amount: float) -> Optional[dict]:
        try:
            price = self.get_current_price(symbol)
            info = self.get_symbol_info(symbol)
            qty = self.round_qty(usdt_amount / price, info['step_size'])
            if qty < info['min_qty']:
                logger.warning(f"Qty {qty} below min {info['min_qty']}")
                return None
            order = self.client.order_market_buy(symbol=symbol, quantity=qty)
            logger.info(f"BUY ORDER: {qty} {symbol} @ ~${price:.2f}")
            return order
        except Exception as e:
            logger.error(f"Buy order failed: {e}")
            return None

    def place_market_sell(self, symbol: str, qty: float) -> Optional[dict]:
        try:
            order = self.client.order_market_sell(symbol=symbol, quantity=qty)
            logger.info(f"SELL ORDER: {qty} {symbol}")
            return order
        except Exception as e:
            logger.error(f"Sell order failed: {e}")
            return None

    def get_open_orders(self, symbol: str) -> list:
        return self.client.get_open_orders(symbol=symbol)


class TradingBot:
    """Main orchestrator: fetches data, generates signals, executes trades."""

    def __init__(self, config_path: str = "/app/config/config.json"):
        with open(config_path) as f:
            self.config = json.load(f)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("/app/logs/bot.log")
            ]
        )

        self.symbol = self.config["symbol"]
        self.interval = self.config.get("interval", "5m")
        self.sleep_seconds = self.config.get("loop_interval_seconds", 300)

        self.trader = BinanceTrader(
            api_key=os.environ["BINANCE_API_KEY"],
            api_secret=os.environ["BINANCE_API_SECRET"],
            testnet=self.config.get("testnet", False)
        )

        use_model = self.config.get("use_kronos_model", True)
        self.signal_gen = KronosSignalGenerator(use_model=use_model)
        self.risk = RiskManager(self.config.get("risk", {}))

        # Initialize min_notional from Binance
        try:
            symbol_info = self.trader.get_symbol_info(self.symbol)
            self.risk.min_notional = symbol_info.get("min_notional", 5.0)
            logger.info(f"RiskManager min_notional set to {self.risk.min_notional}")
        except Exception as e:
            logger.warning(f"Could not fetch min_notional from Binance, using default: {e}")

        self.position = None   # {"qty": float, "entry_price": float, "entry_time": str}
        self.trade_history = []
        self._last_balance = 0.0
        self._last_price = 0.0
        self._last_asset_balance = 0.0
        self.state_file = "/app/data/state.json"
        self._load_state()

        self._lock = threading.Lock()
        self.running = False
        logger.info(f"TradingBot initialized — {self.symbol} @ {self.interval}")

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    state = json.load(f)
                self.position = state.get("position")
                self.trade_history = state.get("trade_history", [])
                logger.info(f"State loaded: position={self.position}")
            except Exception as e:
                logger.warning(f"Could not load state: {e}")

    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump({
                "position": self.position,
                "trade_history": self.trade_history,
                "last_update": datetime.now(timezone.utc).isoformat()
            }, f, indent=2)

    def _check_stop_loss_take_profit(self, current_price: float) -> Optional[str]:
        """Returns 'stop_loss' or 'take_profit' if triggered, else None."""
        if not self.position:
            return None
        entry = self.position["entry_price"]
        pct = (current_price - entry) / entry
        if pct <= -self.risk.stop_loss_pct:
            return "stop_loss"
        if pct >= self.risk.take_profit_pct:
            return "take_profit"
        return None

    def _execute_buy(self, signal: dict, balance_usdt: float):
        size_usdt = self.risk.position_size_usdt(balance_usdt, signal["confidence"])
        order = self.trader.place_market_buy(self.symbol, size_usdt)
        if order:
            # Use the actual quantity filled by Binance (after fees)
            qty = sum(float(fill['qty']) for fill in order.get('fills', []))
            price = sum(float(fill['price']) * float(fill['qty']) for fill in order.get('fills', [])) / qty if qty > 0 else self.trader.get_current_price(self.symbol)
            self.position = {
                "qty": qty,
                "entry_price": price,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "order_id": order.get('orderId')
            }
            self._record_trade("BUY", price, qty, signal)
            self._save_state()

    def _execute_sell(self, reason: str = "signal"):
        if not self.position:
            return

        # Get actual asset balance from Binance to avoid "Insufficient Balance" errors due to fees/rounding
        asset = self.symbol.replace("USDT", "")
        actual_balance = self.trader.get_balance(asset)
        qty = min(self.position["qty"], actual_balance)
        
        # Ensure we meet the exchange's step size requirements
        info = self.trader.get_symbol_info(self.symbol)
        qty = self.trader.round_qty(qty, info['step_size'])

        order = self.trader.place_market_sell(self.symbol, qty)
        if order:
            price = self.trader.get_current_price(self.symbol)
            pnl = (price - self.position["entry_price"]) * qty
            if pnl < 0:
                self.risk.record_loss(abs(pnl))
            self._record_trade("SELL", price, qty, {"signal": "SELL", "reason": reason, "pnl": pnl})
            self.position = None
            self._save_state()
            logger.info(f"Position closed ({reason}): PnL = ${pnl:.4f}")

    def _record_trade(self, side: str, price: float, qty: float, meta: dict):
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "side": side,
            "price": price,
            "qty": qty,
            "value_usdt": price * qty,
            "meta": meta
        }
        self.trade_history.append(record)
        # Keep last 500 trades in memory
        if len(self.trade_history) > 500:
            self.trade_history = self.trade_history[-500:]

    def get_status(self) -> dict:
        """Returns current bot status for dashboard API."""
        with self._lock:
            try:
                balance_usdt = self._last_balance
                price = self._last_price
                position_value = 0.0
                unrealized_pnl = 0.0
                if self.position:
                    position_value = self.position["qty"] * price
                    unrealized_pnl = (price - self.position["entry_price"]) * self.position["qty"]
                return {
                    "running": self.running,
                    "symbol": self.symbol,
                    "asset_name": self.symbol.replace("USDT", ""),
                    "current_price": price,
                    "balance_usdt": balance_usdt,
                    "position": self.position,
                    "position_value": position_value,
                    "unrealized_pnl": unrealized_pnl,
                    "trade_history": self.trade_history[-50:],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "actual_asset_balance": self._last_asset_balance
                }
            except Exception as e:
                return {"error": str(e), "running": self.running}

    def loop(self):
        """Main trading loop."""
        logger.info("Trading loop started")
        self.running = True
        while self.running:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
            time.sleep(self.sleep_seconds)

    def _tick(self):
        logger.info(f"--- Tick {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} ---")
        try:
            df = self.trader.get_klines(self.symbol, self.interval, limit=500)
            current_price = float(df['close'].iloc[-1])
            self._last_price = current_price

            # Check stop-loss / take-profit first
            exit_reason = self._check_stop_loss_take_profit(current_price)
            if exit_reason:
                logger.info(f"Triggered: {exit_reason} at ${current_price:.2f}")
                self._execute_sell(reason=exit_reason)
                return

            signal = self.signal_gen.generate_signal(df, self.symbol)
            logger.info(f"Signal: {signal['signal']} | Confidence: {signal['confidence']:.2f} | Method: {signal['method']} | PctChange: {signal.get('pct_change', 'N/A')}")

            balance_usdt = self.trader.get_balance("USDT")
            self._last_balance = balance_usdt
            
            asset = self.symbol.replace("USDT", "")
            self._last_asset_balance = self.trader.get_balance(asset)
            logger.info(f"Balance: ${balance_usdt:.2f} USDT | Position: {self.position}")

            if signal["signal"] == "BUY" and self.position is None:
                if self.risk.can_trade(signal, balance_usdt):
                    self._execute_buy(signal, balance_usdt)

            elif signal["signal"] == "SELL" and self.position is not None:
                self._execute_sell(reason="signal")
        except Exception as e:
            logger.error(f"Error during tick execution: {e}")
            # Do not stop the bot, just wait for the next interval

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        logger.info("Bot stopped")
