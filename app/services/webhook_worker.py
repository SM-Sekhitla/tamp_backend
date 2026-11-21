"""Run separately: python -m app.services.webhook_worker (at-least-once delivery)."""

import asyncio
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select

from app.controllers.webhooks import cipher
from app.core.config import settings
from app.core.logging import configure_file_logging
from app.core.security import now
from app.db.models import MODELS
from app.db.session import AsyncSessionLocal
from app.services.records import get

logger = logging.getLogger("tamp.webhook_worker")


def _ms(start):
    return round((time.perf_counter() - start) * 1000, 1)


def _log(level, event, **fields):
    """Emit machine-readable fields without secrets or payload contents."""
    logger.log(
        level,
        json.dumps(
            {"event": event, **{key: value for key, value in fields.items() if value is not None}},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _queue_wait_ms(created_at):
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return round(max(0, (now() - created).total_seconds() * 1000), 1)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class DeliveryResult:
    status_code: int
    validation_ms: float
    dns_ms: float
    http_ms: float
    response_bytes: int | None


class WebhookDeliveryError(Exception):
    def __init__(self, stage, cause):
        super().__init__(f"Webhook delivery failed during {stage}")
        self.stage = stage
        self.error_type = type(cause).__name__


def deliver(url, body, secret, ident):
    validation_started = time.perf_counter()
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in settings.webhook_allowed_hosts:
            raise ValueError("Destination not allowed")
    except Exception as exc:
        raise WebhookDeliveryError("validation", exc) from exc
    validation_ms = _ms(validation_started)

    dns_started = time.perf_counter()
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        ips = [address[4][0] for address in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError("Destination must resolve only to public addresses")
    except Exception as exc:
        raise WebhookDeliveryError("dns", exc) from exc
    dns_ms = _ms(dns_started)

    conn = http.client.HTTPSConnection(parsed.hostname, timeout=10)
    # Pin the validated IP while retaining the hostname for TLS verification.
    conn._create_connection = lambda address, timeout, source_address=None: (
        socket.create_connection((ips[0], 443), timeout, source_address)
    )
    http_started = time.perf_counter()
    try:
        conn.request(
            "POST",
            parsed.path + ("?" + parsed.query if parsed.query else ""),
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-TAMP-Delivery": ident,
                "X-TAMP-Signature": "sha256="
                + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            },
        )
        response = conn.getresponse()
        content_length = response.getheader("Content-Length")
        return DeliveryResult(
            status_code=response.status,
            validation_ms=validation_ms,
            dns_ms=dns_ms,
            http_ms=_ms(http_started),
            response_bytes=int(content_length) if content_length and content_length.isdigit() else None,
        )
    except Exception as exc:
        raise WebhookDeliveryError("http", exc) from exc
    finally:
        conn.close()


async def tick():
    tick_started = time.perf_counter()
    metrics = {
        "delivery_id": None,
        "endpoint_id": None,
        "event_type": None,
        "attempt": None,
        "queue_wait_ms": None,
        "claim_db_ms": None,
        "endpoint_lookup_db_ms": None,
        "payload_bytes": None,
        "validation_ms": None,
        "dns_ms": None,
        "http_ms": None,
        "outbound_total_ms": None,
        "response_bytes": None,
        "response_code": None,
        "retry_delay_ms": None,
        "error_stage": None,
        "error_type": None,
    }
    outcome = None
    persist_started = None
    async with AsyncSessionLocal.begin() as db:
        claim_started = time.perf_counter()
        model = MODELS["deliveries"]
        job = await db.scalar(
            select(model)
            .where(
                model.data["status"].as_string() == "PENDING",
                model.data["nextAttempt"].as_string() <= now().isoformat(),
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return False
        metrics["claim_db_ms"] = _ms(claim_started)
        endpoint_lookup_started = time.perf_counter()
        endpoint = await db.run_sync(
            lambda sync_db: get(sync_db, "webhooks", job.data["endpointId"])
        )
        metrics["endpoint_lookup_db_ms"] = _ms(endpoint_lookup_started)
        data = dict(job.data)
        data["attempts"] += 1
        body_started = time.perf_counter()
        body = json.dumps(data["payload"], sort_keys=True, separators=(",", ":")).encode()
        serialization_ms = _ms(body_started)
        metrics.update(
            delivery_id=job.id,
            endpoint_id=data["endpointId"],
            event_type=data.get("event"),
            attempt=data["attempts"],
            queue_wait_ms=_queue_wait_ms(data.get("createdAt")),
            payload_bytes=len(body),
        )
        _log(logging.INFO, "webhook_delivery_started", **metrics, serialization_ms=serialization_ms)
        try:
            if not endpoint or not endpoint.data["active"]:
                raise WebhookDeliveryError("endpoint_lookup", ValueError("Endpoint disabled or removed"))
            decrypt_started = time.perf_counter()
            secret = cipher().decrypt(endpoint.data["encryptedSecret"].encode()).decode()
            decrypt_ms = _ms(decrypt_started)
            outbound_started = time.perf_counter()
            result = await asyncio.to_thread(
                deliver,
                endpoint.data["url"],
                body,
                secret,
                job.id,
            )
            outbound_ms = _ms(outbound_started)
            metrics["outbound_total_ms"] = outbound_ms
            # Preserve compatibility with tests or extensions that return the
            # legacy integer status code.
            if isinstance(result, DeliveryResult):
                code = result.status_code
                metrics.update(
                    validation_ms=result.validation_ms,
                    dns_ms=result.dns_ms,
                    http_ms=result.http_ms,
                    response_bytes=result.response_bytes,
                )
            else:
                code = int(result)
                metrics["http_ms"] = outbound_ms
            metrics["response_code"] = code
            data["responseCode"] = code
            if not 200 <= code < 300:
                raise WebhookDeliveryError("http_response", ValueError(f"HTTP {code}"))
            data["status"] = "DELIVERED"
            data["lastError"] = None
            metrics["decrypt_ms"] = decrypt_ms
            outcome = "delivered"
        except Exception as exc:
            metrics["error_stage"] = exc.stage if isinstance(exc, WebhookDeliveryError) else "worker"
            metrics["error_type"] = exc.error_type if isinstance(exc, WebhookDeliveryError) else type(exc).__name__
            data["lastError"] = metrics["error_type"]
            terminal = data["attempts"] >= 5
            data["status"] = "FAILED" if terminal else "PENDING"
            retry_seconds = 30 * 2 ** data["attempts"]
            data["nextAttempt"] = (
                now() + timedelta(seconds=retry_seconds)
            ).isoformat()
            metrics["retry_delay_ms"] = None if terminal else retry_seconds * 1000
            outcome = "failed" if terminal else "retrying"
        persist_started = time.perf_counter()
        job.data = data

    persist_ms = _ms(persist_started) if persist_started is not None else None
    total_ms = _ms(tick_started)
    slow = total_ms >= settings.webhook_slow_total_ms or (
        metrics["http_ms"] is not None and metrics["http_ms"] >= settings.webhook_slow_http_ms
    )
    level = logging.ERROR if outcome == "failed" else logging.WARNING if outcome == "retrying" or slow else logging.INFO
    _log(
        level,
        "webhook_delivery_completed",
        **metrics,
        outcome=outcome,
        persist_db_ms=persist_ms,
        total_ms=total_ms,
        slow=slow,
    )
    return True


async def main():
    _log(
        logging.INFO,
        "webhook_worker_started",
        slow_http_ms=settings.webhook_slow_http_ms,
        slow_total_ms=settings.webhook_slow_total_ms,
    )
    while True:
        loop_started = time.perf_counter()
        try:
            if not await tick():
                await asyncio.sleep(2)
        except Exception as exc:
            _log(
                logging.ERROR,
                "webhook_worker_error",
                error_type=type(exc).__name__,
                loop_ms=_ms(loop_started),
            )
            logger.exception("Webhook worker iteration failed")
            await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    configure_file_logging()
    asyncio.run(main())
