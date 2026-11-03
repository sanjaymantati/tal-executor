import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

HEARTBEAT_MAX_AGE_SEC = 20


class PipelineStatusStore:
    """Tiny JSON file the live pipeline writes and the UI reads (heartbeat + Telegram state)."""

    def __init__(self, path: Path, clock: Callable[[], datetime]) -> None:
        self._path = path
        self._clock = clock

    def is_pipeline_running(self) -> bool:
        heartbeat = self.read().get("heartbeat_at")
        if heartbeat is None:
            return False
        age_sec = (self._clock() - datetime.fromisoformat(heartbeat)).total_seconds()
        return age_sec <= HEARTBEAT_MAX_AGE_SEC

    def update_telegram(
        self, state: str, channel_id: int | None, channel_title: str | None
    ) -> None:
        data = self.read()
        telegram = data.get("telegram", {})
        telegram["state"] = state
        telegram["updated_at"] = self._clock().isoformat()
        if channel_id is not None:
            telegram["channel_id"] = channel_id
        if channel_title is not None:
            telegram["channel_title"] = channel_title
        data["telegram"] = telegram
        self._write(data)

    def touch_heartbeat(self) -> None:
        data = self.read()
        data["heartbeat_at"] = self._clock().isoformat()
        self._write(data)

    def read(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        os.replace(temporary, self._path)
