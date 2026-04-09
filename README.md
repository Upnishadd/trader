# Kronos Trading Bot 🤖

An automated trading bot for Binance that uses the **Kronos foundation model** (K-line AI forecasting) to generate BUY/SELL signals, with a live web dashboard.

---

## ⚠️ Risk Disclaimer

Automated trading involves significant financial risk. This bot trades with **real money** on your Binance account. Past performance does not guarantee future results. Only deposit funds you can afford to lose. Start with small amounts until you understand the system.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Docker Container                                    │
│                                                      │
│   ┌─────────────┐    ┌──────────────────────────┐   │
│   │  Kronos AI  │───▶│   Trading Engine          │   │
│   │  (OHLCV     │    │   - Signal generation     │   │
│   │   forecast) │    │   - Risk management       │   │
│   └─────────────┘    │   - Order execution       │   │
│                      └──────────┬───────────────┘   │
│                                 │                    │
│                                 ▼                    │
│                      ┌──────────────────────────┐   │
│                      │  Binance API              │   │
│                      │  (Live trading)           │   │
│                      └──────────────────────────┘   │
│                                                      │
│   ┌─────────────────────────────────────────────┐   │
│   │  Web Dashboard  :8080                       │   │
│   │  - Live candlestick chart                   │   │
│   │  - P&L / balance stats                      │   │
│   │  - Trade history                            │   │
│   │  - Manual position close                    │   │
│   └─────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────┘
```

---

## Setup & Deployment

### 1. Get Binance API Keys

1. Log in to [Binance](https://www.binance.com)
2. Go to **Profile → API Management → Create API**
3. Enable: **Read Info** + **Spot & Margin Trading**
4. **Restrict to your server's IP address** for security
5. Copy the API Key and Secret

### 2. Configure the Bot

```bash
# Clone or copy this project to your server
cd kronos-bot

# Create your .env file
cp .env.example .env

# Edit .env with your credentials
nano .env
```

Your `.env` should contain:
```
BINANCE_API_KEY=your_actual_key
BINANCE_API_SECRET=your_actual_secret
```

### 3. Adjust Trading Config (Optional)

Edit `config/config.json` to tune behaviour:

```json
{
  "symbol": "BTCUSDT",          // Trading pair
  "interval": "5m",             // Candle interval for signals
  "loop_interval_seconds": 300, // How often the bot checks (5 min)
  "testnet": false,             // Set true to use Binance testnet first!
  "use_kronos_model": true,     // false = use simple EMA/RSI fallback

  "risk": {
    "max_position_pct": 0.30,   // Max 30% of balance per trade
    "stop_loss_pct": 0.02,      // Exit if down 2%
    "take_profit_pct": 0.04,    // Exit if up 4%
    "min_confidence": 0.50,     // Only trade if confidence >= 50%
    "max_daily_loss_pct": 0.05  // Stop trading if down 5% today
  }
}
```

### 4. Build and Run

```bash
# Build the Docker image (first run takes ~10 min downloading Kronos model)
docker compose build

# Start the bot
docker compose up -d

# View logs
docker compose logs -f
```

### 5. Open the Dashboard

Visit: **http://your-server-ip:8080**

---

## 🧪 Test with Binance Testnet First (Recommended)

1. Create a testnet account at https://testnet.binance.vision/
2. Get testnet API keys there
3. Set `"testnet": true` in `config/config.json`
4. Run with testnet keys — no real money at risk
5. Once satisfied, switch to live keys and set `testnet: false`

---

## How the Signal Logic Works

**With Kronos model enabled:**
1. Fetches last 400 5-min candles from Binance
2. Passes OHLCV data through Kronos tokenizer → transformer
3. Generates a 12-candle (1 hour) price forecast
4. If forecast shows >0.3% gain → BUY signal
5. If forecast shows <-0.3% loss → SELL signal

**Fallback (if model unavailable):**
- EMA(9) / EMA(21) crossover strategy
- RSI(14) overbought/oversold filter

**Risk controls (always active):**
- Stop-loss: -2% from entry
- Take-profit: +4% from entry
- Max 30% of balance per position
- 5% daily loss limit → bot pauses trading

---

## Stopping / Managing the Bot

```bash
# Stop the container (preserves state)
docker compose stop

# Remove container entirely
docker compose down

# Close open position via dashboard
# → Click "Close Position" button in the UI

# View trade history
cat data/state.json | python3 -m json.tool
```

---

## Project Structure

```
kronos-bot/
├── main.py                    # Entrypoint
├── bot/
│   ├── trading_engine.py      # Signal gen, risk mgmt, order execution
│   └── dashboard_api.py       # Flask REST API
├── dashboard/
│   └── static/index.html      # Web dashboard
├── config/
│   └── config.json            # Bot configuration
├── data/                      # Persisted trade state (auto-created)
├── logs/                      # Log files (auto-created)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```
