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

The service only sends another message when the ranked top-10 snapshot changes, avoiding duplicate Telegram spam every 15 seconds.

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
