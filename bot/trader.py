"""
Kronos Trading Bot - Core Trading Engine
Connects Kronos model forecasts to Binance live trading.

Strategy:
  - Fetch recent OHLCV candles from Binance
  - Run Kronos probabilistic forecast (sample_count=3, averaged)
  - forecast close > current close by buy_threshold  -> BUY
  - forecast close < current close by sell_threshold -> SELL
  - Hard stop-loss at stop_loss_pct from entry
  - Max 15% of USDT balance per position
"""

import logging
import asyncio
from datetime import datetime, timedelta
import pandas as pd

logger = logging.getLogger("kronos.trader")


class KronosTradingBot:
    def __init__(self, config, predictor, state_manager, binance_client):
        self.config       = config
        self.predictor    = predictor
        self.state        = state_manager
        self.client       = binance_client

        self.pairs            = config.get("pairs", ["BTCUSDT"])
        self.interval         = config.get("interval", "5m")
        self.lookback         = config.get("lookback", 400)
        self.pred_len         = config.get("pred_len", 24)
        self.buy_threshold    = config.get("buy_threshold", 0.005)
        self.sell_threshold   = config.get("sell_threshold", -0.003)
        self.max_position_pct = config.get("max_position_pct", 0.15)
        self.stop_loss_pct    = config.get("stop_loss_pct", -0.03)
        self.poll_interval    = config.get("poll_interval_seconds", 300)
        self.min_notional     = config.get("min_notional_usdt", 10.0)

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    async def run(self):
        logger.info("Kronos Trading Bot starting up")
        self.state.set_status("running")
        while True:
            try:
                await self._cycle()
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
                self.state.log_event("error", str(e))
            await asyncio.sleep(self.poll_interval)

    async def _cycle(self):
        logger.info(f"--- Cycle {datetime.utcnow().isoformat()} ---")
        balance_usdt = await self._get_usdt_balance()
        self.state.update_balance(balance_usdt)
        logger.info(f"USDT balance: {balance_usdt:.2f}")

        for pair in self.pairs:
            try:
                await self._process_pair(pair, balance_usdt)
            except Exception as e:
                logger.error(f"Error processing {pair}: {e}", exc_info=True)

    async def _process_pair(self, pair: str, balance_usdt: float):
        # 1. Fetch candles
        df, timestamps = await self._fetch_candles(pair)
        if df is None or len(df) < 50:
            logger.warning(f"Insufficient data for {pair}")
            return

        current_close = float(df["close"].iloc[-1])
        self.state.update_price(pair, current_close)

        # 2. Kronos forecast
        forecast_df = await self._run_forecast(df, timestamps)
        if forecast_df is None:
            return

        forecast_close = float(forecast_df["close"].iloc[-1])
        pct_change     = (forecast_close - current_close) / current_close
        logger.info(f"{pair} | cur={current_close:.4f} | fcast={forecast_close:.4f} | Δ={pct_change*100:.2f}%")

        self.state.update_forecast(pair, {
            "current":    current_close,
            "forecast":   forecast_close,
            "pct_change": pct_change,
            "timestamp":  datetime.utcnow().isoformat(),
        })

        # 3. Stop-loss check on open position
        position = self.state.get_position(pair)
        if position:
            unrealised = (current_close - position["entry_price"]) / position["entry_price"]
            if unrealised <= self.stop_loss_pct:
                logger.warning(f"Stop-loss triggered {pair} ({unrealised*100:.2f}%)")
                await self._close_position(pair, position, current_close, "stop_loss")
                return

        # 4. Signal logic
        if pct_change >= self.buy_threshold and not position:
            usdt = balance_usdt * self.max_position_pct
            if usdt >= self.min_notional:
                await self._open_long(pair, usdt, current_close, pct_change)
            else:
                logger.info(f"Skip {pair}: notional {usdt:.2f} < min {self.min_notional}")

        elif pct_change <= self.sell_threshold and position:
            await self._close_position(pair, position, current_close, "signal")

        else:
            logger.info(f"{pair}: Hold (no action)")

    # ------------------------------------------------------------------ #
    # Binance helpers                                                       #
    # ------------------------------------------------------------------ #

    async def _get_usdt_balance(self) -> float:
        try:
            account = await asyncio.get_event_loop().run_in_executor(
                None, self.client.get_account)
            for asset in account["balances"]:
                if asset["asset"] == "USDT":
                    return float(asset["free"])
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
        return 0.0

    async def _fetch_candles(self, pair: str):
        try:
            klines = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.client.get_klines(
                    symbol=pair, interval=self.interval, limit=self.lookback))
            rows, ts = [], []
            for k in klines:
                ts.append(pd.Timestamp(k[0], unit="ms"))
                rows.append({
                    "open":   float(k[1]),
                    "high":   float(k[2]),
                    "low":    float(k[3]),
                    "close":  float(k[4]),
                    "volume": float(k[5]),
                    "amount": float(k[7]),
                })
            return pd.DataFrame(rows), pd.Series(ts)
        except Exception as e:
            logger.error(f"Candle fetch error {pair}: {e}")
            return None, None

    async def _run_forecast(self, df: pd.DataFrame, timestamps: pd.Series):
        try:
            last_ts   = timestamps.iloc[-1]
            freq_min  = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(self.interval, 5)
            future_ts = pd.Series([
                last_ts + timedelta(minutes=freq_min * (i + 1))
                for i in range(self.pred_len)
            ])
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: self.predictor.predict(
                df=df, x_timestamp=timestamps, y_timestamp=future_ts,
                pred_len=self.pred_len, T=1.0, top_p=0.9, sample_count=3))
        except Exception as e:
            logger.error(f"Forecast error: {e}")
            return None

    async def _open_long(self, pair: str, usdt_amount: float, price: float, pct_change: float):
        qty = self._round_qty(pair, usdt_amount / price)
        if qty <= 0:
            return
        logger.info(f"BUY {pair} qty={qty} ~${price:.4f}")
        try:
            order = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.client.order_market_buy(symbol=pair, quantity=qty))
            fills        = order.get("fills", [{}])
            filled_price = float(fills[0].get("price", price)) if fills else price
            trade = {
                "pair": pair, "side": "BUY", "qty": qty,
                "entry_price": filled_price, "usdt_spent": usdt_amount,
                "order_id": order.get("orderId"),
                "timestamp": datetime.utcnow().isoformat(),
                "forecast_pct": pct_change,
            }
            self.state.open_position(pair, trade)
            self.state.log_trade(trade)
            logger.info(f"BUY filled: {trade}")
        except Exception as e:
            logger.error(f"BUY failed {pair}: {e}")

    async def _close_position(self, pair: str, position: dict, price: float, reason: str):
        qty = position["qty"]
        logger.info(f"SELL {pair} qty={qty} ~${price:.4f} reason={reason}")
        try:
            order = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.client.order_market_sell(symbol=pair, quantity=qty))
            fills        = order.get("fills", [{}])
            filled_price = float(fills[0].get("price", price)) if fills else price
            pnl = (filled_price - position["entry_price"]) * qty
            trade = {
                "pair": pair, "side": "SELL", "qty": qty,
                "exit_price": filled_price, "entry_price": position["entry_price"],
                "pnl_usdt": pnl, "reason": reason,
                "order_id": order.get("orderId"),
                "timestamp": datetime.utcnow().isoformat(),
            }
            self.state.close_position(pair)
            self.state.log_trade(trade)
            logger.info(f"SELL filled: PnL={pnl:.4f} USDT")
        except Exception as e:
            logger.error(f"SELL failed {pair}: {e}")

    def _round_qty(self, pair: str, qty: float) -> float:
        steps = {
            "BTCUSDT": 0.00001, "ETHUSDT": 0.0001,
            "BNBUSDT": 0.001,   "SOLUSDT": 0.01, "XRPUSDT": 0.1,
        }
        step = steps.get(pair, 0.001)
        return float(int(qty / step) * step)
