from datetime import time

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import CancelReason, ExitMode, LlmProvider

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"


class UnknownLotSizeError(Exception):
    cancel_reason = CancelReason.UNKNOWN_LOT_SIZE


class ExecutionConfig(BaseModel):
    """Everything the paper executor needs; snapshotted per paper trade for reproducibility."""

    model_config = ConfigDict(frozen=True)

    lots: int
    lot_size_map: dict[str, int]
    rr_reward: float
    rr_risk: float
    exit_mode: ExitMode
    entry_timeout_min: int
    market_close_exit: time
    slippage: float
    brokerage_per_order: float

    def quantity_for(self, underlying: str) -> int:
        lot_size = self.lot_size_map.get(underlying)
        if lot_size is None:
            raise UnknownLotSizeError(f"no lot size configured for {underlying}")
        return lot_size * self.lots


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///data/tal.db"


class Settings(DatabaseSettings):
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_session_name: str = "telegram_session"
    tg_channel: str
    backfill_initial_limit: int = 0

    llm_provider: LlmProvider = LlmProvider.OPENAI
    openai_api_key: str = ""
    openai_model: str
    openai_base_url: str = ""
    openai_proxy: str = ""
    classifier_context_size: int = 10

    @model_validator(mode="after")
    def apply_llm_defaults(self) -> Self:
        if self.llm_provider == LlmProvider.OPENAI and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        if self.llm_provider == LlmProvider.OLLAMA:
            if not self.openai_api_key:
                self.openai_api_key = "ollama"
            if not self.openai_base_url.strip():
                self.openai_base_url = DEFAULT_OLLAMA_BASE_URL
        return self

    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_access_token: str
    upstox_poll_interval_sec: float = 1.0
    instrument_cache_dir: str = "data/instruments"

    broker: str = "upstox"
    lots: int = 1
    lot_size_map: dict[str, int] = {"NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "BANKEX": 30}
    rr_reward: float = 2
    rr_risk: float = 1
    exit_mode: ExitMode = ExitMode.RR
    assembly_window_min: int = 30
    entry_timeout_min: int = 60
    market_close_exit: time = time(15, 15)
    slippage: float = 0
    brokerage_per_order: float = 0

    log_level: str = "INFO"
    status_file: str = "data/status.json"
    ui_host: str = "127.0.0.1"
    ui_port: int = 8000

    def to_execution_config(self) -> ExecutionConfig:
        return ExecutionConfig(
            lots=self.lots,
            lot_size_map=self.lot_size_map,
            rr_reward=self.rr_reward,
            rr_risk=self.rr_risk,
            exit_mode=self.exit_mode,
            entry_timeout_min=self.entry_timeout_min,
            market_close_exit=self.market_close_exit,
            slippage=self.slippage,
            brokerage_per_order=self.brokerage_per_order,
        )
