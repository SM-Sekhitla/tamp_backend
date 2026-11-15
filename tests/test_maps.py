from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.services import maps


def route_input():
    return SimpleNamespace(
        origin=SimpleNamespace(lat=-25.7479, lng=28.2293),
        destination=SimpleNamespace(lat=-25.7258, lng=28.2437),
        waypoints=[],
    )


@pytest.mark.asyncio
async def test_route_falls_back_when_provider_is_unavailable(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        raise AppError("Mapping provider unavailable", 503)

    monkeypatch.setattr(maps, "provider_request", unavailable)
    result = await maps.route(route_input())

    assert result["provider"] == "estimated"
    assert result["distanceKm"] > 0
    assert result["durationMin"] > 0
    assert len(result["geometry"]["coordinates"]) == 2
    assert maps.verify_route_token(result["routeToken"])["route"]["provider"] == "estimated"


@pytest.mark.asyncio
async def test_route_falls_back_when_provider_returns_no_legs(monkeypatch):
    async def empty_route(*_args, **_kwargs):
        return {"trip": {"legs": []}}

    monkeypatch.setattr(maps, "provider_request", empty_route)
    result = await maps.route(route_input())

    assert result["provider"] == "estimated"
    assert result["expiresAt"]
