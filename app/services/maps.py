import base64
import hashlib
import hmac
import json
import re
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


def _route_token(route_result, origin, destination, waypoints=None):
    payload = {
        "origin": {"lat": origin.lat, "lng": origin.lng},
        "destination": {"lat": destination.lat, "lng": destination.lng},
        "waypoints": [{"lat": point.lat, "lng": point.lng} for point in (waypoints or [])],
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
        properties.get("locality") or properties.get("district") or properties.get("suburb"),
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


def _predictions(features):
    predictions = []
    seen = set()
    for feature in features:
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
            dict.fromkeys(
                str(part)
                for part in [
                    properties.get("locality") or properties.get("district"),
                    properties.get("city") or properties.get("town") or properties.get("village"),
                    properties.get("state"),
                    properties.get("postcode"),
                ]
                if part
            )
        )
        predictions.append(
            {
                "id": "photon:" + ident,
                "primary": place["label"],
                "secondary": secondary,
                "place": place,
                "hasHouseNumber": bool(properties.get("housenumber")),
            }
        )
    return predictions


def _tomtom_headers(version, attributes, session_token=None):
    require(settings.tomtom_api_key, "TomTom API key is not configured", 503)
    headers = {
        "TomTom-Api-Key": settings.tomtom_api_key,
        "TomTom-Api-Version": str(version),
        "Attributes": attributes,
        "Accept": "application/json",
        "Accept-Language": "en-ZA,en;q=0.9",
        "User-Agent": "TAMP/1.0 (address search)",
    }
    if session_token:
        headers["Session-Id"] = session_token
    return headers


def _tomtom_place(result, preserve_coordinates=None):
    position = result.get("position", {})
    coordinates = position.get("coordinates", [])
    preferred = next(
        (
            item.get("position", {}).get("coordinates", [])
            for item in result.get("accessPoints", [])
            if item.get("preferredRouting")
        ),
        None,
    )
    coordinates = preferred or coordinates
    require(len(coordinates) >= 2, "TomTom result has no coordinates", 503)
    address = result.get("address", {})
    parts = [result.get("title"), *result.get("subtitles", [])]
    label = ", ".join(dict.fromkeys(str(part).strip() for part in parts if part))
    if not label:
        street = " ".join(
            str(part)
            for part in [address.get("houseNumber"), address.get("street")]
            if part
        )
        label = ", ".join(
            dict.fromkeys(
                str(part)
                for part in [
                    street,
                    address.get("neighborhood") or address.get("municipalitySubdivision"),
                    address.get("municipality"),
                    address.get("postalCode"),
                    address.get("countrySubdivision"),
                    address.get("country"),
                ]
                if part
            )
        )
    require(label, "TomTom result has no address label", 503)
    lng, lat = preserve_coordinates or coordinates[:2]
    return {
        "label": label,
        "province": address.get("countrySubdivision") or "",
        "lat": float(lat),
        "lng": float(lng),
    }


