import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.deps import current_party, is_admin
from app.api.v1.procedures import REGISTRY, dispatch
from app.core.config import settings
from app.core.errors import AppError, require
from app.db.session import get_session
from app.services.records import needed
from app.services.storage import store

router = APIRouter()
logger = logging.getLogger("tamp.api")


@router.get("/healthz")
def health():
    return {"status": "ok"}


@router.get("/api/v1/health")
def ready(db=Depends(get_session)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "postgresql"}


@router.get("/api/v1/capabilities")
def capabilities():
    return {"googleLogin": False, "maps": bool(settings.google_maps_api_key)}


def execute(name, payload, method, db, request, response):
    procedure = REGISTRY.get(name)
    require(procedure is not None, "Procedure not found", 404)
    require(method == ("POST" if procedure.mutation else "GET"), "Method not allowed", 405)
    data = procedure.schema.model_validate(payload or {})
    party = current_party(db, request, not procedure.public)
    result = dispatch(name, data, db, request, response, party)
    db.commit()
    return result


@router.api_route("/api/trpc/{paths}", methods=["GET", "POST"], include_in_schema=False)
async def trpc(paths: str, request: Request, response: Response, db=Depends(get_session)):
    # Wire adapter for the existing @trpc/client httpBatchLink + SuperJSON.
    from starlette.concurrency import run_in_threadpool

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
    for i, name in enumerate(names):
        try:
            envelope = raw.get(str(i), {}) if batch else raw
            require(isinstance(envelope, dict), "Invalid input envelope", 400)
            payload = envelope.get("json")
            require(payload is None or isinstance(payload, dict), "Expected object input", 400)
            # SuperJSON undefined values arrive as null; omit them at the boundary.
            if payload is not None:
                payload = {k: v for k, v in payload.items() if v is not None}
            result = await run_in_threadpool(
                execute, name, payload, request.method, db, request, response
            )
            results.append({"result": {"data": {"json": result}}})
            statuses.append(200)
        except (AppError, ValidationError, IntegrityError) as exc:
            db.rollback()
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
                            "data": {"code": code, "httpStatus": status, "path": name},
                        }
                    }
                }
            )
            statuses.append(status)
        except Exception:
            db.rollback()
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


# Direct versioned routes are documented individually in OpenAPI; same controllers.
def make_endpoint(name, procedure):
    if procedure.mutation:

        def endpoint(
            request: Request, response: Response, payload: procedure.schema, db=Depends(get_session)
        ):
            return execute(
                name,
                payload.model_dump(mode="json", by_alias=True, exclude_none=True),
                "POST",
                db,
                request,
                response,
            )
    else:

        def endpoint(
            request: Request, response: Response, input: str = "{}", db=Depends(get_session)
        ):
            try:
                payload = json.loads(input)
            except ValueError:
                raise AppError("Invalid JSON input", 400)
            return execute(name, payload, "GET", db, request, response)

    endpoint.__name__ = name.replace(".", "_")
    return endpoint


for name, procedure in REGISTRY.items():
    router.add_api_route(
        "/api/v1/" + name.replace(".", "/"),
        make_endpoint(name, procedure),
        methods=["POST" if procedure.mutation else "GET"],
        tags=[name.split(".")[0]],
    )


@router.post("/api/uploads")
async def upload(
    request: Request, file: UploadFile = File(...), kind: str = Form(...), db=Depends(get_session)
):
    party = current_party(db, request)
    content = await file.read(settings.max_upload_bytes + 1)
    from starlette.concurrency import run_in_threadpool

    result = await run_in_threadpool(store, db, party, content, file.filename, kind)
    db.commit()
    return result


@router.get("/api/uploads/{ident}")
def download(ident: str, request: Request, db=Depends(get_session)):
    party = current_party(db, request)
    record = needed(db, "uploads", ident)
    allowed = record.owner_id == party.id or is_admin(party)
    if not allowed and record.data["kind"] != "KYC":
        from app.services.domain import snapshot

        visible = snapshot(db, party)
        url = record.data["url"]
        allowed = (
            any(p.get("avatarUrl") == url for p in visible["parties"])
            or any(url in t.get("photos", []) for t in visible["trucks"])
            or any(p.get("photoUrl") == url for p in visible["proofs"])
        )
    require(allowed, "Private file")
    path = Path(settings.upload_path) / record.id
    require(path.is_file(), "File not found", 404)
    return FileResponse(
        path,
        media_type=record.data["contentType"],
        filename=record.data["filename"],
        content_disposition_type="attachment" if record.data["kind"] == "KYC" else "inline",
    )
