# Telegram Signal Capture + Paper Trading System — Build Plan

> Purpose: single source of truth for Cursor / Claude Code. Build exactly what is written here. Where something is marked **OPEN**, use the stated default and keep it configurable. Do not invent features outside this document.

## 1. Goal

1. Read messages from ONE Telegram channel (trading signals) in real time.
2. Store every message raw, exactly once.
3. Use an LLM (OpenAI) to classify each message and extract signal fields.
4. Assemble partial messages (security/target in one, SL in another, entry in a third) into one **trade** record.
5. Paper-trade each captured trade against Upstox market data (no real orders), with a full audit log.
6. Broker layer must be swappable (Upstox now, Zerodha later).

**Phase 1:** capture + assemble + store trades (no trading).
**Phase 2:** paper-trade simulation + transaction log.
**Out of scope:** real order placement, UI, multi-channel support.

## 2. Tech Stack

- Python 3.11+
- Telethon (Telegram user-account client, uses api_id / api_hash)
- OpenAI API (structured JSON output, model configurable via env)
- PostgreSQL (SQLite allowed for dev) via SQLAlchemy 2.x + Alembic
- Upstox API v2 (historical + live market data) behind a broker interface
- asyncio throughout; Pydantic for all schemas; structlog for logging
- Config via `.env` + `pydantic-settings`

## 3. Architecture (pipeline)

```
Telegram channel
   │  (Telethon event handler: NewMessage + MessageEdited)
   ▼
[1] Ingestor ──► raw_messages (idempotent)
   ▼
[2] Classifier (LLM) ──► message_classifications
   ▼
[3] Trade Assembler ──► trades + trade_messages (links)
   ▼
[4] Trade Executor (paper) ──► paper_trades + trade_events
   ▲
[5] Price Feed (Upstox live/historical, behind BrokerClient interface)
```

Each stage reads from DB and writes to DB. Stages are decoupled so any one can be re-run on stored data (replay).

## 4. Components

### 4.1 Ingestor
- Telethon client, session file persisted. Subscribes to the configured channel only.
- Handle `NewMessage` and `MessageEdited`.
- On startup: **backfill** — fetch messages since last stored `tg_message_id` to cover downtime.
- Store per message: `channel_id`, `tg_message_id`, `reply_to_msg_id`, `text`, `sent_at` (UTC), `edited_at`, `raw_json`.
- Idempotency: UNIQUE(`channel_id`, `tg_message_id`). Insert-or-ignore. On edit: update text, set `edited_at`, mark classification stale for re-run.
- Capture reply/quote reference (`reply_to_msg_id`) — critical for linking premium updates and follow-up messages to the original trade.
- Reconnect on disconnect with backoff. Never crash the process on a single bad message.

### 4.2 Classifier (LLM)
Input: the message text + the replied-to message text (if any) + last N (default 10) messages of context.
Output (strict JSON, validated by Pydantic):

```json
{
  "message_type": "NEW_SIGNAL | SIGNAL_UPDATE | ENTRY_TRIGGER | PREMIUM_UPDATE | TARGET_HIT | SL_HIT | EXIT | NOISE",
  "confidence": 0.0,
  "fields": {
    "underlying": "string|null",
    "expiry": "string|null",
    "strike": "number|null",
    "option_type": "CE|PE|null",
    "entry_low": "number|null",
    "entry_high": "number|null",
    "stop_loss": "number|null",
    "targets": ["number"],
    "current_premium": "number|null"
  },
  "references_msg_id": "int|null"
}
```

Rules:
- `targets` is an ordered list of levels (T1, T2, T3...), never a single value.
- `entry_low`/`entry_high` form a range (e.g. 140–145). Single entry → both equal.
- Absent fields = `null`. The LLM must never guess or fabricate.
- Store prompt version, model name, raw response, tokens, latency per call.
- Classify messages in arrival order; one retry on invalid JSON; then mark `FAILED` and continue.
- **Phase 1 priority:** only `NEW_SIGNAL`, `SIGNAL_UPDATE`, `ENTRY_TRIGGER` must be accurate. `PREMIUM_UPDATE` etc. are stored but may be ignored downstream.

### 4.3 Trade Assembler
Problem: one trade can arrive across several messages.

