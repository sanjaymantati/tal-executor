from pathlib import Path

import httpx

from app.broker.base import BrokerClient
from app.broker.instruments import InstrumentMaster
from app.broker.upstox import UpstoxBroker
from app.config import Settings
from app.timeutil import utc_now


def build_broker(settings: Settings, http_client: httpx.AsyncClient) -> BrokerClient:
    if settings.broker == "upstox":
        master = InstrumentMaster(http_client, Path(settings.instrument_cache_dir), utc_now)
        return UpstoxBroker(
            http_client,
            settings.upstox_access_token,
            master,
            settings.upstox_poll_interval_sec,
            utc_now,
        )
    raise ValueError(f"unsupported BROKER: {settings.broker}")
