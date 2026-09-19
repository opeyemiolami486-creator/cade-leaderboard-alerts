from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("cade-alerts")

CADE_URL = os.getenv("CADE_LEADERBOARD_URL", "https://cade.market/api/leaderboard?period=24h")
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "15")))
TOP_N = max(1, min(10, int(os.getenv("TOP_N", "10"))))
RESET_HOUR_UTC = int(os.getenv("RESET_HOUR_UTC", "0")) % 24
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
TELEGRAM_POLL_SECONDS = 20
TELEGRAM_READ_TIMEOUT_SECONDS = 35
SPEED_WINDOW_HOURS = 3.0
COPY_LOOKBACK_DAYS = 4
COPY_TRADE_PCT = max(0.1, min(5.0, float(os.getenv("COPY_TRADE_PCT", "1"))))
MAX_TOTAL_COPY_PCT = max(COPY_TRADE_PCT, min(25.0, float(os.getenv("MAX_TOTAL_COPY_PCT", "10"))))
COPY_RANKING = os.getenv("COPY_RANKING", "roi").lower()
MIN_COPY_SETTLED_TRADES = max(1, int(os.getenv("MIN_COPY_SETTLED_TRADES", "10")))
MIN_COPY_ROI_PCT = max(0.0, float(os.getenv("MIN_COPY_ROI_PCT", "1000")))


