"""Admin-only, read-only Bitunix Futures monitoring endpoint."""
from fastapi import APIRouter, Depends, HTTPException, Response

from app.routes.auth import require_dashboard_access
from app.services.bitunix_dashboard import get_bitunix_dashboard_snapshot
from app.services.bitunix_private_readonly import BitunixReadOnlyError

router = APIRouter(prefix="/api/gui/bitunix", tags=["bitunix"])


async def _require_admin(pair=Depends(require_dashboard_access)):
    # In session-auth mode require administrator; legacy API-key mode
    # is enforced by SessionSecurityMiddleware.ADMIN_PREFIXES.
    if pair is not None and not pair[0].is_admin:
        raise HTTPException(403, "Admin access required")


@router.get("/snapshot", dependencies=[Depends(_require_admin)])
async def bitunix_snapshot(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await get_bitunix_dashboard_snapshot()
    except (BitunixReadOnlyError, ValueError):
        # Never expose upstream body, credentials, account identifiers, or
        # HTTP details in a browser-visible error.
        raise HTTPException(
            502, "ارتباط با Bitunix ناموفق بود؛ مجوز API، IP و زمان سرور را بررسی کنید."
        ) from None
