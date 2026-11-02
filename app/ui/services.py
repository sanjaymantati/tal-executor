from collections.abc import Awaitable, Callable

import httpx

from app.broker.base import BrokerClient
from app.config import Settings
from app.db.session import SessionFactory
from app.execution.executor import PaperTradeExecutor
from app.execution.ledger import PaperTradeLedger
from app.execution.replay import TradeReplayer
from app.timeutil import utc_now
from app.ui.health import TelegramHealthCheck, UpstoxHealthCheck
from app.ui.pnl import PnlQueries
from app.ui.queries import DashboardQueries
from app.ui.server import UiServices
from app.wiring import build_status_store


def build_ui_services(
    settings: Settings,
    session_factory: SessionFactory,
    http_client: httpx.AsyncClient,
    broker: BrokerClient,
    executor: PaperTradeExecutor,
    close: Callable[[], Awaitable[None]],
) -> UiServices:
    return UiServices(
        queries=DashboardQueries(session_factory),
        pnl=PnlQueries(session_factory),
        ledger=PaperTradeLedger(session_factory),
        upstox_health=UpstoxHealthCheck(http_client, settings.upstox_access_token, utc_now),
        telegram_health=TelegramHealthCheck(
            settings.tg_session_name,
            settings.tg_api_id,
            settings.tg_api_hash,
            settings.tg_channel,
            utc_now,
        ),
        replayer=TradeReplayer(executor, broker),
        status_store=build_status_store(settings),
        channel_reference=settings.tg_channel,
        clock=utc_now,
        close=close,
    )