def countdown(now: datetime | None = None, reset_at: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    boundary = reset_at
    if boundary is None:
        boundary = now.replace(hour=RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
        if boundary <= now:
            boundary += timedelta(days=1)
    elif boundary <= now:
        boundary += timedelta(days=1)
    seconds = int((boundary - now).total_seconds())
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def normalize(payload: dict[str, Any], top_n: int = TOP_N) -> list[dict[str, Any]]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Cade response has no entries array")
    rows = []
    for entry in entries:
        try:
            rows.append({
                "rank": int(entry.get("rank", 0)),
                "username": str(entry.get("username") or "anonymous"),
                "wallet": str(entry.get("wallet_address") or ""),
                "predictions": int(entry.get("prediction_count", 0)),
                "win_rate": int(entry.get("win_rate_bps", 0)) / 100,
            })
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Cade leaderboard entry: {entry!r}") from exc
    rows.sort(key=lambda x: (-x["predictions"], x["username"].lower(), x["wallet"]))
    for index, row in enumerate(rows[:top_n], 1):
        row["rank"] = index
    return rows[:top_n]


class SpeedTracker:
    """Calculate each trader's prediction speed from samples collected in the last 3 hours."""

    def __init__(self, window_hours: float = SPEED_WINDOW_HOURS):
        self.window = timedelta(hours=window_hours)
        self.samples: dict[str, deque[tuple[datetime, int]]] = defaultdict(deque)

    def update(self, rows: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        result = []
        for row in rows:
            wallet = row["wallet"] or row["username"]
            history = self.samples[wallet]
            # Cade's daily counter can reset; discard an invalid backwards sample.
            if history and row["predictions"] < history[-1][1]:
                history.clear()
            if not history or history[-1][1] != row["predictions"]:
                history.append((now, row["predictions"]))
            cutoff = now - self.window
            while len(history) > 1 and history[1][0] < cutoff:
                history.popleft()
            if len(history) >= 2:
                first_time, first_count = history[0]
                elapsed_hours = max((now - first_time).total_seconds() / 3600, 1 / 3600)
                speed = max(0.0, (row["predictions"] - first_count) / elapsed_hours)
            else:
                speed = 0.0
            enriched = dict(row)
            enriched["trades_per_hour"] = speed
            result.append(enriched)
        return result


def snapshot_key(rows: list[dict[str, Any]]) -> str:
    return json.dumps([(r["username"], r["wallet"], r["predictions"]) for r in rows], separators=(",", ":"))


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def reset_from_cursor(cursor: str | None) -> datetime | None:
    """Cade encodes the current cycle start/end timestamps in next_cursor."""
    if not cursor:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        schedule = json.loads(decoded).get("schedule", "").split("|")
        for value in reversed(schedule):
            parsed = parse_timestamp(value)
            if parsed and parsed > datetime.now(timezone.utc):
                return parsed
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        log.warning("could not decode Cade cycle cursor")
    return None


def realized_profit(prediction: dict[str, Any]) -> int:
    """Return realized profit in Cade's smallest credit units; unresolved trades are zero."""
    if prediction.get("lifecycle_state") not in {"settled", "resolved"}:
        return 0
    try:
        return int(prediction.get("credit_payout_raw") or 0) - int(prediction.get("net_stake_raw") or 0)
    except (TypeError, ValueError):
        return 0


def build_copy_plan(rows: list[dict[str, Any]], histories: dict[str, list[dict[str, Any]]], balance: float, now: datetime | None = None, min_settled_trades: int = MIN_COPY_SETTLED_TRADES, min_roi_pct: float = MIN_COPY_ROI_PCT) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=COPY_LOOKBACK_DAYS)
    candidates = []
    for row in rows:
        trades = [t for t in histories.get(row["wallet"], []) if (ts := parse_timestamp(t.get("created_at"))) and ts >= cutoff]
        settled = [t for t in trades if t.get("lifecycle_state") in {"settled", "resolved"}]
        profit = sum(realized_profit(t) for t in settled)
        candidates.append({"row": row, "trades": trades, "settled": settled, "profit_raw": profit})
    for candidate in candidates:
        stake = sum(int(t.get("net_stake_raw") or 0) for t in candidate["settled"])
        candidate["stake_raw"] = stake
        candidate["roi_pct"] = (100 * candidate["profit_raw"] / stake) if stake else None
    eligible = [
        x for x in candidates
        if len(x["settled"]) >= min_settled_trades
        and x["roi_pct"] is not None
        and x["roi_pct"] >= min_roi_pct
    ]
    if COPY_RANKING == "profit":
        eligible.sort(key=lambda x: (-x["profit_raw"], -len(x["settled"]), x["row"]["username"].lower()))
    else:
        eligible.sort(key=lambda x: (-x["roi_pct"], -len(x["settled"]), -x["profit_raw"], x["row"]["username"].lower()))
    candidates = eligible
    winner = candidates[0] if candidates else None
    per_trade = balance * COPY_TRADE_PCT / 100
    max_total = balance * MAX_TOTAL_COPY_PCT / 100
    plan = {
        "balance": balance,
        "copy_trade_pct": COPY_TRADE_PCT,
        "max_total_copy_pct": MAX_TOTAL_COPY_PCT,
        "per_trade_amount": per_trade,
        "max_total_amount": max_total,
        "ranking": COPY_RANKING,
        "minimum_settled_trades": min_settled_trades,
        "minimum_roi_pct": min_roi_pct,
        "winner": winner,
        "as_of": now,
    }
    return plan


def format_message(rows: list[dict[str, Any]], now: datetime | None = None, reset_at: datetime | None = None, period_date: str | None = None) -> str:
    generated = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_predictions = sum(row.get("predictions", 0) for row in rows)
    cycle = period_date or "Cade current 24h cycle"
    lines = ["📊 <b>Cade top traders — predictions</b>", f"Cade cycle {cycle} • checked {generated}", f"Top {len(rows)} combined predictions: <b>{total_predictions:,}</b>", "Trades/hour = speed measured from samples in the previous 3 hours.", f"Cade cycle resets in <b>{countdown(now, reset_at)}</b>", ""]
    for row in rows:
        name = row["username"].replace("<", "&lt;").replace(">", "&gt;")
        speed = row.get("trades_per_hour", 0.0)
        lines.append(f"<b>{row['rank']}.</b> {name} — <b>{row['predictions']}</b> predictions — <b>{speed:.2f}/hr</b>")
    return "\n".join(lines)


def format_copy_plan(plan: dict[str, Any]) -> str:
    winner = plan.get("winner")
    if not winner:
        return (
            f"No trader currently meets the copy filter: at least {MIN_COPY_ROI_PCT:.0f}% realized ROI "
            f"over {COPY_LOOKBACK_DAYS} days and the minimum settled-trade sample.\n"
            "No weaker trader will be recommended automatically."
        )
    row = winner["row"]
    name = row["username"].replace("<", "&lt;").replace(">", "&gt;")
    profit = winner["profit_raw"]
    lines = [
        "<b>Manual copy-trade advisory — Cade</b>",
        f"Most profitable tracked trader: <b>{name}</b>",
        f"Realized profit, last {COPY_LOOKBACK_DAYS} days: <b>{profit:,} Cade raw units</b>",
        f"ROI: <b>{winner.get('roi_pct', 0):.2f}%</b> • required: <b>{plan['minimum_roi_pct']:.0f}%+</b> • minimum sample: {plan['minimum_settled_trades']} settled trades",
        f"Settled trades analyzed: {len(winner['settled'])}",
        "",
        f"Suggested size: <b>{plan['copy_trade_pct']:.2f}%</b> of available balance per copied trade",
        f"For balance {plan['balance']:.2f}: <b>{plan['per_trade_amount']:.2f}</b> credits per trade",
        f"Maximum combined copy exposure: <b>{plan['max_total_copy_pct']:.2f}%</b> = <b>{plan['max_total_amount']:.2f}</b> credits",
        "",
        "This is an unsubmitted manual plan. The bot does not connect to a wallet or place trades.",
        "The trader is selected from the current top-10 leaderboard; this is not a guarantee of future profit.",
        "",
        "<b>Recent trades to review:</b>",
    ]
    for trade in sorted(winner["trades"], key=lambda t: t.get("created_at", ""), reverse=True)[:10]:
        title = str(trade.get("market_title") or trade.get("description") or "Unnamed market").replace("<", "&lt;").replace(">", "&gt;")
        side = str(trade.get("side") or "unknown").upper()
        stake = int(trade.get("net_stake_raw") or 0)
        lines.append(f"• {side} — {stake:,} Cade raw units — {title[:100]}")
    return "\n".join(lines)


@dataclass
class State:
    alerts: dict[int, bool]
    last_key: dict[int, str]
    offset: int = 0


class CadeClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.reset_at: datetime | None = None
        self.period_date: str | None = None

    async def leaderboard(self) -> list[dict[str, Any]]:
        url = CADE_URL.replace("period=day", "period=24h")
        response = await self.client.get(url)
        response.raise_for_status()
        payload = response.json()
        self.period_date = payload.get("date")
        self.reset_at = reset_from_cursor(payload.get("next_cursor"))
        return normalize(payload)

    async def prediction_history(self, wallet: str) -> list[dict[str, Any]]:
        predictions = []
        cursor = None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = await self.client.get(f"https://cade.market/api/users/{wallet}/prediction-history", params=params)
            response.raise_for_status()
            body = response.json()
            batch = body.get("predictions")
            if not isinstance(batch, list):
                raise ValueError(f"Cade history response has no predictions array for {wallet}")
            predictions.extend(batch)
            if not body.get("has_more") or not body.get("next_cursor"):
                break
            cursor = body["next_cursor"]
        return predictions


class TelegramClient:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client, self.base = client, f"https://api.telegram.org/bot{token}"

    async def updates(self, offset: int) -> list[dict[str, Any]]:
        # Telegram holds getUpdates open for TELEGRAM_POLL_SECONDS. The HTTP read
        # timeout must be longer than that server-side wait, or every idle poll
        # becomes a false failure and commands appear unreliable.
        response = await self.client.get(
            f"{self.base}/getUpdates",
            params={"timeout": TELEGRAM_POLL_SECONDS, "offset": offset},
            timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {body}")
        return body.get("result", [])

    async def send(self, chat_id: int, text: str) -> None:
        response = await self.client.post(f"{self.base}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        response.raise_for_status()


app = FastAPI(title="Cade Leaderboard Alerts", version="1.1.0")
state = State(
    alerts={int(x): True for x in os.getenv("ALERT_CHAT_IDS", "").split(",") if x.strip().isdigit()},
    last_key={},
)
speed_tracker = SpeedTracker()


@app.get("/")
async def root() -> dict[str, Any]:
    return {"service": "cade-leaderboard-alerts", "status": "ok", "poll_seconds": POLL_SECONDS, "speed_window_hours": SPEED_WINDOW_HOURS, "cade_url": CADE_URL}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def run_bot() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN is not set; bot loop disabled")
        return
    timeout = httpx.Timeout(connect=REQUEST_TIMEOUT, read=TELEGRAM_READ_TIMEOUT_SECONDS, write=REQUEST_TIMEOUT, pool=REQUEST_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        cade, telegram = CadeClient(client), TelegramClient(client, token)
        while True:
            try:
                for update in await telegram.updates(state.offset):
                    state.offset = max(state.offset, int(update["update_id"]) + 1)
                    message = update.get("message") or {}
                    chat = message.get("chat") or {}
                    chat_id = chat.get("id")
                    command = (message.get("text") or "").strip().lower().split()[0] if message.get("text") else ""
                    if not chat_id:
                        continue
                    if command == "/alerts":
                        state.alerts[chat_id] = True
                        rows = speed_tracker.update(await cade.leaderboard())
                        state.last_key[chat_id] = snapshot_key(rows)
                        await telegram.send(chat_id, format_message(rows, reset_at=cade.reset_at, period_date=cade.period_date))
                    elif command == "/copyplan":
                        parts = (message.get("text") or "").strip().split()
                        if len(parts) != 2:
                            await telegram.send(chat_id, "Usage: /copyplan 1000\nReplace 1000 with your available balance in Cade credits.")
                            continue
                        try:
                            balance = float(parts[1])
                            if balance <= 0:
                                raise ValueError
                        except ValueError:
                            await telegram.send(chat_id, "Balance must be a positive number. Example: /copyplan 1000")
                            continue
                        rows = await cade.leaderboard()
                        history_batches = await asyncio.gather(*(cade.prediction_history(row["wallet"]) for row in rows), return_exceptions=True)
                        histories = {row["wallet"]: batch for row, batch in zip(rows, history_batches) if isinstance(batch, list)}
                        if len(histories) != len(rows):
                            log.warning("copyplan history incomplete: %s/%s wallets", len(histories), len(rows))
                        plan = build_copy_plan(rows, histories, balance)
                        await telegram.send(chat_id, format_copy_plan(plan))
                    elif command in ("/stop", "/alertsoff"):
                        state.alerts[chat_id] = False
                        await telegram.send(chat_id, "Alerts are off. Send /alerts to turn them on again.")
                    elif command == "/status":
                        await telegram.send(chat_id, f"Alerts: {'on' if state.alerts.get(chat_id, False) else 'off'}\nPolling: every {POLL_SECONDS}s\nSpeed window: previous 3 hours")
                rows = speed_tracker.update(await cade.leaderboard())
                key = snapshot_key(rows)
                for chat_id, enabled in list(state.alerts.items()):
                    if enabled and state.last_key.get(chat_id) != key:
                        await telegram.send(chat_id, format_message(rows, reset_at=cade.reset_at, period_date=cade.period_date))
                        state.last_key[chat_id] = key
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                log.warning("network timeout during poll; retrying safely")
            except httpx.HTTPError:
                log.exception("HTTP error during poll; retrying safely")
            except Exception:
                log.exception("unexpected poll error; retrying safely")
            await asyncio.sleep(POLL_SECONDS)


@app.on_event("startup")
async def startup() -> None:
    app.state.bot_task = asyncio.create_task(run_bot())


@app.on_event("shutdown")
async def shutdown() -> None:
    task = getattr(app.state, "bot_task", None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
