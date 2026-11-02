import asyncio
import base64
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest_asyncio
from fastapi import FastAPI

from app.db.models import PaperTrade
from app.enums import PaperTradeStatus
from app.assembler.assembler import TradeAssembler
from app.assembler.matching import TradeMatcher
from app.db.message_store import RawMessageStore
from app.execution.replay import TradeReplayer
from app.llm.classifier import MessageClassifier
from app.llm.prompts import PROMPT_VERSION
from app.status import PipelineStatusStore
from app.ui.health import (
    UpstoxHealthCheck,
    describe_token_expiry,
    interpret_probe_response,
    read_jwt_expiry,
)
from app.execution.ledger import PaperTradeLedger
from app.ui.pnl import PnlQueries
from app.ui.queries import DashboardQueries
from app.ui.server import UiServices, create_app, serve_dashboard
from tests.conftest import BASE_TIME, make_message
from tests.helpers import FixtureLlm, ingest_sample_messages
from tests.test_replay_and_live import FakeBroker, build_executor, candle

NOW = BASE_TIME + timedelta(hours=4)


class StaticHealth:
    def __init__(self, result: dict) -> None:
        self._result = result

    async def check(self) -> dict:
        return self._result


async def noop() -> None:
    return None


async def seed_pipeline_data(session_factory) -> None:
    await ingest_sample_messages(RawMessageStore(session_factory))
    classifier = MessageClassifier(session_factory, FixtureLlm(), "test", PROMPT_VERSION, 10)
    await classifier.classify_pending()
    assembler = TradeAssembler(session_factory, TradeMatcher(timedelta(minutes=30)), lambda: NOW)
    await assembler.assemble_pending()


@pytest_asyncio.fixture
async def ui(session_factory, tmp_path: Path):
    await seed_pipeline_data(session_factory)
    broker = FakeBroker(
        candles=[candle(m, 143, 144, 141, 143) for m in range(1, 400)], ticks=[]
    )
    status_store = PipelineStatusStore(tmp_path / "status.json", lambda: NOW)
    services = UiServices(
        queries=DashboardQueries(session_factory),
        pnl=PnlQueries(session_factory),
        ledger=PaperTradeLedger(session_factory),
        upstox_health=StaticHealth({"ok": True, "message": "Upstox token is valid"}),
        telegram_health=StaticHealth({"ok": False, "message": "no session"}),
        replayer=TradeReplayer(build_executor(session_factory, broker), broker),
        status_store=status_store,
        channel_reference="-1001234567890",
        clock=lambda: NOW,
        close=noop,
    )
    transport = httpx.ASGITransport(app=create_app(services))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.status_store = status_store
        yield client


async def test_index_page_is_served(ui):
    response = await ui.get("/")

    assert response.status_code == 200
    assert "tal-executor" in response.text


async def test_messages_show_parsed_type_fields_and_trade_link(ui):
    messages = (await ui.get("/api/messages?limit=50")).json()

    assert [m["tg_message_id"] for m in messages][:2] == [107, 106]  # newest first
    by_id = {m["tg_message_id"]: m for m in messages}
    signal = by_id[101]
    assert signal["parsed"]["message_type"] == "NEW_SIGNAL"
    assert signal["parsed"]["fields"]["underlying"] == "BANKNIFTY"
    assert signal["trade_id"] is not None and signal["trade_role"] == "ORIGIN"
    assert by_id[100]["parsed"]["message_type"] == "NOISE"
    assert by_id[100]["trade_id"] is None


async def test_unparsed_message_is_reported_as_waiting(ui, session_factory):
    await RawMessageStore(session_factory).save_new_message(make_message(500, "new", 300, None))

    newest = (await ui.get("/api/messages?limit=1")).json()[0]
    summary = (await ui.get("/api/summary")).json()

    assert newest["tg_message_id"] == 500 and newest["parsed"] is None
    assert summary["messages_waiting_for_parsing"] == 1


async def test_trades_list_and_detail(ui):
    trades = (await ui.get("/api/trades")).json()

    assert [t["label"] for t in trades] == ["#2 NIFTY 24500 PE", "#1 BANKNIFTY 52000 CE"]
    assert all(t["state"] == "COMPLETE" for t in trades)
    detail = (await ui.get(f"/api/trades/{trades[1]['id']}")).json()
    assert [m["role"] for m in detail["messages"]] == ["ORIGIN", "UPDATE", "TRIGGER", "PREMIUM"]
    assert {h["field"] for h in detail["field_history"]} >= {"stop_loss", "entry_low"}
    assert (await ui.get("/api/trades/999")).status_code == 404