Algorithm:
1. `NEW_SIGNAL` → create a trade in state `PARTIAL` (or `COMPLETE` if all required fields present).
2. `SIGNAL_UPDATE` / `ENTRY_TRIGGER` → attach to a trade by, in priority order:
   a. `reply_to_msg_id` pointing to a message already linked to a trade;
   b. LLM-provided `references_msg_id`;
   c. otherwise the most recent `PARTIAL` trade within the time window (default 30 min, configurable).
3. Merge fields: only fill nulls, or overwrite on explicit update; record every change in `trade_field_history`.
4. A trade is `COMPLETE` when it has: underlying, strike, option_type, entry range, stop_loss, ≥1 target.
5. A `PARTIAL` trade older than the window with missing fields → `EXPIRED_INCOMPLETE` (kept for review, never traded).
6. Every message→trade link is stored in `trade_messages`.
7. Duplicate protection: same underlying+strike+type+expiry+entry within the window → same trade, not a new one.

Trade states: `PARTIAL → COMPLETE → ACTIVE → CLOSED` or `EXPIRED_INCOMPLETE` / `CANCELLED`.

### 4.4 Broker Abstraction
```python
class BrokerClient(Protocol):
    async def resolve_instrument(underlying, expiry, strike, option_type) -> Instrument
    async def get_ltp(instrument_key) -> float
    async def get_historical_candles(instrument_key, interval, start, end) -> list[Candle]
    async def stream_ltp(instrument_keys, callback) -> None
```
- Implement `UpstoxBroker` now. Zerodha later = new class, no changes elsewhere.
- Instrument master (Upstox instruments file) downloaded daily and cached; used to map (underlying, expiry, strike, CE/PE) → instrument key.
- Handle Upstox token expiry (daily token): refresh flow isolated in the broker class. **OPEN:** how token is obtained daily — default: read from env/file, fail loudly if expired.

### 4.5 Paper Trade Executor
Runs for every trade reaching `COMPLETE`.

**Entry**
- Subscribe to live premium of the option.
- Enter (simulated) when premium is within `[entry_low, entry_high]`. Fill price = LTP at trigger time.
- If SL is hit before entry, or entry not triggered within `ENTRY_TIMEOUT` (default 60 min) or by market close → `CANCELLED`.
- Quantity = `lot_size × LOTS` (both from config; lot size per underlying in config).

**Exit (checked on every tick)** — first condition wins:
1. `LTP <= stop_loss` → exit at SL, reason `SL_HIT`.
2. Target by R-multiple: risk = `entry_price − stop_loss`; `rr_target = entry_price + 2 × risk` (RR = 2:1, configurable `RR_REWARD=2`, `RR_RISK=1`) → exit, reason `RR_TARGET`.
3. Market close (configurable, default 15:15 IST) → exit at LTP, reason `EOD`.
- Also record which of the channel's target levels (T1, T2, T3) were touched, with timestamps, as informational events (`TARGET_LEVEL_TOUCHED`), independent of exit logic.
- **OPEN:** exit policy. Default = 2R fixed exit as above. Alternative modes to keep as config options: `SIGNAL_TARGETS` (exit at T1/T2/T3 per signal), `HYBRID`. Implement only `RR` first; leave a strategy interface for the others.

**Fees/slippage:** configurable flat slippage (default 0) and brokerage per order (default 0).

**P&L:** `(exit − entry) × qty − fees`. Also store R-multiple achieved.

**Replay mode:** same executor can run over historical candles (Upstox historical API) for a stored trade to verify logic without waiting for live market. Candle resolution: 1-minute; within a candle, assume SL is checked before target when both are touched (conservative). Document this assumption in code.

### 4.6 Audit Trail (mandatory)
Every state change is an immutable row in `trade_events`:
`trade_id`, `event_type` (CAPTURED, FIELD_UPDATED, COMPLETE, ENTRY_TRIGGERED, TARGET_LEVEL_TOUCHED, SL_HIT, RR_TARGET_HIT, EOD_EXIT, CANCELLED, EXPIRED), `price`, `event_time`, `source_message_id`, `payload_json`.

Timestamps required: message sent time, captured time, classified time, trade-complete time, entry time, exit time. All UTC in DB, IST for display.

## 5. Database Schema

