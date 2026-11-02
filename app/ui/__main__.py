"""Dashboard only (no Telegram listener, classifier or live trading): `python -m app.ui`

Normally you want `python -m app.main run`, which starts the pipeline and the dashboard together.
"""

import httpx
import uvicorn

from app.broker.factory import build_broker
from app.config import Settings
from app.db.session import create_engine, create_session_factory
from app.logging_setup import configure_logging
from app.ui.server import UiServices, create_app
from app.ui.services import build_ui_services
from app.wiring import build_executor

HTTP_TIMEOUT_SEC = 30.0


def build_standalone_services(settings: Settings) -> UiServices:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
    broker = build_broker(settings, http_client)
    executor = build_executor(settings, session_factory, broker)

    async def close() -> None:
        await http_client.aclose()
        await engine.dispose()

    return build_ui_services(settings, session_factory, http_client, broker, executor, close)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    app = create_app(build_standalone_services(settings))
    # Localhost only: the page can trigger mock trades and has no login.
    uvicorn.run(app, host=settings.ui_host, port=settings.ui_port, log_level="warning")


if __name__ == "__main__":
    main()
