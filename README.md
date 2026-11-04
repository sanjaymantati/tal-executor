# tal-executor

Captures trading signals from one Telegram channel, classifies them with an LLM, assembles split
messages into trades, and paper-trades them against Upstox market data. **No real orders are ever
placed**: `BrokerClient` has no order methods. See [plan.md](plan.md) for the full specification.

```
Telegram -> Ingestor -> raw_messages -> Classifier -> message_classifications
         -> Assembler -> trades -> Paper executor -> paper_trades + trade_events
                                        ^ BrokerClient (Upstox)
```

## Setup (Python 3.11+)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env .env            # fill in the values
alembic upgrade head            # create the schema
python get_chat_id.py           # find your channel's chat id for TG_CHANNEL
```

Use a dedicated Telegram account for the listener. The first run asks for the login code.
`LOT_SIZE_MAP` in `.env` must be checked against current exchange lot sizes.
`UPSTOX_ACCESS_TOKEN` must be a token for the **live** API (api.upstox.com); sandbox tokens
(30 days, issued for the sandbox) are rejected. The app only reads market data (instrument file,
LTP, candles) and never places orders, so a read-only token that allows market data is enough.
A normal OAuth access token expires around 3:30 AM the next day, so paste a fresh one each trading
day. A bad or expired token fails loudly (`BrokerAuthError`, the startup log line
`upstox_token_invalid`, and the red Upstox card on the dashboard).

## Run

```bash
python -m app.main run                    # everything: pipeline + dashboard at http://127.0.0.1:8000
python -m app.main run --no-dashboard     # pipeline only
python -m app.main replay                 # simulate every COMPLETE trade over 1-minute candles
python -m app.main replay --trade-id 12   # one trade
python -m app.ui                          # dashboard only, to browse stored data (no listener)
python -m app.report                      # captured trades, win rate, P&L, avg R, incomplete count
python -m pytest                          # tests (LLM and broker are mocked)
python -m scripts.eval_classifier PATH    # manual: real LLM calls vs a hand-labelled file
```

**Local Ollama (OpenAI-compatible API):** set `LLM_PROVIDER=ollama`, `OPENAI_MODEL` to your pulled
model (e.g. `llama3.2`), and optionally `OPENAI_BASE_URL` (defaults to `http://localhost:11434/v1`).
`OPENAI_API_KEY` can be omitted for Ollama (defaults to `ollama`).

Each stage reads from and writes to the database and is idempotent, so any stage can be re-run.

## Docker

```bash
docker compose build
# Telegram login (first time only), or copy an existing session: cp telegram_session.session data/
docker compose run --rm -it app
docker compose up -d                    # pipeline + dashboard at http://127.0.0.1:8000
docker compose logs -f app
```

`data/` is mounted as a volume and holds the database, the Telegram session
(`TG_SESSION_NAME=data/telegram_session`) and the status file. Credentials come from `.env` (never baked into
the image). Migrations run on every start. Run a single container: two processes must not share the session.

## Dashboard

`python -m app.main run` serves a read-mostly web page on localhost (`UI_HOST`/`UI_PORT`; no login, so keep it local)
in the same process as the pipeline. If the dashboard cannot bind its address it logs
`dashboard_failed_to_start` and the pipeline keeps running. `python -m app.ui` starts the dashboard alone;
do not run it next to `run` on the same port.

- **Health cards:** Upstox (reads the NIFTY 50 LTP with your token and shows the token's expiry) and
  Telegram (while the pipeline runs it shows the listener's own state; it only probes a temporary copy of
  the session file when no pipeline is running, because a second connection steals live messages from the
  listener). Upstox is checked when the page loads and when you click "Check now", not on a timer, so an idle
  dashboard makes no Upstox calls.
- **Header:** channel name, and whether the pipeline is running (a heartbeat file, `STATUS_FILE`).
- **Live messages:** every captured message with the LLM's type, confidence and extracted fields,
  and the trade it was linked to. Refreshes every 2 seconds.
- **Captured trades:** assembled trades; click a row for source messages, field changes and events.
  Active paper trades (waiting to enter or open) show the live option premium from Upstox. When it cannot be
  fetched the cell shows an `HTTP 404` chip (no price returned, or any other failure) or `HTTP 429` (Upstox
  said too many requests). The P&L column is unrealised while a trade is open (what closing now would book,
  after slippage and fees) and realised once it is closed; it is `-` for trades that have not entered.
- **Mock trading and logs:** paper trades with P&L, the audit-trail log, and Replay buttons that
  simulate COMPLETE trades over today's 1-minute candles (useful when the pipeline is not running).
  Live paper trades are made automatically by the running pipeline.
- **Manual stop:** a running paper trade has a **Stop & exit** button (open position: closed at the latest
  price, exit reason `MANUAL`) or **Cancel entry** (not entered yet). The click is saved as a
  `MANUAL_STOP_REQUESTED` event and the pipeline applies it within about 2 seconds, so the pipeline must
  be running; a request made while it is down is applied when it restarts.
- **P&L:** net/gross P&L, fees, win rate, average win/loss, profit factor, a cumulative P&L chart,
  daily P&L (IST) and the list of closed trades. Only closed paper trades count; open positions are
  shown as a count (no unrealised P&L).

## Milestone checklist

1. Ingest: `app/telegram/` (Telethon, backfill after downtime, edits, idempotent inserts).
2. Classify: `app/llm/`. Build a hand-labelled set of 50+ real messages in the format of
   `tests/fixtures/sample_messages.json` and run `scripts.eval_classifier`. Do not trust
   downstream stages until `NEW_SIGNAL` precision is acceptable.
3. Assemble: `app/assembler/` (reply link, then LLM reference, then recent PARTIAL in the window).
4. Broker: `app/broker/` (`UpstoxBroker`; `get_ltp` and `stream_ltp` poll the REST LTP endpoint).
5. Replay: `app/execution/replay.py`.
6. Live: `app/execution/live.py`.
7. Report: `app/report.py`.

## Assumptions worth knowing

- OPEN items use the defaults of plan.md section 11: pure 2R exit (`EXIT_MODE=RR`; the other modes
  raise `NotImplementedError`), index options, strike stated in the signal, SL as an option premium,
  manual daily Upstox token, no limit on concurrent paper trades.
- Same-candle rule (replay and live): if one bar touches both the stop loss and the target, the stop
  loss wins. On the candle that triggers entry only the stop loss is evaluated.
- Entry fill: the bar open clamped into the entry range (a live tick fills at its own price).
- A trade is only merged into when it is `PARTIAL` or `COMPLETE`. A message without a reply or
  reference attaches only to a `PARTIAL` trade within `ASSEMBLY_WINDOW_MIN`.
- The entry timeout is measured from the sent time of the trade's latest non-premium message.
- Replay needs the option contract to still be in today's Upstox instrument file, so replay a
  stored trade before its contract expires.
- `message_classifications.assembled_at` is an extra column (not in plan.md) used so the assembler
  consumes each classification exactly once.
- Without a first-run history, nothing is back-filled; set `BACKFILL_INITIAL_LIMIT` to pull recent
  messages (useful for building the labelled set).
