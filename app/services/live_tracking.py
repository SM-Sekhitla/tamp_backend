import asyncio
import math
from collections import defaultdict
from datetime import timedelta

from fastapi import WebSocket
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import is_admin
from app.core.config import settings
from app.core.errors import AppError, require
from app.core.security import new_id, now, token_hash
from app.db.models import MODELS, Party, Session, VehiclePosition


class PositionHub:
    """Process-local fan-out. WEB_CONCURRENCY=1 keeps this correct for the monolith."""

    def __init__(self):
        self._clients: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def add(self, trip_id: str, websocket: WebSocket):
        async with self._lock:
            self._clients[trip_id].add(websocket)

    async def remove(self, trip_id: str, websocket: WebSocket):
        async with self._lock:
            clients = self._clients.get(trip_id)
            if clients:
                clients.discard(websocket)
                if not clients:
                    self._clients.pop(trip_id, None)

    async def publish(self, trip_id: str, payload: dict):
        async with self._lock:
            clients = tuple(self._clients.get(trip_id, ()))
        failed = []
        for client in clients:
            try:
                await client.send_json({"type": "position", "position": payload})
            except Exception:
                failed.append(client)
        for client in failed:
            await self.remove(trip_id, client)


hub = PositionHub()


def distance_metres(a: dict, lat: float, lng: float):
    lat1, lat2 = math.radians(a["lat"]), math.radians(lat)
    dlat = lat2 - lat1
    dlng = math.radians(lng - a["lng"])
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(value)))


async def add_notification(db: AsyncSession, owner_id: str, load_id: str, title: str, detail: str):
    model = MODELS["notifications"]
    body = {
        "id": new_id("NT"),
        "title": title,
        "detail": detail,
        "at": now().isoformat(),
        "unread": True,
        "tone": "info",
        "subjectType": "LOAD",
        "subjectId": load_id,
    }
    db.add(model(id=body["id"], owner_id=owner_id, data=body))


async def update_geofences(db: AsyncSession, trip_row, truck: dict, load: dict, lat: float, lng: float, at):
    trip = dict(trip_row.data)
    states = dict(trip.get("geofences") or {})
    events = list(trip.get("geofenceEvents") or [])
    transition = None
    for zone, place in (("PICKUP", load["origin"]), ("DESTINATION", load["destination"])):
        distance_m = round(distance_metres(place, lat, lng))
        state = dict(states.get(zone) or {"inside": False})
        entered = not state.get("inside") and distance_m <= settings.geofence_radius_m
        exited = state.get("inside") and distance_m >= settings.geofence_exit_radius_m
        if not entered and not exited:
            state["distanceM"] = distance_m
            states[zone] = state
            continue
        event_type = "ENTERED" if entered else "EXITED"
        event = {
            "id": new_id("GF"),
            "zone": zone,
            "type": event_type,
            "at": at.isoformat(),
            "lat": lat,
            "lng": lng,
            "distanceM": distance_m,
        }
        events.append(event)
        state.update(
            inside=entered,
            distanceM=distance_m,
            **({"enteredAt": event["at"]} if entered else {"exitedAt": event["at"]}),
        )
        states[zone] = state
        transition = event
        label = "pickup" if zone == "PICKUP" else "destination"
        action = "entered" if entered else "left"
        title = f"Vehicle {action} {label} geofence"
        detail = f"Trip {trip['id']} {action} the {label} zone."
        for recipient in {load["ownerId"], truck["transporterId"]}:
            await add_notification(db, recipient, load["id"], title, detail)

    suggestion = trip.get("suggestedAction")
    if states.get("PICKUP", {}).get("inside") and trip["status"] == "EN_ROUTE_PICKUP":
        suggestion = "AT_PICKUP"
    elif states.get("DESTINATION", {}).get("inside") and trip["status"] == "IN_TRANSIT":
        suggestion = "AT_DROPOFF"
    trip_row.data = {
        **trip,
        "geofences": states,
        "geofenceEvents": events[-100:],
        "suggestedAction": suggestion,
    }
    return transition


async def websocket_party(db: AsyncSession, websocket: WebSocket):
    raw = websocket.cookies.get("tamp_session", "")
    session = await db.get(Session, token_hash(raw)) if raw else None
    party = await db.get(Party, session.party_id) if session and session.expires > now() else None
    if not party or party.data.get("suspended"):
        raise AppError("Sign in to continue", 401)
    return party


async def trip_graph(db: AsyncSession, trip_id: str):
    trip_row = await db.get(MODELS["trips"], trip_id)
    require(trip_row is not None, "Trip not found", 404)
    match_row = await db.get(MODELS["matches"], trip_row.data["matchId"])
    require(match_row is not None, "Trip match not found", 404)
    truck_row = await db.get(MODELS["trucks"], match_row.data["truckPostingId"])
    load_row = await db.get(MODELS["loads"], match_row.data["loadId"])
    require(truck_row is not None and load_row is not None, "Trip data not found", 404)
    return trip_row, match_row, truck_row, load_row


