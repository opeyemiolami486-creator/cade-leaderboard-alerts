from datetime import datetime, timedelta, timezone

import pytest

from app import SpeedTracker, build_copy_plan, countdown, format_message, normalize, snapshot_key


def test_normalize_sorts_by_predictions_not_source_rank():
    payload = {"entries": [
        {"rank": 1, "username": "source-first", "prediction_count": 2, "wallet_address": "a"},
        {"rank": 7, "username": "most", "prediction_count": 9, "wallet_address": "b"},
        {"rank": 3, "username": "middle", "prediction_count": 5, "wallet_address": "c"},
    ]}
    rows = normalize(payload)
    assert [(r["rank"], r["username"], r["predictions"]) for r in rows] == [(1, "most", 9), (2, "middle", 5), (3, "source-first", 2)]


def test_normalize_rejects_missing_entries():
    with pytest.raises(ValueError):
        normalize({})


def test_three_hour_speed_is_delta_divided_by_elapsed_hours():
    tracker = SpeedTracker()
    start = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    row = {"rank": 1, "username": "alpha", "wallet": "w", "predictions": 10}
    assert tracker.update([row], start)[0]["trades_per_hour"] == 0
    row = {**row, "predictions": 16}
    measured = tracker.update([row], start + timedelta(hours=2))[0]["trades_per_hour"]
    assert measured == pytest.approx(3.0)


def test_speed_resets_when_cade_daily_counter_goes_backwards():
    tracker = SpeedTracker()
    start = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    row = {"rank": 1, "username": "alpha", "wallet": "w", "predictions": 10}
    tracker.update([row], start)
    reset = {**row, "predictions": 2}
    assert tracker.update([reset], start + timedelta(hours=1))[0]["trades_per_hour"] == 0


def test_countdown_uses_next_utc_midnight():
    now = datetime(2026, 9, 18, 23, 59, 50, tzinfo=timezone.utc)
    assert countdown(now) == "00:00:10"


def test_message_contains_predictions_speed_and_reset():
    now = datetime(2026, 9, 18, 23, 59, 50, tzinfo=timezone.utc)
    text = format_message([{"rank": 1, "username": "alpha", "predictions": 12, "trades_per_hour": 4.25}], now)
    assert "alpha" in text and "12" in text and "4.25/hr" in text and "00:00:10" in text


def test_snapshot_key_is_stable_and_changes_on_prediction_count():
    one = [{"username": "alpha", "wallet": "w", "predictions": 1}]
    two = [{"username": "alpha", "wallet": "w", "predictions": 2}]
    assert snapshot_key(one) == snapshot_key(one)
    assert snapshot_key(one) != snapshot_key(two)


def test_copy_plan_selects_highest_four_day_realized_profit_and_sizes_manually():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"rank": 1, "username": "alpha", "wallet": "a", "predictions": 20},
        {"rank": 2, "username": "beta", "wallet": "b", "predictions": 10},
    ]
    histories = {
        "a": [{"created_at": "2026-09-18T12:00:00Z", "lifecycle_state": "resolved", "credit_payout_raw": "150", "net_stake_raw": "100"}],
        "b": [{"created_at": "2026-09-18T12:00:00Z", "lifecycle_state": "resolved", "credit_payout_raw": "300", "net_stake_raw": "100"}],
    }
    plan = build_copy_plan(rows, histories, 1000, now, min_settled_trades=1, min_roi_pct=0)
    assert plan["winner"]["row"]["username"] == "beta"
    assert plan["per_trade_amount"] == pytest.approx(10)
    assert plan["max_total_amount"] == pytest.approx(100)


def test_copy_plan_excludes_old_and_unresolved_trades():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    rows = [{"rank": 1, "username": "alpha", "wallet": "a", "predictions": 20}]
    histories = {"a": [
        {"created_at": "2026-09-10T12:00:00Z", "lifecycle_state": "resolved", "credit_payout_raw": "999", "net_stake_raw": "0"},
        {"created_at": "2026-09-19T11:00:00Z", "lifecycle_state": "open", "credit_payout_raw": "999", "net_stake_raw": "0"},
    ]}
    plan = build_copy_plan(rows, histories, 1000, now, min_roi_pct=0)
    assert plan["winner"] is None


def test_copy_plan_does_not_fallback_below_roi_threshold():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    rows = [{"rank": 1, "username": "alpha", "wallet": "a", "predictions": 20}]
    histories = {"a": [{"created_at": "2026-09-18T12:00:00Z", "lifecycle_state": "resolved", "credit_payout_raw": "110", "net_stake_raw": "100"}]}
    plan = build_copy_plan(rows, histories, 1000, now, min_settled_trades=1, min_roi_pct=1000)
    assert plan["winner"] is None
