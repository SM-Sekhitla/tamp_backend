from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            # Custom header forces a CORS preflight. Simple cross-site forms
            # cannot submit authenticated mutations, including login CSRF.
            if (
                request.headers.get("x-tamp-request") != "1"
                or (origin and origin not in settings.cors_origins)
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return JSONResponse(
                    {"error": "Invalid request origin or missing X-TAMP-Request header"},
                    status_code=403,
                )
        return await call_next(request)
