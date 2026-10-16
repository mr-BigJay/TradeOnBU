"""Fixed hourly AI analysis slots at :30 Asia/Tehran (+ offset seconds)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.tehran_time import TEHRAN

SCHEDULE_OFFSET_SECONDS = 10
SLOT_WINDOW = timedelta(hours=1)


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def slot_fire_tehran(local_slot: datetime) -> datetime:
    """UTC instant when analysis may start (local :30:00 + offset)."""
    fire_local = local_slot.replace(second=SCHEDULE_OFFSET_SECONDS, microsecond=0)
    if local_slot.minute != 30 or local_slot.second != 0 or local_slot.microsecond != 0:
        fire_local = local_slot.replace(minute=30, second=SCHEDULE_OFFSET_SECONDS, microsecond=0)
    return fire_local.astimezone(timezone.utc)


def slot_fire_ms(fire_utc: datetime) -> int:
    return int(_ensure_utc(fire_utc).timestamp() * 1000)


def active_due_slot_fire_utc(now_utc: datetime | None = None) -> datetime | None:
    """
    The single hourly slot whose window contains ``now`` (Tehran :30 + offset cadence).

    Missed earlier slots are not returned after the window closes (no catch-up burst).
    """
    now_utc = _ensure_utc(now_utc or datetime.now(timezone.utc))
    now_t = now_utc.astimezone(TEHRAN)
    fire_t = now_t.replace(minute=30, second=SCHEDULE_OFFSET_SECONDS, microsecond=0)
    if now_t < fire_t:
        fire_t -= timedelta(hours=1)
    if now_t >= fire_t + SLOT_WINDOW:
        return None
    return fire_t.astimezone(timezone.utc)


def next_tehran_scheduled_fire_utc(now_utc: datetime | None = None) -> datetime:
    """Next upcoming slot start (strictly after ``now`` if already past this hour's fire)."""
    now_utc = _ensure_utc(now_utc or datetime.now(timezone.utc))
    now_t = now_utc.astimezone(TEHRAN)
    fire_t = now_t.replace(minute=30, second=SCHEDULE_OFFSET_SECONDS, microsecond=0)
    if now_t >= fire_t:
        fire_t += timedelta(hours=1)
    return fire_t.astimezone(timezone.utc)


def sync_next_run_at_from_tehran_schedule(cfg, now_utc: datetime | None = None) -> None:
    cfg.next_run_at = next_tehran_scheduled_fire_utc(now_utc)