async def test_replay_endpoint_creates_paper_trades_and_logs(ui):
    assert (await ui.post("/api/trades/1/replay")).json() == {"replayed": True}
    assert (await ui.post("/api/trades/1/replay")).json() == {"replayed": False}

    trades = (await ui.get("/api/trades")).json()
    paper = next(t for t in trades if t["id"] == 1)["paper_trade"]
    assert paper["status"] in ("CLOSED", "CANCELLED")
    events = (await ui.get("/api/events")).json()
    assert events and events[0]["trade"].startswith("#1 ")
    summary = (await ui.get("/api/summary")).json()
    assert summary["paper_trades_closed"] + summary["paper_trades_cancelled"] >= 1


async def test_stop_endpoint_queues_a_request_for_a_running_paper_trade(ui, session_factory):
    assert (await ui.post("/api/trades/1/stop")).status_code == 409  # no paper trade yet
    assert (await ui.post("/api/trades/999/stop")).status_code == 404
    async with session_factory.begin() as session:
        session.add(PaperTrade(trade_id=1, qty=30, status=PaperTradeStatus.OPEN.value))

    first = await ui.post("/api/trades/1/stop")
    second = await ui.post("/api/trades/1/stop")

    assert first.json()["result"] == "REQUESTED"
    assert second.json()["result"] == "ALREADY_REQUESTED"
    paper = next(t for t in (await ui.get("/api/trades")).json() if t["id"] == 1)["paper_trade"]
    assert paper["stoppable"] is True and paper["stop_requested"] is True


async def test_pnl_endpoint_summarises_closed_paper_trades(ui):
    await ui.post("/api/trades/1/replay")
    await ui.post("/api/trades/2/replay")

    pnl = (await ui.get("/api/pnl")).json()

    closed = pnl["summary"]["closed_trades"]
    assert len(pnl["trades"]) == closed == len(pnl["equity_curve"])
    assert pnl["open_positions"] == 0
    assert pnl["summary"]["net_pnl"] == sum(t["pnl"] for t in pnl["trades"])


async def test_replay_all_handles_every_waiting_trade(ui):
    result = (await ui.post("/api/replay")).json()

    assert result == {"replayed": 2}


async def test_health_endpoints_pass_through(ui):
    assert (await ui.get("/api/health/upstox")).json()["ok"] is True
    assert (await ui.get("/api/health/telegram")).json()["message"] == "no session"


async def test_pipeline_status_follows_the_heartbeat(ui):
    assert (await ui.get("/api/pipeline")).json()["running"] is False

    ui.status_store.touch_heartbeat()
    ui.status_store.update_telegram("listening", -100123, "Alpha Calls")
    status = (await ui.get("/api/pipeline")).json()

    assert status["running"] is True
    assert status["telegram_state"] == "listening"
    assert status["channel_title"] == "Alpha Calls"
    assert status["channel_reference"] == "-1001234567890"


def make_jwt(expiry: datetime) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry.timestamp()}).encode()).decode()
    return f"header.{payload.rstrip('=')}.signature"


def test_token_expiry_is_read_from_the_jwt():
    expiry = NOW + timedelta(minutes=90)

    info = describe_token_expiry(make_jwt(expiry), NOW)

    assert info["token_minutes_left"] == 90
    assert read_jwt_expiry("not-a-jwt") is None


def test_probe_responses_are_interpreted():
    quote = {"data": {"NSE_INDEX:Nifty 50": {"last_price": 24500.5, "instrument_token": "x"}}}
    ok = interpret_probe_response(httpx.Response(200, json=quote))
    expired = interpret_probe_response(httpx.Response(401, json={}))
    unexpected = interpret_probe_response(httpx.Response(503))

    assert ok == {"ok": True, "message": "Upstox token works for market data", "nifty_ltp": 24500.5}
    assert expired["ok"] is False and "expired" in expired["message"]
    assert unexpected == {"ok": False, "message": "Upstox returned HTTP 503"}


async def test_upstox_check_never_returns_the_token():
    token = make_jwt(NOW + timedelta(hours=1))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert request.url.params["instrument_key"] == "NSE_INDEX|Nifty 50"
        return httpx.Response(200, json={"data": {"k": {"last_price": 24500.0}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await UpstoxHealthCheck(client, token, lambda: NOW).check()

    assert result["ok"] is True
    assert token not in json.dumps(result, default=str)


async def test_dashboard_can_run_beside_other_tasks_and_stop_on_cancel():
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict:
        return {"pong": True}

    port = find_free_port()
    task = asyncio.create_task(serve_dashboard(app, "127.0.0.1", port))
    try:
        async with httpx.AsyncClient() as client:
            response = await fetch_when_up(client, f"http://127.0.0.1:{port}/ping")
        assert response.json() == {"pong": True}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_dashboard_that_cannot_bind_does_not_crash_the_caller():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]

        await serve_dashboard(FastAPI(), "127.0.0.1", port)  # returns instead of exiting


def find_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def fetch_when_up(client: httpx.AsyncClient, url: str) -> httpx.Response:
    for _ in range(100):
        try:
            return await client.get(url)
        except httpx.ConnectError:
            await asyncio.sleep(0.05)
    raise AssertionError("dashboard did not start")
