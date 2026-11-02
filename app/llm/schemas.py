from pydantic import BaseModel, Field, field_validator, model_validator

from app.enums import MessageType, OptionType

UNDERLYING_ALIASES = {"NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY"}


class SignalFields(BaseModel):
    underlying: str | None = None
    expiry: str | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    targets: list[float] = Field(default_factory=list)
    current_premium: float | None = None

    @field_validator("underlying", mode="before")
    @classmethod
    def normalise_underlying(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        squeezed = "".join(value.upper().split()).replace("-", "")
        return UNDERLYING_ALIASES.get(squeezed, squeezed) or None

    @field_validator("option_type", mode="before")
    @classmethod
    def normalise_option_type(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("targets", mode="before")
    @classmethod
    def treat_missing_targets_as_empty(cls, value: object) -> object:
        return [] if value is None else value

    @model_validator(mode="after")
    def mirror_single_sided_entry(self) -> "SignalFields":
        """A single entry price is a range whose bounds are equal."""
        if self.entry_low is None and self.entry_high is not None:
            self.entry_low = self.entry_high
        if self.entry_high is None and self.entry_low is not None:
            self.entry_high = self.entry_low
        if self.entry_low is not None and self.entry_high is not None:
            self.entry_low, self.entry_high = sorted((self.entry_low, self.entry_high))
        return self


class ClassificationResult(BaseModel):
    message_type: MessageType
    confidence: float = Field(ge=0.0, le=1.0)
    fields: SignalFields
    references_msg_id: int | None = None
