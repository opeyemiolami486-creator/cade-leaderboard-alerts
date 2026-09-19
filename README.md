# Cade Leaderboard Telegram Alerts

A Railway-ready Python service that polls Cade’s public leaderboard every 15 seconds and sends the current top 10 traders ranked by `prediction_count` to Telegram when alerts are enabled.

## What the alert shows

Each alert contains the trader’s rank, total predictions in Cade’s current 24-hour leaderboard, and **trades/hour**. Trades/hour is calculated from the change in `prediction_count` over samples collected during the **previous three hours**:

```text
trades_per_hour = new_prediction_count - old_prediction_count
                   -----------------------------------------
                         elapsed_hours
```

The first sample for a trader shows `0.00/hr` because there is not yet enough history. The tracker is in memory, so its three-hour history starts over after a Railway redeploy. If Cade’s daily counter goes backward at reset, the tracker safely starts a new history window.

## Verified Cade contract

The service uses:

```text
GET https://cade.market/api/leaderboard?period=day
```

Cade currently returns `period: "24h"`, so “daily” follows Cade’s live 24-hour leaderboard/countdown rather than assuming a local calendar day. The service displays the time remaining to the next UTC midnight; change `RESET_HOUR_UTC` if Cade’s reset boundary differs.

## Telegram commands

- `/alerts` enables alerts for the chat and immediately sends a snapshot.
- `/stop` or `/alertsoff` disables alerts for the chat.
- `/status` reports alert state, polling interval, and the three-hour speed window.
- `/copyplan 1000` analyzes the current top-10 traders' settled prediction history from the previous four days and returns the most profitable tracked trader's recent trades plus a manual sizing plan for a 1000-unit available balance.

The service only sends another message when the ranked top-10 snapshot changes, avoiding duplicate Telegram spam every 15 seconds.

## Four-day copy-trade advisory

The `/copyplan <available_balance>` command uses Cade's read-only endpoint:

```text
GET /api/users/{wallet}/prediction-history?limit=100&cursor=...
```

It follows pagination, filters to the previous four days, excludes unresolved trades, and ranks the current top-10 leaderboard traders by **ROI** (`realized profit / settled stake`). By default, a trader must have at least 10 settled trades and at least **1,000% realized ROI** to qualify. If nobody meets both conditions, the bot returns no recommendation rather than falling back to a weaker trader. It then shows the winning trader's recent trades and proposes a conservative default of `1%` of the available balance per copied trade, with a `10%` combined exposure cap.

These are configurable Railway variables:

```text
COPY_TRADE_PCT=1
MAX_TOTAL_COPY_PCT=10
COPY_RANKING=roi
MIN_COPY_SETTLED_TRADES=10
MIN_COPY_ROI_PCT=1000
```

The service does **not** connect to a wallet, request private keys, submit transactions, or execute copy trading. The output is an unsubmitted manual advisory. Enter the balance in the same units you use when deciding your Cade stake; the percentage calculation is `balance × COPY_TRADE_PCT / 100`. Historical profitability is not a guarantee of future results.

## Railway environment variables

Set these in **Railway → your service → Variables**:

| Variable | Required | Value | Purpose |
|---|---:|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Token from `@BotFather` | Lets the service read commands and send messages |
| `POLL_SECONDS` | No | `15` | Poll interval; values below 15 are clamped to 15 |
| `TOP_N` | No | `10` | Number of traders, clamped to 1–10 |
| `CADE_LEADERBOARD_URL` | No | `https://cade.market/api/leaderboard?period=day` | Cade endpoint; leave default unless it changes |
| `RESET_HOUR_UTC` | No | `0` | Countdown reset hour in UTC; `0` means midnight UTC |
| `REQUEST_TIMEOUT_SECONDS` | No | `10` | HTTP timeout for Cade and Telegram requests |
| `COPY_TRADE_PCT` | No | `1` | Suggested percentage of available balance per copied trade; clamped to 0.1–5% |
| `MAX_TOTAL_COPY_PCT` | No | `10` | Maximum combined copy exposure; clamped to 1–25% |
| `COPY_RANKING` | No | `roi` | Use `roi` to prefer low-stake/high-return traders, or `profit` for absolute profit |
| `MIN_COPY_SETTLED_TRADES` | No | `10` | Minimum four-day settled trades required before a trader qualifies |
| `MIN_COPY_ROI_PCT` | No | `1000` | Hard minimum realized ROI percentage; no recommendation is returned below this threshold |
| `ALERT_CHAT_IDS` | No | blank | Optional comma-separated Telegram chat IDs to start enabled after boot |
| `LOG_LEVEL` | No | `INFO` | Logging level |

Do **not** add `PORT`; Railway injects it automatically. Do not commit `TELEGRAM_BOT_TOKEN` to GitHub.

### Finding a Telegram chat ID

After deploying, send any message to your bot, then send `/alerts`. The bot uses the incoming chat ID automatically. `ALERT_CHAT_IDS` is optional and is not needed for normal use.

## Railway deployment from GitHub

1. In Railway, choose **New Project → Deploy from GitHub repo**.
2. Select this repository.
3. Railway detects the included `Dockerfile` and `railway.toml`.
4. Add `TELEGRAM_BOT_TOKEN` in Variables.
5. Deploy and confirm the `/health` endpoint is healthy.
6. Open Telegram and send `/alerts` to the bot.

The service binds to Railway’s injected `$PORT`, exposes `/health`, and restarts on failure.

Telegram uses long polling. The bot now keeps the Telegram HTTP read timeout longer than Telegram's 20-second `getUpdates` wait, so an idle bot does not falsely report a poll failure. If commands still do not arrive after deployment, check that the bot does not have a webhook configured; Telegram does not allow `getUpdates` while a webhook is active. Remove the webhook with `https://api.telegram.org/bot<YOUR_TOKEN>/deleteWebhook` and redeploy.

The repository uses `start.sh` instead of putting `$PORT` directly in the Procfile. This matters on Railway deployments where the Procfile command can pass the literal string `$PORT` to Uvicorn. If logs show `Error: Invalid value for '--port': '$PORT' is not a valid integer`, pull the latest public GitHub commit and trigger a redeploy. After deployment, the logs should show a running Uvicorn server rather than repeated port errors.

## Local test

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn app:app --reload --port 8000
curl http://localhost:8000/health
```

## Project files

- `app.py` — FastAPI health server, Cade polling, rolling speed tracker, Telegram commands.
- `test_app.py` — unit tests for parsing, ranking, countdown, and speed calculation.
- `Dockerfile`, `Procfile`, `railway.toml` — Railway deployment configuration.
