import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

from app.core.config import settings
from app.core.errors import AppError, require
from app.core.security import now

PLACES = json.loads(Path(__file__).with_name("places.json").read_text())
_provider_client = None
_autocomplete_cache = {}


async def close_provider_client():
    global _provider_client
    if _provider_client is not None:
        await _provider_client.aclose()
        _provider_client = None


def _route_token(route_result, origin, destination):
    payload = {
        "origin": {"lat": origin.lat, "lng": origin.lng},
        "destination": {"lat": destination.lat, "lng": destination.lng},
        "route": route_result,
        "expiresAt": (now() + timedelta(minutes=10)).isoformat(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(settings.secret_key.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode().rstrip("="), payload["expiresAt"]


def verify_route_token(token):
    try:
        packed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        raw, supplied = packed[:-32], packed[-32:]
        expected = hmac.new(settings.secret_key.encode(), raw, hashlib.sha256).digest()
        require(hmac.compare_digest(supplied, expected), "Invalid route proof", 409)
        payload = json.loads(raw)
        require(datetime.fromisoformat(payload["expiresAt"]) > now(), "Route calculation expired", 409)
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise AppError("Invalid route proof", 409) from exc


async def provider_request(method, url, **kwargs):
    global _provider_client
    try:
        if _provider_client is None:
            _provider_client = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0),
                follow_redirects=False,
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
            )
        response = await _provider_client.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppError("Mapping provider unavailable", 503) from exc


def _local_autocomplete(query):
    return [
        {
            "id": "local:" + key,
            "primary": p["label"] + " (city centre)",
            "secondary": p["province"],
            "place": p,
        }
        for key, p in PLACES.items()
        if query.lower() in p["label"].lower()
    ][:10]


def _feature_place(feature):
    coordinates = feature.get("geometry", {}).get("coordinates", [])
    properties = feature.get("properties", {})
    require(len(coordinates) >= 2, "Place result has no coordinates", 503)
    street = " ".join(
        str(part)
        for part in [properties.get("housenumber"), properties.get("street")]
        if part
    )
    parts = [
        street or properties.get("name"),
        properties.get("district") or properties.get("locality"),
        properties.get("city") or properties.get("town") or properties.get("village"),
        properties.get("postcode"),
        properties.get("state"),
        properties.get("country"),
    ]
    label = ", ".join(dict.fromkeys(str(part) for part in parts if part))
    return {
        "label": label or "Selected location",
        "province": properties.get("state") or properties.get("county") or "",
        "lat": float(coordinates[1]),
        "lng": float(coordinates[0]),
    }


def _decode_polyline6(encoded):
    """Decode Valhalla's six-decimal polyline into GeoJSON [lng, lat] points."""
    coordinates = []
    index = latitude = longitude = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            result = shift = 0
            while True:
                require(index < len(encoded), "Routing provider returned invalid geometry", 503)
                value = ord(encoded[index]) - 63
                index += 1
                result |= (value & 0x1F) << shift
                shift += 5
                if value < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        coordinates.append([longitude / 1_000_000, latitude / 1_000_000])
    return coordinates


async def autocomplete(data):
    if settings.geocoding_provider != "photon" or not settings.photon_url:
        return _local_autocomplete(data.query)
    query = " ".join(data.query.strip().split())
    cache_key = query.casefold()
    cached = _autocomplete_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    result = await provider_request(
        "GET",
        settings.photon_url.rstrip("/") + "/api",
        params={
            "q": query,
            "limit": 8,
            "lang": "en",
            "bbox": "16,-35,33,-22",
            "countrycode": "ZA",
            "lat": -30.5595,
            "lon": 22.9375,
            "location_bias_scale": 0.2,
        },
        headers={"User-Agent": "TAMP/1.0 (address search)"},
    )
    predictions = []
    seen = set()
    for feature in result.get("features", []):
        try:
            place = _feature_place(feature)
        except AppError:
            continue
        properties = feature.get("properties", {})
        normalized = place["label"].casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        ident = quote(json.dumps(place, separators=(",", ":")), safe="")
        secondary = ", ".join(
            part
            for part in [
                properties.get("city") or properties.get("town") or properties.get("village"),
                properties.get("state"),
                properties.get("country"),
            ]
            if part
        )
        predictions.append(
            {
                "id": "photon:" + ident,
                "primary": place["label"],
                "secondary": secondary,
                "place": place,
            }
        )
    predictions = predictions or _local_autocomplete(query)
    if len(_autocomplete_cache) >= 500:
        _autocomplete_cache.clear()
    _autocomplete_cache[cache_key] = (time.monotonic(), predictions)
    return predictions


async def details(data):
    if data.placeId.startswith("photon:"):
        try:
            return json.loads(unquote(data.placeId[7:]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AppError("Place not found", 404) from exc
    if data.placeId.startswith("local:"):
        place = PLACES.get(data.placeId[6:])
        require(place is not None, "Place not found", 404)
        return place
    raise AppError("Place not found", 404)


async def route(data):
    if settings.routing_provider != "valhalla" or not settings.valhalla_url:
        return None
    result = await provider_request(
        "POST",
        settings.valhalla_url.rstrip("/") + "/route",
        json={
            "locations": [
                {"lat": data.origin.lat, "lon": data.origin.lng, "type": "break"},
                {"lat": data.destination.lat, "lon": data.destination.lng, "type": "break"},
            ],
            "costing": "truck",
            "units": "kilometers",
            "shape_format": "geojson",
            "directions_options": {"units": "kilometers"},
        },
        headers={"User-Agent": "TAMP/1.0 (freight routing)"},
    )
    trip = result.get("trip", {})
    legs = trip.get("legs", [])
    if not legs:
        return None
    coordinates = []
    for leg in legs:
        shape = leg.get("shape")
        if isinstance(shape, dict) and shape.get("type") == "LineString":
            points = shape.get("coordinates", [])
            coordinates.extend(points[1:] if coordinates else points)
        elif isinstance(shape, str):
            points = _decode_polyline6(shape)
            coordinates.extend(points[1:] if coordinates else points)
    require(len(coordinates) >= 2, "Routing provider returned no route geometry", 503)
    summary = trip.get("summary", {})
    route_result = {
        "distanceKm": round(float(summary.get("length", 0)), 1),
        "durationMin": round(float(summary.get("time", 0)) / 60),
        "geometry": {"type": "LineString", "coordinates": coordinates},
        "provider": "valhalla",
    }
    token, expires_at = _route_token(route_result, data.origin, data.destination)
    return {**route_result, "routeToken": token, "expiresAt": expires_at}