- `raw_messages`(id, channel_id, tg_message_id, reply_to_msg_id, text, sent_at, edited_at, raw_json, created_at) — UNIQUE(channel_id, tg_message_id)
- `message_classifications`(id, message_id FK, message_type, confidence, fields_json, references_msg_id, model, prompt_version, raw_response, tokens, latency_ms, status, created_at)
- `trades`(id, underlying, expiry, strike, option_type, instrument_key, entry_low, entry_high, stop_loss, targets_json, state, first_message_id, created_at, completed_at, updated_at)
- `trade_messages`(trade_id, message_id, role) — role: ORIGIN / UPDATE / TRIGGER / PREMIUM
- `trade_field_history`(id, trade_id, field, old_value, new_value, message_id, changed_at)
- `paper_trades`(id, trade_id FK, entry_price, entry_time, qty, exit_price, exit_time, exit_reason, pnl, r_multiple, fees, status)
- `trade_events`(see 4.6)
- `config_snapshots`(id, trade_id, json) — config used per paper trade, for reproducibility

## 6. Configuration (`.env`)

```
TG_API_ID, TG_API_HASH, TG_SESSION_NAME, TG_CHANNEL
OPENAI_API_KEY, OPENAI_MODEL
UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_ACCESS_TOKEN
DATABASE_URL
BROKER=upstox
LOTS=1
LOT_SIZE_MAP={"NIFTY":75,"BANKNIFTY":30}   # verify current exchange lot sizes before use
RR_REWARD=2
RR_RISK=1
EXIT_MODE=RR
ASSEMBLY_WINDOW_MIN=30
ENTRY_TIMEOUT_MIN=60
MARKET_CLOSE_EXIT=15:15
SLIPPAGE=0
BROKERAGE_PER_ORDER=0
```

## 7. Project Structure

```
app/
  config.py
  db/            models.py, session.py, migrations/
  telegram/      ingestor.py, backfill.py
  llm/           classifier.py, prompts.py, schemas.py
  assembler/     assembler.py, matching.py
  broker/        base.py, upstox.py, instruments.py   (zerodha.py later)
  execution/     executor.py, exit_strategies.py, replay.py
  audit/         events.py
  main.py        (wires ingestor + classifier + assembler + executor as async tasks)
tests/
  fixtures/      sample_messages.json   (real channel samples, anonymized)
  test_classifier.py, test_assembler.py, test_executor.py
```

## 8. Build Order (milestones)

1. **M1 — Ingest:** Telethon listener + backfill + `raw_messages`. Verify no duplicates after restart.
2. **M2 — Classify:** LLM classifier + fixtures from real messages. Measure accuracy on a hand-labelled set (≥50 messages). Do not proceed until `NEW_SIGNAL` precision is acceptable.
3. **M3 — Assemble:** multi-message trade assembly using reply links + window. Tests for the split-message case (target+name → SL → entry).
4. **M4 — Broker:** Upstox instrument resolution + LTP + historical candles behind `BrokerClient`.
5. **M5 — Replay executor:** run stored trades over historical candles; produce paper_trades + events.
6. **M6 — Live executor:** stream LTP, paper-trade in real time.
7. **M7 — Report:** simple CLI/SQL report: trades captured, win rate, total P&L, avg R, incomplete-trade count.

## 9. Testing Requirements

- Unit tests for assembler using real split-message sequences.
- Classifier tests with a labelled fixture set; LLM calls mocked in CI, real calls in a manual eval script.
- Executor tests: SL-first, target-first, both in same candle, EOD, entry never triggered.
- Idempotency test: replaying the same messages creates no duplicates.

## 10. Non-Functional Rules

- No real orders, ever, in this codebase. No order-placement method on `BrokerClient` in this phase.
- Secrets only from env. Never log tokens or full API keys.
- Any LLM output failing validation is stored and skipped, never trusted.
- Every stage idempotent and re-runnable from DB.
- Single channel; use a dedicated Telegram account/session for the listener (userbot use carries account-restriction risk; keep read-only, no sending).

## 11. OPEN Items (defaults applied if unanswered)

| # | Question | Default |
|---|----------|---------|
| 1 | Exit rule: pure 2R, or channel targets T1/T2/T3? | Pure 2R |
| 2 | Instruments: Nifty/BankNifty index options only, or stock options too? | Index options |
| 3 | Strike: always stated in signal, or inferred (ATM/OTM)? | Stated in signal |
| 4 | Is SL given as premium price or underlying index level? | Premium price |
| 5 | Upstox daily token acquisition (manual vs automated)? | Manual via env |
| 6 | Max concurrent open paper trades? | Unlimited |
