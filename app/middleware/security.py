from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(self), payment=()"
        )
        response.headers["Cache-Control"] = "no-store"
        if settings.environment == "production":
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )
        return response
