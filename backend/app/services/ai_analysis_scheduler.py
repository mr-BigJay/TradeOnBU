from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_effective_settings
from app.db import SessionLocal
from app.models import AiAnalysisSettings, SystemState
from app.services.siwc.credentials import get_credential_row
from app.services.siwc.oauth import has_plan_scope
from app.services.system_state import get_system_state

FAILURE_BACKOFF_MINUTES = 15
MIN_AI_INTERVAL_MINUTES = 60
MAX_AI_INTERVAL_MINUTES = 360
DEFAULT_AI_INTERVAL_MINUTES = MIN_AI_INTERVAL_MINUTES
LEGACY_AUTO_MIGRATED_INTERVAL_MINUTES = 120


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _effective_interval_minutes(cfg: AiAnalysisSettings) -> int:
    raw = cfg.interval_minutes if cfg.interval_minutes is not None else DEFAULT_AI_INTERVAL_MINUTES
    return min(MAX_AI_INTERVAL_MINUTES, max(MIN_AI_INTERVAL_MINUTES, int(raw)))


def _earliest_next_run(cfg: AiAnalysisSettings, interval: int, now: datetime) -> datetime:
    if cfg.last_success_at:
        return max(_ensure_utc(cfg.last_success_at) + timedelta(minutes=interval), now)
    if cfg.last_run_at:
        return max(_ensure_utc(cfg.last_run_at) + timedelta(minutes=interval), now)
    return now


def _reschedule_next_run_after_interval_change(
    cfg: AiAnalysisSettings,
    now: datetime | None = None,
    *,
    previous_interval_minutes: int | None = None,
) -> None:
    """
    Recompute next_run_at for a new interval.

    When shortening the interval, snap to the new cadence (never before
    last_success_at + new interval). When lengthening, do not move next_run
    earlier than the previous scheduled time.
    """
    now = now or _utc_now()
    interval = _effective_interval_minutes(cfg)
    floor = _earliest_next_run(cfg, interval, now)
    current = _ensure_utc(cfg.next_run_at) if cfg.next_run_at else None

    if previous_interval_minutes is not None and previous_interval_minutes > interval:
        cfg.next_run_at = floor
        return

    if current and current > now:
        cfg.next_run_at = max(current, floor)
    else:
        cfg.next_run_at = floor


def migrate_ai_schedule_settings(cfg: AiAnalysisSettings) -> bool:
    """
    One-time restore for rows auto-bumped 60→120 by legacy migration.

    Skips installs where the user explicitly kept 120 (interval_migrated_from_60
    cleared via settings save) or chose another interval.
    """
    if not cfg.interval_migrated_from_60:
        return False
    if cfg.interval_minutes != LEGACY_AUTO_MIGRATED_INTERVAL_MINUTES:
        return False
    previous = cfg.interval_minutes
    cfg.interval_minutes = DEFAULT_AI_INTERVAL_MINUTES
    cfg.interval_migrated_from_60 = False
    _reschedule_next_run_after_interval_change(
        cfg, previous_interval_minutes=previous
    )
    return True


def apply_user_schedule_interval_choice(
    cfg: AiAnalysisSettings,
    *,
    previous_interval_minutes: int,
    requested_interval_minutes: int,
) -> None:
    """
    Record an explicit user interval choice without touching unrelated fields.

    Clears the legacy migration marker only when the user changes the interval
    or re-saves 120 while the legacy auto-migrate flag is still set (confirming
    an intentional 120-minute schedule).
    """
    cfg.interval_minutes = requested_interval_minutes
    if requested_interval_minutes != previous_interval_minutes:
        cfg.interval_migrated_from_60 = False
    elif cfg.interval_migrated_from_60 and requested_interval_minutes == LEGACY_AUTO_MIGRATED_INTERVAL_MINUTES:
        cfg.interval_migrated_from_60 = False
    if requested_interval_minutes != previous_interval_minutes:
        _reschedule_next_run_after_interval_change(
            cfg, previous_interval_minutes=previous_interval_minutes
        )


async def get_or_create_settings(
    session: AsyncSession,
    *,
    autocommit_migration: bool = False,
    apply_legacy_restore: bool = True,
) -> AiAnalysisSettings:
    row = (await session.execute(select(AiAnalysisSettings).where(AiAnalysisSettings.id == 1))).scalar_one_or_none()
    created = False
    if row is None:
        row = AiAnalysisSettings(
            id=1,
            enabled=False,
            unattended_consent=False,
            interval_minutes=DEFAULT_AI_INTERVAL_MINUTES,
        )
        session.add(row)
        created = True
        await session.flush()
    migrated = False
    if apply_legacy_restore and not created:
        migrated = migrate_ai_schedule_settings(row)
    if migrated:
        await session.flush()
        if autocommit_migration:
            await session.commit()
    return row


