import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("tamp.requests")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        ident = str(uuid.uuid4())
        request.state.request_id = ident
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = ident
        logger.info(
            "request_id=%s method=%s path=%s status=%s ms=%.1f",
            ident,
            request.method,
            request.url.path,
            response.status_code,
            (time.monotonic() - start) * 1000,
        )
        return response
