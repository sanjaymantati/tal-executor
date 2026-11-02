from typing import Protocol

from app.config import ExecutionConfig
from app.enums import ExitMode, ExitReason
from app.execution.types import ExitDecision, OpenPosition, PriceBar


class ExitStrategy(Protocol):
    def decide_exit(
        self, position: OpenPosition, bar: PriceBar, entered_on_this_bar: bool
    ) -> ExitDecision | None: ...


class RiskRewardExit:
    """Exit at the stop loss, or at entry + (reward / risk) x (entry - stop loss).

    Conservative same-bar assumption: when one bar touches both the stop loss and the target,
    the stop loss wins. On the bar that triggered the entry the order of events inside the bar
    is unknown, so only the stop loss is evaluated there; the target is judged from the next bar.
    """

    def __init__(self, rr_reward: float, rr_risk: float) -> None:
        self._reward_to_risk = rr_reward / rr_risk

    def decide_exit(
        self, position: OpenPosition, bar: PriceBar, entered_on_this_bar: bool
    ) -> ExitDecision | None:
        if bar.low <= position.stop_loss:
            return ExitDecision(ExitReason.SL_HIT, position.stop_loss)
        target_price = self.target_price(position)
        if not entered_on_this_bar and bar.high >= target_price:
            return ExitDecision(ExitReason.RR_TARGET, target_price)
        return None

    def target_price(self, position: OpenPosition) -> float:
        return position.entry_price + self._reward_to_risk * position.risk


def build_exit_strategy(config: ExecutionConfig) -> ExitStrategy:
    if config.exit_mode is ExitMode.RR:
        return RiskRewardExit(config.rr_reward, config.rr_risk)
    raise NotImplementedError(f"exit mode {config.exit_mode} is not implemented yet")
