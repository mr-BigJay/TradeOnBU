from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.services.siwc.connect_flow import consume_connect_session

router = APIRouter(prefix="/api/public/siwc", tags=["siwc-public"])


class PublicConnectCompleteBody(BaseModel):
    connect_token: str = Field(min_length=16, max_length=64)
    oauth_state: str = Field(min_length=8, max_length=128)
    code: str = Field(min_length=8)
    callback_client_id: str | None = Field(default=None, max_length=64)


@router.post("/connect/complete")
async def public_connect_complete(
    body: PublicConnectCompleteBody,
    x_connect_secret: str | None = Header(default=None, alias="X-Connect-Secret"),
):
    if not x_connect_secret or len(x_connect_secret) < 16:
        raise HTTPException(401, "Missing X-Connect-Secret header")
    return await consume_connect_session(
        connect_token=body.connect_token,
        connect_secret=x_connect_secret,
        oauth_state=body.oauth_state,
        code=body.code,
        callback_client_id=body.callback_client_id,
    )
