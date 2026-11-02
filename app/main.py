"""Entry point.

    python -m app.main run                  # pipeline (ingest, classify, assemble, paper-trade) + dashboard
    python -m app.main run --no-dashboard   # pipeline only
    python -m app.main replay [--trade-id]  # paper-trade stored COMPLETE trades over historical candles
    python -m app.ui                        # dashboard only (no listener), e.g. to browse stored data
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine
from telethon import TelegramClient

from app.broker.factory import build_broker
from app.config import Settings
from app.db.session import SessionFactory, create_engine, create_session_factory
from app.execution.live import LiveTradeWatcher
from app.execution.replay import TradeReplayer
from app.logging_setup import configure_logging
from app.timeutil import utc_now
from app.ui.health import UpstoxHealthCheck
from app.ui.server import create_app, serve_dashboard
from app.ui.services import build_ui_services
from app.wiring import (
    build_assembler,
    build_classifier,
    build_executor,
    build_ingestor,
    build_status_store,
)
from app.workers import run_periodically

log = structlog.get_logger(__name__)

WORKER_INTERVAL_SEC = 2.0
HEARTBEAT_INTERVAL_SEC = 5.0
HTTP_TIMEOUT_SEC = 30.0

DashboardTask = Callable[[], Awaitable[None]]


def main() -> None:
    args = parse_arguments()
    settings = Settings()
    configure_logging(settings.log_level)
    if args.command == "run":
        asyncio.run(run_pipeline(settings, with_dashboard=not args.no_dashboard))
    else:
        asyncio.run(replay_trades(settings, args.trade_id))


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app.main")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--no-dashboard", action="store_true")
    replay = commands.add_parser("replay")
    replay.add_argument("--trade-id", type=int, default=None)
    return parser.parse_args()


async def run_pipeline(settings: Settings, with_dashboard: bool) -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    client = TelegramClient(settings.tg_session_name, settings.tg_api_id, settings.tg_api_hash)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as http_client:
        broker = build_broker(settings, http_client)
        executor = build_executor(settings, session_factory, broker)
        watcher = LiveTradeWatcher(executor, broker, utc_now)
        dashboard = None
        if with_dashboard:
            dashboard = build_dashboard_task(
                settings, session_factory, http_client, broker, executor
            )
        await log_upstox_token_status(settings, http_client)
        try:
            await run_workers(settings, session_factory, client, watcher, dashboard)
        finally:
            await shutdown(engine, client, watcher)


def build_dashboard_task(
    settings: Settings, session_factory, http_client, broker, executor
) -> DashboardTask:
    async def keep_resources_open() -> None:
        """The pipeline owns the database engine and HTTP client, so the dashboard must not close them."""

    services = build_ui_services(
        settings, session_factory, http_client, broker, executor, keep_resources_open
    )
    app = create_app(services)
    return lambda: serve_dashboard(app, settings.ui_host, settings.ui_port)


async def log_upstox_token_status(settings: Settings, http_client: httpx.AsyncClient) -> None:
    """Surfaces a bad token at startup; the pipeline otherwise only calls Upstox once a trade is COMPLETE."""
    check = UpstoxHealthCheck(http_client, settings.upstox_access_token, utc_now)
    result = await check.check()
    if result["ok"]:
        log.info(
            "upstox_token_ok", nifty_ltp=result["nifty_ltp"], minutes_left=result["token_minutes_left"]
        )
    else:
        log.warning(
            "upstox_token_invalid", reason=result["message"], paper_trading="will not work until fixed"
        )


async def run_workers(
    settings: Settings,
    session_factory: SessionFactory,
    client: TelegramClient,
    watcher: LiveTradeWatcher,
    dashboard: DashboardTask | None,
) -> None:
    status_store = build_status_store(settings)
    ingestor = build_ingestor(settings, session_factory, client, status_store)
    classifier = build_classifier(settings, session_factory)
    assembler = build_assembler(settings, session_factory)

    async def assemble_and_expire() -> None:
        await assembler.assemble_pending()
        await assembler.expire_incomplete_trades()

    async def beat() -> None:
        status_store.touch_heartbeat()

    async with asyncio.TaskGroup() as group:
        group.create_task(ingestor.run_forever())
        group.create_task(run_periodically("heartbeat", HEARTBEAT_INTERVAL_SEC, beat))
        group.create_task(
            run_periodically("classifier", WORKER_INTERVAL_SEC, classifier.classify_pending)
        )
        group.create_task(run_periodically("assembler", WORKER_INTERVAL_SEC, assemble_and_expire))
        group.create_task(
            run_periodically("live_executor", WORKER_INTERVAL_SEC, watcher.sync_watches)
        )
        if dashboard is not None:
            group.create_task(dashboard())


async def replay_trades(settings: Settings, trade_id: int | None) -> None:
    engine = create_engine(settings.database_url)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as http_client:
        broker = build_broker(settings, http_client)
        executor = build_executor(settings, create_session_factory(engine), broker)
        replayer = TradeReplayer(executor, broker)
        try:
            if trade_id is None:
                log.info("replay_finished", trades=await replayer.replay_all_new_trades())
            else:
                log.info("replay_finished", replayed=await replayer.replay_trade(trade_id))
        finally:
            await engine.dispose()


async def shutdown(engine: AsyncEngine, client: TelegramClient, watcher: LiveTradeWatcher) -> None:
    await watcher.stop_all()
    if client.is_connected():
        await client.disconnect()
    await engine.dispose()


if __name__ == "__main__":
    main()