def confirm_legacy_one_twenty_schedule(cfg: AiAnalysisSettings) -> None:
    """User explicitly kept 120 minutes; skip automatic restore to 60."""
    if cfg.interval_migrated_from_60:
        cfg.interval_migrated_from_60 = False


async def scheduling_allowed(
    session: AsyncSession,
    cfg: AiAnalysisSettings | None = None,
) -> tuple[bool, str | None]:
    cfg = cfg or await get_or_create_settings(session)
    if not cfg.unattended_consent:
        return False, "unattended_consent_required"
    if not cfg.enabled:
        return False, "scheduling_disabled"
    settings = get_effective_settings()
    if settings.ai_provider == "openai_api":
        if not settings.openai_api_key.strip():
            return False, "openai_api_key_missing"
        return True, None
    cred = await get_credential_row(session)
    if not cred or not cred.access_token_enc:
        return False, "not_connected"
    if not has_plan_scope(cred.scopes_json or []):
        return False, "plan_scope_missing"
    return True, None


def _next_regular_run_at(cfg: AiAnalysisSettings, now: datetime) -> datetime:
    interval = _effective_interval_minutes(cfg)
    anchor = cfg.last_success_at or cfg.last_run_at or now
    anchor = _ensure_utc(anchor)
    nxt = anchor + timedelta(minutes=interval)
    if nxt <= now:
        nxt = now + timedelta(minutes=interval)
    return nxt


def record_run_outcome(
    cfg: AiAnalysisSettings,
    run_status: str,
    error_detail: str | None = None,
    *,
    trigger: str = "scheduled",
) -> None:
    now = _utc_now()
    cfg.last_run_at = now
    interval = _effective_interval_minutes(cfg)
    if run_status.startswith("completed"):
        cfg.last_success_at = now
        cfg.last_error = None
        cfg.auto_retry_pending = False
        cfg.next_run_at = now + timedelta(minutes=interval)
        return

    cfg.last_error = error_detail or "run_failed"
    if trigger != "scheduled":
        return

    if not cfg.auto_retry_pending:
        cfg.auto_retry_pending = True
        cfg.next_run_at = now + timedelta(minutes=FAILURE_BACKOFF_MINUTES)
        return

    cfg.auto_retry_pending = False
    cfg.next_run_at = _next_regular_run_at(cfg, now)


async def _try_acquire_analysis_lock(session: AsyncSession, owner: str, ttl_seconds: int) -> bool:
    await get_system_state(session)
    now_ms = int(_utc_now().timestamp() * 1000)
    until_ms = now_ms + ttl_seconds * 1000
    stmt = (
        update(SystemState)
        .where(SystemState.id == 1)
        .where(
            or_(
                SystemState.ai_analysis_lock_until_ms.is_(None),
                SystemState.ai_analysis_lock_until_ms <= now_ms,
            )
        )
        .values(
            ai_analysis_lock_owner=owner,
            ai_analysis_lock_until_ms=until_ms,
        )
    )
    result = await session.execute(stmt)
    if result.rowcount == 0:
        return False
    state = (await session.execute(select(SystemState).where(SystemState.id == 1))).scalar_one_or_none()
    return bool(state and state.ai_analysis_lock_owner == owner and (state.ai_analysis_lock_until_ms or 0) > now_ms)


async def _release_analysis_lock(session: AsyncSession, owner: str) -> bool:
    state = (await session.execute(select(SystemState).where(SystemState.id == 1))).scalar_one_or_none()
    if state and state.ai_analysis_lock_owner == owner:
        state.ai_analysis_lock_owner = None
        state.ai_analysis_lock_until_ms = 0
        return True
    return False


async def acquire_analysis_lock(owner: str, ttl_seconds: int | None = None) -> bool:
    if ttl_seconds is None:
        from app.config import get_effective_settings

        ttl_seconds = int(get_effective_settings().ai_analysis_lock_ttl_seconds)
    async with SessionLocal() as session:
        ok = await _try_acquire_analysis_lock(session, owner, ttl_seconds)
        if ok:
            await session.commit()
            return True
        await session.rollback()
        return False


async def release_analysis_lock(owner: str) -> bool:
    async with SessionLocal() as session:
        released = await _release_analysis_lock(session, owner)
        if released:
            await session.commit()
            return True
        await session.rollback()
        return False


async def is_analysis_lock_held() -> bool:
    async with SessionLocal() as session:
        state = (await session.execute(select(SystemState).where(SystemState.id == 1))).scalar_one_or_none()
        if not state or not state.ai_analysis_lock_owner:
            return False
        now_ms = int(_utc_now().timestamp() * 1000)
        return (state.ai_analysis_lock_until_ms or 0) > now_ms


async def should_run_scheduled(session: AsyncSession) -> bool:
    cfg = await get_or_create_settings(session, autocommit_migration=True)
    ok, _ = await scheduling_allowed(session, cfg)
    if not ok:
        return False
    if cfg.next_run_at is None:
        return True
    return _utc_now() >= _ensure_utc(cfg.next_run_at)
