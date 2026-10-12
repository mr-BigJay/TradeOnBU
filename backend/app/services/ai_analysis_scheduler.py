from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiAnalysisSettings, SystemState
from app.services.siwc.credentials import get_credential_row
from app.services.siwc.oauth import has_plan_scope
from app.services.system_state import get_system_state


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def get_or_create_settings(session: AsyncSession) -> AiAnalysisSettings:
    row = (await session.execute(select(AiAnalysisSettings).where(AiAnalysisSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = AiAnalysisSettings(id=1, enabled=False, unattended_consent=False)
        session.add(row)
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


async def try_acquire_analysis_lock(session: AsyncSession, owner: str, ttl_seconds: int = 900) -> bool:
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
                SystemState.ai_analysis_lock_owner == owner,
            )
        )
        .values(
            ai_analysis_lock_owner=owner,
            ai_analysis_lock_until_ms=until_ms,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    if result.rowcount == 0:
        return False
    state = (await session.execute(select(SystemState).where(SystemState.id == 1))).scalar_one_or_none()
    return bool(state and state.ai_analysis_lock_owner == owner and (state.ai_analysis_lock_until_ms or 0) > now_ms)


async def release_analysis_lock(session: AsyncSession, owner: str) -> None:
    state = (await session.execute(select(SystemState).where(SystemState.id == 1))).scalar_one_or_none()
    if state and state.ai_analysis_lock_owner == owner:
        state.ai_analysis_lock_owner = None
        state.ai_analysis_lock_until_ms = 0
        await session.flush()


async def should_run_scheduled(session: AsyncSession) -> bool:
    ok, _ = await scheduling_allowed(session)
    if not ok:
        return False
    cfg = await get_or_create_settings(session)
    if cfg.last_run_at:
        interval = max(15, cfg.interval_minutes)
        next_at = _ensure_utc(cfg.last_run_at) + timedelta(minutes=interval)
        if cfg.next_run_at:
            next_at = min(next_at, _ensure_utc(cfg.next_run_at))
        if _utc_now() < next_at:
            return False
    return True


def bump_next_run(cfg: AiAnalysisSettings) -> None:
    interval = max(15, cfg.interval_minutes)
    cfg.next_run_at = _utc_now() + timedelta(minutes=interval)
