import contextlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.broker.base import BrokerAuthError
from app.enums import StopRequestResult
from app.execution.ledger import PaperTradeLedger
from app.execution.replay import TradeReplayer
from app.status import PipelineStatusStore
from app.ui.health import HealthCheck
from app.ui.pnl import PnlQueries
from app.ui.queries import DashboardQueries

log = structlog.get_logger(__name__)

INDEX_FILE = Path(__file__).parent / "static" / "index.html"
HEARTBEAT_MAX_AGE_SEC = 20


@dataclass
class UiServices:
    queries: DashboardQueries
    pnl: PnlQueries
    ledger: PaperTradeLedger
    upstox_health: HealthCheck
    telegram_health: HealthCheck
    replayer: TradeReplayer
    status_store: PipelineStatusStore
    channel_reference: str
    clock: Callable[[], datetime]
    close: Callable[[], Awaitable[None]]


def create_app(services: UiServices) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await services.close()

    app = FastAPI(title="tal-executor dashboard", lifespan=lifespan)
    register_page_routes(app)
    register_read_routes(app, services)
    register_health_routes(app, services)
    register_mock_trading_routes(app, services)
    register_manual_stop_routes(app, services)
    return app


class EmbeddedServer(uvicorn.Server):
    """Runs inside the host application's event loop; the host owns Ctrl+C handling."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def serve_dashboard(app: FastAPI, host: str, port: int) -> None:
    """Serves the dashboard next to other tasks. A dashboard failure never stops the pipeline."""
    server = EmbeddedServer(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    log.info("dashboard_starting", url=f"http://{host}:{port}")
    try:
        await server.serve()
    except SystemExit:  # uvicorn exits the process when it cannot bind the address
        log.error("dashboard_failed_to_start", host=host, port=port, hint="check UI_HOST/UI_PORT")
    except Exception:
        log.exception("dashboard_crashed")


def register_page_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return FileResponse(INDEX_FILE)


def register_read_routes(app: FastAPI, services: UiServices) -> None:
    @app.get("/api/pipeline")
    async def read_pipeline_status() -> dict[str, Any]:
        return describe_pipeline(services)

    @app.get("/api/summary")
    async def read_summary() -> dict[str, Any]:
        return await services.queries.summarise()

    @app.get("/api/messages")
    async def read_messages(limit: int = 100) -> list[dict[str, Any]]:
        return await services.queries.list_recent_messages(limit)

    @app.get("/api/trades")
    async def read_trades(limit: int = 100) -> list[dict[str, Any]]:
        return await services.queries.list_trades(limit)

    @app.get("/api/trades/{trade_id}")
    async def read_trade(trade_id: int) -> dict[str, Any]:
        detail = await services.queries.describe_trade_in_detail(trade_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="trade not found")
        return detail

    @app.get("/api/pnl")
    async def read_pnl() -> dict[str, Any]:
        return await services.pnl.build_pnl_view()

    @app.get("/api/events")
    async def read_events(limit: int = 200) -> list[dict[str, Any]]:
        return await services.queries.list_recent_events(limit)


def register_health_routes(app: FastAPI, services: UiServices) -> None:
    @app.get("/api/health/upstox")
    async def check_upstox() -> dict[str, Any]:
        return await services.upstox_health.check()

    @app.get("/api/health/telegram")
    async def check_telegram() -> dict[str, Any]:
        return await services.telegram_health.check()


def register_mock_trading_routes(app: FastAPI, services: UiServices) -> None:
    @app.post("/api/replay")
    async def replay_all_new_trades() -> dict[str, Any]:
        return {"replayed": await run_replay(lambda: services.replayer.replay_all_new_trades())}

    @app.post("/api/trades/{trade_id}/replay")
    async def replay_trade(trade_id: int) -> dict[str, Any]:
        replayed = await run_replay(lambda: services.replayer.replay_trade(trade_id))
        return {"replayed": replayed}


STOP_RESPONSES = {
    StopRequestResult.REQUESTED: (200, "Stop requested. The running pipeline will close it within a few seconds."),
    StopRequestResult.ALREADY_REQUESTED: (200, "A stop was already requested for this trade."),
    StopRequestResult.NOT_STOPPABLE: (409, "This trade has no running paper trade to stop."),
    StopRequestResult.UNKNOWN_TRADE: (404, "trade not found"),
}


def register_manual_stop_routes(app: FastAPI, services: UiServices) -> None:
    @app.post("/api/trades/{trade_id}/stop")
    async def stop_trade(trade_id: int) -> dict[str, Any]:
        result = await services.ledger.request_manual_stop(trade_id, services.clock())
        status_code, message = STOP_RESPONSES[result]
        if status_code != 200:
            raise HTTPException(status_code=status_code, detail=message)
        return {"result": result.value, "message": message}


async def run_replay(operation: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await operation()
    except BrokerAuthError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except Exception as error:
        log.exception("replay_failed")
        raise HTTPException(status_code=500, detail=f"Replay failed: {type(error).__name__}") from error


def describe_pipeline(services: UiServices) -> dict[str, Any]:
    status = services.status_store.read()
    heartbeat = parse_time(status.get("heartbeat_at"))
    now = services.clock()
    age_sec = (now - heartbeat).total_seconds() if heartbeat else None
    telegram = status.get("telegram", {})
    return {
        "running": age_sec is not None and age_sec <= HEARTBEAT_MAX_AGE_SEC,
        "heartbeat_age_sec": age_sec,
        "telegram_state": telegram.get("state"),
        "telegram_updated_at": telegram.get("updated_at"),
        "channel_title": telegram.get("channel_title"),
        "channel_id": telegram.get("channel_id"),
        "channel_reference": services.channel_reference,
    }


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
