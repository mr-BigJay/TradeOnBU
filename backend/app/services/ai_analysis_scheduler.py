from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import AiAnalysisSettings, SystemState
from app.services.siwc.credentials import get_credential_row
from app.services.siwc.oauth import has_plan_scope
from app.services.system_state import get_system_state

FAILURE_BACKOFF_MINUTES = 15
DEFAULT_AI_INTERVAL_MINUTES = 120


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def migrate_ai_schedule_settings(cfg: AiAnalysisSettings) -> None:
    """Move legacy 60-minute schedules to 120 minutes without disabling consent."""
    if cfg.interval_minutes == 60:
        cfg.interval_minutes = DEFAULT_AI_INTERVAL_MINUTES
    now = _utc_now()
    if cfg.next_run_at is not None:
        nr = _ensure_utc(cfg.next_run_at)
        if nr <= now:
            return
        if cfg.last_run_at:
            lr = _ensure_utc(cfg.last_run_at)
            expected = lr + timedelta(minutes=cfg.interval_minutes)
            if nr < expected - timedelta(minutes=5):
                cfg.next_run_at = expected
        elif nr <= now + timedelta(minutes=5) and cfg.enabled:
            cfg.next_run_at = now + timedelta(minutes=cfg.interval_minutes)


async def get_or_create_settings(session: AsyncSession) -> AiAnalysisSettings:
    row = (await session.execute(select(AiAnalysisSettings).where(AiAnalysisSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = AiAnalysisSettings(
            id=1,
            enabled=False,
            unattended_consent=False,
            interval_minutes=DEFAULT_AI_INTERVAL_MINUTES,
        )
        session.add(row)
        await session.flush()
    migrate_ai_schedule_settings(row)
    await session.flush()
    return row


async def scheduling_allowed(session: AsyncSession) -> tuple[bool, str | None]:
    cfg = await get_or_create_settings(session)
    if not cfg.unattended_consent:
        return False, "unattended_consent_required"
    if not cfg.enabled:
        return False, "scheduling_disabled"
    cred = await get_credential_row(session)
    if not cred or not cred.access_token_enc:
        return False, "not_connected"
    if not has_plan_scope(cred.scopes_json or []):
        return False, "plan_scope_missing"
    return True, None


def record_run_outcome(cfg: AiAnalysisSettings, run_status: str, error_detail: str | None = None) -> None:
    now = _utc_now()
    cfg.last_run_at = now
    interval = max(DEFAULT_AI_INTERVAL_MINUTES, cfg.interval_minutes)
    if run_status.startswith("completed"):
        cfg.last_success_at = now
        cfg.last_error = None
        cfg.next_run_at = now + timedelta(minutes=interval)
    else:
        cfg.last_error = error_detail or "run_failed"
        cfg.next_run_at = now + timedelta(minutes=FAILURE_BACKOFF_MINUTES)


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
    ok, _ = await scheduling_allowed(session)
    if not ok:
        return False
    cfg = await get_or_create_settings(session)
    if cfg.next_run_at is None:
        return True
    return _utc_now() >= _ensure_utc(cfg.next_run_at)
