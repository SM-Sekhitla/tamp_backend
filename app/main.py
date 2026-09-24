import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import router
from app.core.config import settings
from app.core.errors import AppError
from app.middleware.body_limit import BodyLimitMiddleware
from app.middleware.csrf import CsrfMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security import SecurityHeadersMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.initial_users import ensure_platform_super_user

    ensure_platform_super_user()
    yield


def create_app():
    logging.basicConfig(level=logging.INFO)
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        docs_url="/docs" if settings.environment != "production" else None,
        lifespan=lifespan,
    )
    app.include_router(router)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-TAMP-Request"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.exception_handler(AppError)
    async def error(request: Request, exc: AppError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(IntegrityError)
    async def conflict(request: Request, exc):
        return JSONResponse({"error": "Conflicting record or state"}, status_code=409)

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc):
        return JSONResponse({"error": "Invalid request data"}, status_code=400)

    @app.exception_handler(Exception)
    async def internal(request: Request, exc):
        logging.getLogger("tamp").error("Unhandled request failure", exc_info=exc)
        return JSONResponse({"error": "Internal server error"}, status_code=500)

    return app


app = create_app()
