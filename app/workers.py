import asyncio
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)


async def run_periodically(
    name: str, interval_sec: float, step: Callable[[], Awaitable[object]]
) -> None:
    """Run `step` forever; a failing step is logged and retried on the next tick."""
    while True:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker_step_failed", worker=name)
        await asyncio.sleep(interval_sec)
