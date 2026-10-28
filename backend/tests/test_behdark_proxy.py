import pytest
import httpx
from unittest.mock import AsyncMock, patch
from app.main import app

@pytest.mark.asyncio
async def test_behdark_auth_required(monkeypatch):
    monkeypatch.setenv('SESSION_AUTH_ENABLED','1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.get('/api/gui/behdark/snapshot')).status_code==401
        assert (await c.post('/api/gui/behdark/capital',json={'amount':1000})).status_code==401

@pytest.mark.asyncio
async def test_behdark_disabled_does_not_break_main_health(monkeypatch):
    monkeypatch.setenv('SESSION_AUTH_ENABLED','0');monkeypatch.delenv('BEHDARK_URL',raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'X-API-Key':'test-dashboard-key-0123456789'}) as c:
        assert (await c.get('/api/gui/behdark/snapshot')).status_code==503
        assert (await c.get('/api/health')).status_code==200
        assert (await c.get('/api/gui/behdark/arbitrary')).status_code==404

@pytest.mark.asyncio
async def test_behdark_write_requires_admin_key(monkeypatch):
    monkeypatch.setenv('SESSION_AUTH_ENABLED','0')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'X-API-Key':'test-dashboard-key-0123456789'}) as c:
        assert (await c.put('/api/gui/behdark/settings',json={})).status_code==401
