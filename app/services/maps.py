import json
from pathlib import Path

import httpx

from app.core.config import settings
from app.core.errors import AppError, require

PLACES = json.loads(Path(__file__).with_name("places.json").read_text())


def google(method, url, **kwargs):
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppError("Mapping provider unavailable", 503) from exc


def autocomplete(data):
    if not settings.google_maps_api_key:
        return [
            {
                "id": "local:" + key,
                "primary": p["label"] + " (city centre)",
                "secondary": p["province"],
                "place": p,
            }
            for key, p in PLACES.items()
            if data.query.lower() in p["label"].lower()
        ][:10]
    result = google(
        "GET",
        "https://maps.googleapis.com/maps/api/place/autocomplete/json",
        params={
            "key": settings.google_maps_api_key,
            "input": data.query,
            "components": "country:za",
            **({"sessiontoken": data.sessionToken} if data.sessionToken else {}),
        },
    )
    require(result.get("status") in ["OK", "ZERO_RESULTS"], "Place search unavailable", 503)
    return [
        {
            "id": p["place_id"],
            "primary": p["structured_formatting"]["main_text"],
            "secondary": p["structured_formatting"].get("secondary_text", ""),
        }
        for p in result.get("predictions", [])
    ]


def details(data):
    if data.placeId.startswith("local:"):
        place = PLACES.get(data.placeId[6:])
        require(place is not None, "Place not found", 404)
        return place
    require(bool(settings.google_maps_api_key), "Configure GOOGLE_MAPS_API_KEY", 503)
    result = google(
        "GET",
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={
            "key": settings.google_maps_api_key,
            "place_id": data.placeId,
            "fields": "formatted_address,geometry,address_components",
            **({"sessiontoken": data.sessionToken} if data.sessionToken else {}),
        },
    )
    require(result.get("status") == "OK", "Place not found", 404)
    p = result["result"]
    province = next(
        (
            a["short_name"]
            for a in p["address_components"]
            if "administrative_area_level_1" in a["types"]
        ),
        "",
    )
    return {"label": p["formatted_address"], "province": province, **p["geometry"]["location"]}


def route(data):
    if not settings.google_maps_api_key:
        return None
    p = google(
        "POST",
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        headers={
            "X-Goog-Api-Key": settings.google_maps_api_key,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
        },
        json={
            "origin": {
                "location": {"latLng": {"latitude": data.origin.lat, "longitude": data.origin.lng}}
            },
            "destination": {
                "location": {
                    "latLng": {"latitude": data.destination.lat, "longitude": data.destination.lng}
                }
            },
            "travelMode": "DRIVE",
        },
    )
    if not p.get("routes"):
        return None
    r = p["routes"][0]
    return {
        "distanceKm": round(r["distanceMeters"] / 1000, 1),
        "durationMin": round(float(r["duration"].rstrip("s")) / 60),
        "encodedPolyline": r["polyline"]["encodedPolyline"],
    }
