from __future__ import annotations

import asyncio
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

CADE_URL = os.getenv("CADE_LEADERBOARD_URL", "https://cade.market/api/leaderboard?period=day")
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "15")))
TOP_N = max(1, min(10, int(os.getenv("TOP_N", "10"))))
RESET_HOUR_UTC = int(os.getenv("RESET_HOUR_UTC", "0")) % 24
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
SPEED_WINDOW_HOURS = 3.0


def countdown(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    boundary = now.replace(hour=RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if boundary <= now:
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


def format_message(rows: list[dict[str, Any]], now: datetime | None = None) -> str:
    generated = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = ["📊 <b>Cade top traders — predictions</b>", f"24h window • checked {generated}", "Trades/hour = speed measured from samples in the previous 3 hours.", f"Next daily reset in <b>{countdown(now)}</b>", ""]
    for row in rows:
        name = row["username"].replace("<", "&lt;").replace(">", "&gt;")
        speed = row.get("trades_per_hour", 0.0)
        lines.append(f"<b>{row['rank']}.</b> {name} — <b>{row['predictions']}</b> predictions — <b>{speed:.2f}/hr</b>")
    return "\n".join(lines)


@dataclass
class State:
    alerts: dict[int, bool]
    last_key: dict[int, str]
    offset: int = 0


class CadeClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def leaderboard(self) -> list[dict[str, Any]]:
        response = await self.client.get(CADE_URL)
        response.raise_for_status()
        return normalize(response.json())


class TelegramClient:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client, self.base = client, f"https://api.telegram.org/bot{token}"

    async def updates(self, offset: int) -> list[dict[str, Any]]:
        response = await self.client.get(f"{self.base}/getUpdates", params={"timeout": 10, "offset": offset})
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
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
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
                        await telegram.send(chat_id, format_message(rows))
                    elif command in ("/stop", "/alertsoff"):
                        state.alerts[chat_id] = False
                        await telegram.send(chat_id, "Alerts are off. Send /alerts to turn them on again.")
                    elif command == "/status":
                        await telegram.send(chat_id, f"Alerts: {'on' if state.alerts.get(chat_id, False) else 'off'}\nPolling: every {POLL_SECONDS}s\nSpeed window: previous 3 hours")
                rows = speed_tracker.update(await cade.leaderboard())
                key = snapshot_key(rows)
                for chat_id, enabled in list(state.alerts.items()):
                    if enabled and state.last_key.get(chat_id) != key:
                        await telegram.send(chat_id, format_message(rows))
                        state.last_key[chat_id] = key
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll cycle failed; retrying safely")
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
