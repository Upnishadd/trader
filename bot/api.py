"""
Dashboard API - FastAPI backend serving state via REST + SSE.
"""
import json, asyncio, logging
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("kronos.api")
_state = None

def create_app(state_manager):
    global _state
    _state = state_manager
    app = FastAPI(title="Kronos Bot Dashboard")
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/snapshot")
    async def snapshot():
        return _state.snapshot()

    @app.get("/api/trades")
    async def trades():
        return {"trades": _state.get_trades()}

    @app.get("/api/stream")
    async def stream():
        async def gen():
            while True:
                try:
                    data = json.dumps(_state.snapshot())
                    yield f"data: {data}\n\n"
                except Exception as e:
                    logger.error(f"SSE error: {e}")
                await asyncio.sleep(5)
        return StreamingResponse(gen(), media_type="text/event-stream",
            headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    return app
