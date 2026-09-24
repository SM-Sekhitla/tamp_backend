"""Run separately: python -m app.services.webhook_worker (at-least-once delivery)."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import socket
import time
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select

from app.controllers.webhooks import cipher
from app.core.config import settings
from app.core.security import now
from app.db.models import MODELS
from app.db.session import SessionLocal
from app.services.records import get


def deliver(url, body, secret, ident):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in settings.webhook_allowed_hosts:
        raise ValueError("Destination not allowed")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    ips = [a[4][0] for a in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Destination must resolve only to public addresses")
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=10)
    # Pin the validated IP while retaining the hostname for TLS verification.
    conn._create_connection = lambda address, timeout, source_address=None: (
        socket.create_connection((ips[0], 443), timeout, source_address)
    )
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
        return response.status
    finally:
        conn.close()


def tick():
    with SessionLocal.begin() as db:
        model = MODELS["deliveries"]
        job = db.scalar(
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
        endpoint = get(db, "webhooks", job.data["endpointId"])
        d = dict(job.data)
        d["attempts"] += 1
        try:
            if not endpoint or not endpoint.data["active"]:
                raise ValueError("Endpoint disabled or removed")
            secret = cipher().decrypt(endpoint.data["encryptedSecret"].encode()).decode()
            code = deliver(
                endpoint.data["url"],
                json.dumps(d["payload"], sort_keys=True).encode(),
                secret,
                job.id,
            )
            d["responseCode"] = code
            if not 200 <= code < 300:
                raise ValueError(f"HTTP {code}")
            d["status"] = "DELIVERED"
            d["lastError"] = None
        except Exception as exc:
            d["lastError"] = type(exc).__name__
            d["status"] = "FAILED" if d["attempts"] >= 5 else "PENDING"
            d["nextAttempt"] = (now() + timedelta(seconds=30 * 2 ** d["attempts"])).isoformat()
        job.data = d
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            if not tick():
                time.sleep(2)
        except Exception:
            logging.exception("Webhook worker failed")
            time.sleep(5)