def can_view(party, truck: dict, load: dict):
    return is_admin(party) or party.id in {
        load["ownerId"],
        truck["transporterId"],
        truck.get("driverId"),
    }


async def authorize_view(db: AsyncSession, trip_id: str, party):
    graph = await trip_graph(db, trip_id)
    require(can_view(party, graph[2].data, graph[3].data), "Trip is not visible to this account", 403)
    return graph


async def save_position(db: AsyncSession, trip_id: str, party, data):
    trip_row, _match_row, truck_row, _load_row = await authorize_view(db, trip_id, party)
    truck = truck_row.data
    assigned_driver = truck.get("driverId")
    permitted = party.id == assigned_driver if assigned_driver else party.id == truck["transporterId"]
    require(permitted, "Only the assigned driver may share this vehicle location", 403)
    require(trip_row.data["status"] not in ["DELIVERED", "COMPLETED", "CANCELLED"], "Trip is no longer active", 409)
    recorded = data.recordedAt
    require(recorded.tzinfo is not None, "recordedAt must include a timezone", 400)
    current = now()
    require(current - timedelta(minutes=10) <= recorded <= current + timedelta(minutes=2), "Location timestamp is outside the accepted window", 400)

    previous = await db.scalar(
        select(VehiclePosition)
        .where(VehiclePosition.trip_id == trip_id)
        .order_by(VehiclePosition.recorded_at.desc(), VehiclePosition.id.desc())
        .limit(1)
    )
    if previous:
        require(recorded > previous.recorded_at, "Location update is out of order", 409)
        seconds = (recorded - previous.recorded_at).total_seconds()
        lat1, lat2 = math.radians(previous.lat), math.radians(data.lat)
        dlat = lat2 - lat1
        dlng = math.radians(data.lng - previous.lng)
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
        distance_km = 6371 * 2 * math.asin(min(1, math.sqrt(haversine)))
        implied_kph = distance_km / (seconds / 3600)
        # Accuracy drift can look fast over very short intervals. Permit that
        # radius before rejecting physically implausible truck movement.
        drift_km = ((previous.accuracy_m or 0) + (data.accuracyM or 0)) / 1000
        require(distance_km <= drift_km or implied_kph <= 180, "Location jump is not physically plausible", 409)

    position = VehiclePosition(
        trip_id=trip_id,
        truck_id=truck["id"],
        reported_by=party.id,
        recorded_at=recorded,
        received_at=current,
        lat=data.lat,
        lng=data.lng,
        accuracy_m=data.accuracyM,
        speed_kph=data.speedKph,
        heading=data.heading,
    )
    db.add(position)
    await db.execute(
        delete(VehiclePosition).where(
            VehiclePosition.received_at < current - timedelta(days=settings.tracking_retention_days)
        )
    )
    payload = {
        "tripId": trip_id,
        "truckId": truck["id"],
        "lat": data.lat,
        "lng": data.lng,
        "accuracyM": data.accuracyM,
        "speedKph": data.speedKph,
        "heading": data.heading,
        "recordedAt": recorded.isoformat(),
        "receivedAt": current.isoformat(),
    }
    # Keep snapshots and public tracking links current while the normalized
    # history table remains the source of truth for location history.
    trip_row.data = {**trip_row.data, "simulatedPosition": {"lat": data.lat, "lng": data.lng, "updatedAt": recorded.isoformat()}}
    geofence_event = await update_geofences(
        db, trip_row, truck, _load_row.data, data.lat, data.lng, recorded
    )
    if geofence_event:
        payload["geofenceEvent"] = geofence_event
    await db.flush()
    return payload


def position_payload(row: VehiclePosition):
    return {
        "tripId": row.trip_id,
        "truckId": row.truck_id,
        "lat": row.lat,
        "lng": row.lng,
        "accuracyM": row.accuracy_m,
        "speedKph": row.speed_kph,
        "heading": row.heading,
        "recordedAt": row.recorded_at.isoformat(),
        "receivedAt": row.received_at.isoformat(),
    }


async def latest_position(db: AsyncSession, trip_id: str, party):
    await authorize_view(db, trip_id, party)
    row = await db.scalar(
        select(VehiclePosition)
        .where(VehiclePosition.trip_id == trip_id)
        .order_by(VehiclePosition.recorded_at.desc(), VehiclePosition.id.desc())
        .limit(1)
    )
    return position_payload(row) if row else None


async def position_history(db: AsyncSession, trip_id: str, party, limit: int):
    await authorize_view(db, trip_id, party)
    rows = (
        await db.scalars(
            select(VehiclePosition)
            .where(VehiclePosition.trip_id == trip_id)
            .order_by(VehiclePosition.recorded_at.desc(), VehiclePosition.id.desc())
            .limit(limit)
        )
    ).all()
    return [position_payload(row) for row in reversed(rows)]

