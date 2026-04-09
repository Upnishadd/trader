"""
Dashboard API Server
Serves the web dashboard and exposes REST endpoints for bot status/control.
Now with WebSocket support for real-time updates.
"""

import os
import json
import logging
from datetime import datetime

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from flask_socketio import SocketIO

logger = logging.getLogger("DashboardAPI")

app = Flask(__name__, static_folder="/app/dashboard/static")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")  # <-- enable WebSockets

bot_instance = None  # Set by main.py


@app.route("/")
def index():
    return send_from_directory("/app/dashboard/static", "index.html")


@app.route("/api/status")
def status():
    if bot_instance is None:
        return jsonify({"error": "Bot not initialized"}), 503
    return jsonify(bot_instance.get_status())


@app.route("/api/klines")
def klines():
    if bot_instance is None:
        return jsonify({"error": "Bot not initialized"}), 503
    try:
        symbol = request.args.get("symbol", bot_instance.symbol)
        interval = request.args.get("interval", "5m")
        limit = int(request.args.get("limit", 200))
        df = bot_instance.trader.get_klines(symbol, interval, limit=limit)
        records = []
        for ts, row in df.iterrows():
            records.append({
                "time": int(ts.timestamp() * 1000),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"]
            })
        return jsonify(records)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
def trades():
    if bot_instance is None:
        return jsonify([]), 503
    return jsonify(bot_instance.trade_history[-100:])


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    if bot_instance:
        bot_instance.stop()
    return jsonify({"status": "stopped"})


@app.route("/api/start", methods=["POST"])
def start_bot():
    if bot_instance and not bot_instance.running:
        bot_instance.start()
    return jsonify({"status": "started"})


@app.route("/api/close_position", methods=["POST"])
def close_position():
    if bot_instance and bot_instance.position:
        bot_instance._execute_sell(reason="manual")
        return jsonify({"status": "position closed"})
    return jsonify({"status": "no position"})


# ---------- WebSocket events ----------
@socketio.on("connect")
def handle_connect():
    logger.info("WebSocket client connected")
    if bot_instance:
        socketio.emit("status_update", bot_instance.get_status())


def emit_signal(signal: dict):
    """Call this in your bot whenever a new trade signal is generated"""
    socketio.emit("signal", signal)


def run_server(host="0.0.0.0", port=8080):
    logger.info(f"Dashboard server starting on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=False)
