from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.security import now, token_hash
from app.db.models import RateBucket
from app.db.session import SessionLocal


def consume(ip, path):
    auth = any(
        s in path.lower() for s in ["login", "sendcode", "password", "register", "emailchange"]
    )
    cost = min(20, path.count(",") + 1)
    limit = settings.auth_rate_limit if auth else settings.rate_limit
    window = int(now().timestamp()) // 60
    key = token_hash(f"{ip}:{'auth' if auth else 'api'}:{window}")
    with SessionLocal.begin() as db:
        db.execute(delete(RateBucket).where(RateBucket.expires < now()))
        stmt = insert(RateBucket).values(key=key, count=cost, expires=now() + timedelta(minutes=2))
        count = db.scalar(
            stmt.on_conflict_do_update(
                index_elements=[RateBucket.key], set_={"count": RateBucket.count + cost}
            ).returning(RateBucket.count)
        )
        return count <= limit


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/api/") and request.method != "OPTIONS":
            if not await run_in_threadpool(consume, client_ip(request), request.url.path):
                return JSONResponse(
                    {"error": "Too many requests"}, status_code=429, headers={"Retry-After": "60"}
                )
        return await call_next(request)
