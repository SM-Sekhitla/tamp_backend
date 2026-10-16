import inspect
import json
import logging
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_party, is_admin
from app.api.v1.procedures import REGISTRY, dispatch
from app.core.config import settings
from app.core.errors import AppError, require
from app.db.session import get_session
from app.schemas.tracking import PositionInput
from app.services.records import needed
from app.services.storage import store

router = APIRouter()
logger = logging.getLogger("tamp.api")


@router.get("/healthz")
async def health():
    return {"status": "ok"}


@router.get("/api/v1/health")
async def ready(db: AsyncSession = Depends(get_session)):
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "postgresql"}


@router.post("/api/v1/tracking/trips/{trip_id}/positions", tags=["tracking"])
async def record_vehicle_position(
    trip_id: str,
    payload: PositionInput,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    from app.services.live_tracking import hub, save_position

    party = await db.run_sync(lambda sync_db: current_party(sync_db, request))
    position = await save_position(db, trip_id, party, payload)
    await db.commit()
    await hub.publish(trip_id, position)
    return position


@router.get("/api/v1/tracking/trips/{trip_id}/positions/latest", tags=["tracking"])
async def latest_vehicle_position(
    trip_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    from app.services.live_tracking import latest_position

    party = await db.run_sync(lambda sync_db: current_party(sync_db, request))
    return await latest_position(db, trip_id, party)


@router.get("/api/v1/tracking/trips/{trip_id}/positions", tags=["tracking"])
async def vehicle_position_history(
    trip_id: str,
    request: Request,
    limit: int = 100,
    db: AsyncSession = Depends(get_session),
):
    from app.services.live_tracking import position_history

    require(1 <= limit <= 500, "Limit must be between 1 and 500", 400)
    party = await db.run_sync(lambda sync_db: current_party(sync_db, request))
    return await position_history(db, trip_id, party, limit)


@router.websocket("/api/v1/tracking/trips/{trip_id}/live")
async def live_vehicle_position(websocket: WebSocket, trip_id: str):
    from app.db.session import AsyncSessionLocal
    from app.services.live_tracking import authorize_view, hub, latest_position, websocket_party

    try:
        origin = websocket.headers.get("origin")
        require(origin in settings.cors_origins, "WebSocket origin is not allowed", 403)
        async with AsyncSessionLocal() as db:
            party = await websocket_party(db, websocket)
            await authorize_view(db, trip_id, party)
            latest = await latest_position(db, trip_id, party)
        await websocket.accept()
        await hub.add(trip_id, websocket)
        if latest:
            await websocket.send_json({"type": "position", "position": latest})
        while True:
            # Clients send a small keepalive message; location updates flow from
            # authenticated HTTP ingestion to every connected viewer.
            await websocket.receive_text()
    except (AppError, WebSocketDisconnect):
        if websocket.client_state.name == "CONNECTING":
            await websocket.close(code=4401)
    finally:
        await hub.remove(trip_id, websocket)


@router.get("/api/v1/capabilities")
async def capabilities():
    return {
        "googleLogin": False,
        "maps": bool(settings.photon_url and settings.valhalla_url),
        "geocodingProvider": settings.geocoding_provider,
        "routingProvider": settings.routing_provider,
    }


async def admin_party(db: AsyncSession, request: Request):
    party = await db.run_sync(lambda sync_db: current_party(sync_db, request))
    require(is_admin(party), "Administrator access is required", 403)
    return party


@router.get("/api/v1/system-logs", tags=["admin"])
async def system_log_index(request: Request, db: AsyncSession = Depends(get_session)):
    from app.services.system_logs import browse

    await admin_party(db, request)
    return browse()


@router.get("/api/v1/system-logs/{group}/{filename}/content", tags=["admin"])
async def system_log_content(
    group: str,
    filename: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    from app.services.system_logs import resolve_log, tail

    await admin_party(db, request)
    return tail(resolve_log(group, filename))


@router.get("/api/v1/system-logs/{group}/{filename}/download", tags=["admin"])
async def download_system_log(
    group: str,
    filename: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    from app.services.system_logs import resolve_log

    await admin_party(db, request)
    path = resolve_log(group, filename)
    return FileResponse(path, media_type="text/plain", filename=filename)


def _execute_sync(sync_db, name, payload, method, request, response):
    procedure = REGISTRY.get(name)
    require(procedure is not None, "Procedure not found", 404)
    require(method == ("POST" if procedure.mutation else "GET"), "Method not allowed", 405)
    data = procedure.schema.model_validate(payload or {})
    party = current_party(sync_db, request, not procedure.public)
    module = name.split(".", 1)[0]
    module_logger = logging.getLogger(f"tamp.module.{module}")
    actor = party.id if party else "public"
    try:
        result = dispatch(name, data, sync_db, request, response, party)
        module_logger.info("OK procedure=%s method=%s actor=%s", name, method, actor)
        return result
    except Exception:
        module_logger.exception("FAILED procedure=%s method=%s actor=%s", name, method, actor)
        raise


async def execute(name, payload, method, db, request, response):
    result = await db.run_sync(
        lambda sync_db: _execute_sync(
            sync_db,
            name,
            payload,
            method,
            request,
            response,
        )
    )
    if inspect.isawaitable(result):
        result = await result
    await db.commit()
    return result


@router.api_route("/api/trpc/{paths}", methods=["GET", "POST"], include_in_schema=False)
async def trpc(
    paths: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
):
    names = paths.split(",")
    require(len(names) <= 20, "Batch limit exceeded", 400)
    batch = request.query_params.get("batch") == "1"
    try:
        raw = (
            await request.json()
            if request.method == "POST"
            else json.loads(request.query_params.get("input", "{}"))
        )
    except (ValueError, UnicodeDecodeError):
        raise AppError("Invalid JSON", 400)
    require(isinstance(raw, dict), "Expected a JSON object", 400)

    results = []
    statuses = []
    for index, name in enumerate(names):
        try:
            envelope = raw.get(str(index), {}) if batch else raw
            require(isinstance(envelope, dict), "Invalid input envelope", 400)
            payload = envelope.get("json")
            require(payload is None or isinstance(payload, dict), "Expected object input", 400)
            if payload is not None:
                payload = {key: value for key, value in payload.items() if value is not None}
            result = await execute(
                name,
                payload,
                request.method,
                db,
                request,
                response,
            )
            results.append({"result": {"data": {"json": result}}})
            statuses.append(200)
        except (AppError, ValidationError, IntegrityError) as exc:
            await db.rollback()
            status = (
                exc.status
                if isinstance(exc, AppError)
                else 400
                if isinstance(exc, ValidationError)
                else 409
            )
            message = (
                exc.message
                if isinstance(exc, AppError)
                else "Invalid request data"
                if isinstance(exc, ValidationError)
                else "Conflicting record or state"
            )
            codes = {
                400: (-32600, "BAD_REQUEST"),
                401: (-32001, "UNAUTHORIZED"),
                403: (-32003, "FORBIDDEN"),
                404: (-32004, "NOT_FOUND"),
                405: (-32005, "METHOD_NOT_SUPPORTED"),
                409: (-32009, "CONFLICT"),
                429: (-32029, "TOO_MANY_REQUESTS"),
                503: (-32003, "INTERNAL_SERVER_ERROR"),
            }
            numeric, code = codes.get(status, (-32603, "INTERNAL_SERVER_ERROR"))
            results.append(
                {
                    "error": {
                        "json": {
                            "message": message,
                            "code": numeric,
                            "data": {
                                "code": code,
                                "httpStatus": status,
                                "path": name,
                            },
                        }
                    }
                }
            )
            statuses.append(status)
        except Exception:
            await db.rollback()
            logger.exception("Procedure failed: %s", name)
            results.append(
                {
                    "error": {
                        "json": {
                            "message": "Internal server error",
                            "code": -32603,
                            "data": {
                                "code": "INTERNAL_SERVER_ERROR",
                                "httpStatus": 500,
                                "path": name,
                            },
                        }
                    }
                }
            )
            statuses.append(500)

    response.status_code = statuses[0] if len(set(statuses)) == 1 else 207
    return results if batch else results[0]


def make_endpoint(name, procedure):
    if procedure.mutation:

        async def endpoint(
            request: Request,
            response: Response,
            payload: procedure.schema,
            db: AsyncSession = Depends(get_session),
        ):
            return await execute(
                name,
                payload.model_dump(mode="json", by_alias=True, exclude_none=True),
                "POST",
                db,
                request,
                response,
            )

    else:

        async def endpoint(
            request: Request,
            response: Response,
            input: str = "{}",
            db: AsyncSession = Depends(get_session),
        ):
            try:
                payload = json.loads(input)
            except ValueError:
                raise AppError("Invalid JSON input", 400)
            return await execute(name, payload, "GET", db, request, response)

    endpoint.__name__ = name.replace(".", "_")
    return endpoint


for procedure_name, registered_procedure in REGISTRY.items():
    router.add_api_route(
        "/api/v1/" + procedure_name.replace(".", "/"),
        make_endpoint(procedure_name, registered_procedure),
        methods=["POST" if registered_procedure.mutation else "GET"],
        tags=[procedure_name.split(".")[0]],
    )


@router.post("/api/uploads")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
    db: AsyncSession = Depends(get_session),
):
    content = await file.read(settings.max_upload_bytes + 1)

    def store_upload(sync_db):
        party = current_party(sync_db, request)
        return store(sync_db, party, content, file.filename, kind)

    result = await db.run_sync(store_upload)
    await db.commit()
    return result


@router.get("/api/uploads/{ident}")
async def download(
    ident: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    def authorize_download(sync_db):
        party = current_party(sync_db, request)
        record = needed(sync_db, "uploads", ident)
        allowed = record.owner_id == party.id or is_admin(party)
        if not allowed and record.data["kind"] != "KYC":
            from app.services.domain import snapshot

            visible = snapshot(sync_db, party)
            url = record.data["url"]
            allowed = (
                any(item.get("avatarUrl") == url for item in visible["parties"])
                or any(url in truck.get("photos", []) for truck in visible["trucks"])
                or any(proof.get("photoUrl") == url for proof in visible["proofs"])
            )
        require(allowed, "Private file")
        return record.id, dict(record.data)

    record_id, data = await db.run_sync(authorize_download)
    path = Path(settings.upload_path) / record_id
    require(path.is_file(), "File not found", 404)
    return FileResponse(
        path,
        media_type=data["contentType"],
        filename=data["filename"],
        content_disposition_type="attachment" if data["kind"] == "KYC" else "inline",
    )
