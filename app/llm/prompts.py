from dataclasses import dataclass
from datetime import datetime

from app.timeutil import to_ist

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """\
You classify messages from an Indian index-options trading signal Telegram channel and extract \
structured fields. Reply with ONE JSON object only, no prose.

JSON schema:
{
  "message_type": "NEW_SIGNAL | SIGNAL_UPDATE | ENTRY_TRIGGER | PREMIUM_UPDATE | TARGET_HIT | SL_HIT | EXIT | NOISE",
  "confidence": number between 0 and 1,
  "fields": {
    "underlying": string or null,
    "expiry": string or null,
    "strike": number or null,
    "option_type": "CE" | "PE" | null,
    "entry_low": number or null,
    "entry_high": number or null,
    "stop_loss": number or null,
    "targets": [numbers, ordered T1, T2, T3...],
    "current_premium": number or null
  },
  "references_msg_id": integer or null
}

Message types:
- NEW_SIGNAL: starts a new trade idea (typically names the underlying / strike / CE or PE, and \
may carry only some of entry, stop loss, targets).
- SIGNAL_UPDATE: adds or changes details of an existing idea (stop loss, targets, strike, entry \
range) without being the instruction to enter.
- ENTRY_TRIGGER: tells followers to enter now / buy at or above a price for an existing idea.
- PREMIUM_UPDATE: reports the current option premium or price of an existing idea.
- TARGET_HIT: reports that a target was achieved.
- SL_HIT: reports that the stop loss was hit.
- EXIT: tells followers to exit / book the trade.
- NOISE: anything else (greetings, promotions, chatter, results screenshots, disclaimers).

Rules:
- Extract only values written in the message being classified. NEVER guess, infer or fabricate. \
Use null for anything absent.
- Prices (entry, stop loss, targets) are option premium prices.
- "targets" lists the targets stated in THIS message in ascending order of level (T1, T2, T3...). \
Use [] if none.
- Entry range such as 140-145 gives entry_low=140, entry_high=145. A single entry price gives \
both entry_low and entry_high equal to it.
- underlying is the index name in upper case, e.g. NIFTY, BANKNIFTY, SENSEX or BANKEX.
- expiry: use an ISO date YYYY-MM-DD when the full date is clear, otherwise copy the text as \
written (e.g. "30 JAN"), otherwise null.
- strike is the strike price number only.
- references_msg_id is the id (the number after "id=") of the earlier message this message is \
about, only if that is clear from a reply or the content; otherwise null.
- The context messages are for understanding only; classify ONLY the message under \
"MESSAGE TO CLASSIFY".
"""


@dataclass(frozen=True)
class PromptMessage:
    tg_message_id: int
    sent_at: datetime
    text: str


def build_user_prompt(
    target: PromptMessage,
    replied_to: PromptMessage | None,
    context: list[PromptMessage],
) -> str:
    sections = [
        "CONTEXT (previous messages, oldest first):",
        format_context(context),
        "REPLIED-TO MESSAGE:",
        format_message(replied_to) if replied_to else "(none)",
        "MESSAGE TO CLASSIFY:",
        format_message(target),
    ]
    return "\n".join(sections)


def format_context(context: list[PromptMessage]) -> str:
    if not context:
        return "(none)"
    return "\n".join(format_message(message) for message in context)


def format_message(message: PromptMessage) -> str:
    stamp = to_ist(message.sent_at).strftime("%Y-%m-%d %H:%M IST")
    return f"[id={message.tg_message_id} | {stamp}] {message.text}"