async def _tomtom_autocomplete(data, query):
    result = await provider_request(
        "POST",
        settings.tomtom_url.rstrip("/") + "/maps/orbis/places/suggest",
        json={
            "query": query,
            "maxResults": 10,
            "filters": {
                "countryCodesIso2": ["ZA"],
                "types": ["address", "street", "intersection", "poi", "area"],
            },
        },
        headers=_tomtom_headers(
            3,
            "results(id,type,title,subtitles,more)",
            data.sessionToken,
        ),
    )
    type_path = {
        "address": "addresses",
        "street": "streets",
        "intersection": "intersections",
        "poi": "pois",
        "area": "areas",
    }
    predictions = []
    seen = set()
    for item in result.get("results", []):
        item_id = item.get("id")
        path_type = type_path.get(item.get("type"))
        title = str(item.get("title") or "").strip()
        if not item_id or not path_type or not title:
            continue
        subtitles = [str(value).strip() for value in item.get("subtitles", []) if value]
        display = ", ".join(dict.fromkeys([title, *subtitles]))
        normalized = display.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        predictions.append(
            {
                "id": f"tomtom:{path_type}:{quote(str(item_id), safe='')}",
                "primary": title,
                "secondary": ", ".join(subtitles),
            }
        )
    return predictions


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
    query = " ".join(data.query.strip().split())
    if settings.geocoding_provider == "tomtom":
        # TomTom requires one UUID to follow the complete suggest -> details
        # journey. Do not serve suggestions created under another session.
        cache_key = f"tomtom:{data.sessionToken or 'anonymous'}:{query.casefold()}"
        cached = _autocomplete_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        predictions = await _tomtom_autocomplete(data, query)
        if len(_autocomplete_cache) >= 500:
            _autocomplete_cache.clear()
        _autocomplete_cache[cache_key] = (time.monotonic(), predictions)
        return predictions
    if settings.geocoding_provider != "photon" or not settings.photon_url:
        return _local_autocomplete(data.query)
    cache_key = query.casefold()
    cached = _autocomplete_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    common = {
        "limit": 10,
        "lang": "en",
        "bbox": "16,-35,33,-22",
        "countrycode": "ZA",
        "lat": -30.5595,
        "lon": 22.9375,
        "location_bias_scale": 0.2,
    }
    try:
        result = await provider_request(
            "GET",
            settings.photon_url.rstrip("/") + "/api",
            params={"q": query, **common},
            headers={"User-Agent": "TAMP/1.0 (address search)"},
        )
    except AppError:
        # Keep major-city selection usable during a temporary provider outage.
        return _local_autocomplete(query)
    features = list(result.get("features", []))
    numbered = re.match(r"^\s*([0-9]+[A-Za-z]?(?:[-/][0-9A-Za-z]+)?)\s+([^,]+)(?:,\s*(.+))?$", query)
    if numbered:
        structured_params = {
            "housenumber": numbered.group(1),
            "street": numbered.group(2).strip(),
            "countrycode": "ZA",
            "limit": 10,
            "lang": "en",
        }
        if numbered.group(3):
            structured_params["city"] = numbered.group(3).split(",")[-1].strip()
        try:
            structured = await provider_request(
                "GET",
                settings.photon_url.rstrip("/") + "/structured",
                params=structured_params,
                headers={"User-Agent": "TAMP/1.0 (structured address search)"},
            )
            features = [*structured.get("features", []), *features]
        except AppError:
            # Some third-party Photon instances disable structured search.
            pass
    predictions = _predictions(features)
    predictions.sort(key=lambda item: not item.pop("hasHouseNumber"))
    predictions = predictions[:10]
    predictions = predictions or _local_autocomplete(query)
    if len(_autocomplete_cache) >= 500:
        _autocomplete_cache.clear()
    _autocomplete_cache[cache_key] = (time.monotonic(), predictions)
    return predictions


async def details(data):
    if data.placeId.startswith("tomtom:"):
        require(settings.geocoding_provider == "tomtom", "TomTom geocoding is not active", 409)
        try:
            _, path_type, encoded_id = data.placeId.split(":", 2)
        except ValueError as exc:
            raise AppError("Place not found", 404) from exc
        require(
            path_type in {"addresses", "streets", "intersections", "pois", "areas"},
            "Place not found",
            404,
        )
        result = await provider_request(
            "GET",
            f"{settings.tomtom_url.rstrip('/')}/maps/orbis/places/details/{path_type}/{quote(unquote(encoded_id), safe='')}",
            headers=_tomtom_headers(
                3,
                "id,type,title,subtitles,position,address,accessPoints",
                data.sessionToken,
            ),
        )
        return _tomtom_place(result)
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


async def reverse(data):
    if settings.geocoding_provider == "tomtom":
        result = await provider_request(
            "GET",
            settings.tomtom_url.rstrip("/") + "/maps/orbis/places/reverseGeocode",
            params={
                "position": f"{data.lng},{data.lat}",
                "radiusInMeters": 250,
            },
            headers=_tomtom_headers(
                2,
                "results(id,type,title,position,address,accessPoints)",
            ),
        )
        results = result.get("results", [])
        require(results, "No address was found near your current location", 404)
        return _tomtom_place(results[0], preserve_coordinates=(data.lng, data.lat))
    if settings.geocoding_provider != "photon" or not settings.photon_url:
        return {
            "label": f"Current location ({data.lat:.6f}, {data.lng:.6f})",
            "province": "",
            "lat": data.lat,
            "lng": data.lng,
        }
    result = await provider_request(
        "GET",
        settings.photon_url.rstrip("/") + "/reverse",
        params={
            "lat": data.lat,
            "lon": data.lng,
            "radius": 0.25,
            "limit": 1,
            "lang": "en",
        },
        headers={"User-Agent": "TAMP/1.0 (current location lookup)"},
    )
    features = result.get("features", [])
    require(features, "No address was found near your current location", 404)
    place = _feature_place(features[0])
    # Preserve the device coordinate. The returned OSM feature may represent
    # the centre of a building or road segment rather than the user's position.
    return {**place, "lat": data.lat, "lng": data.lng}


async def route(data):
    if settings.routing_provider != "valhalla" or not settings.valhalla_url:
        return None
    result = await provider_request(
        "POST",
        settings.valhalla_url.rstrip("/") + "/route",
        json={
            "locations": [
                {"lat": point.lat, "lon": point.lng, "type": "break"}
                for point in [data.origin, *data.waypoints, data.destination]
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
    token, expires_at = _route_token(route_result, data.origin, data.destination, data.waypoints)
    return {**route_result, "routeToken": token, "expiresAt": expires_at}
