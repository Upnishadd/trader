"""
Kronos Trading Bot — Main Entry Point
"""

import os
import sys
import time
import logging
import threading

# Add project root to path
sys.path.insert(0, "/app")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/app/logs/bot.log")
    ]
)

logger = logging.getLogger("Main")


def main():
    logger.info("=" * 60)
    logger.info("  KRONOS TRADING BOT — Starting up")
    logger.info("=" * 60)

    # Validate environment variables
    required_env = ["BINANCE_API_KEY", "BINANCE_API_SECRET"]
    for var in required_env:
        if not os.environ.get(var):
            logger.critical(f"Missing required environment variable: {var}")
            sys.exit(1)

    # Import here after path setup
    from bot.trading_engine import TradingBot
    from bot.dashboard_api import app, run_server
    import bot.dashboard_api as api_module

    # Initialize bot
    bot = TradingBot(config_path="/app/config/config.json")
    api_module.bot_instance = bot

    # Start dashboard server in background thread
    dashboard_thread = threading.Thread(
        target=run_server,
        kwargs={"host": "0.0.0.0", "port": 8080},
        daemon=True
    )
    dashboard_thread.start()
    logger.info("Dashboard available at http://localhost:8080")

    # Start trading bot
    bot.start()

    logger.info("Bot running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        bot.stop()


if __name__ == "__main__":
    main()
