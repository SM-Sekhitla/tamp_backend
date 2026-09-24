import base64
import hashlib
import secrets
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from app.api.deps import role
from app.core.config import settings
from app.core.errors import require
from app.core.security import iso, new_id
from app.services.records import needed, put, rows


def cipher():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def handle(action, data, db, request, response, party):
    role(party, "ADMIN")
    if action == "list":
        return [
            {k: v for k, v in r.data.items() if k != "encryptedSecret"}
            for r in rows(db, "webhooks")
            if r.owner_id == party.id
        ]
    if action == "deliveries":
        return [
            {k: v for k, v in r.data.items() if k not in ["payload", "nextAttempt"]}
            for r in rows(db, "deliveries")
            if r.owner_id == party.id
        ][-data.limit :][::-1]
    if action == "create":
        url = urlsplit(data.url)
        require(
            url.scheme == "https"
            and url.hostname in settings.webhook_allowed_hosts
            and not url.username
            and not url.password
            and url.port in [None, 443],
            "Webhook requires HTTPS and an administrator-configured allowed host",
            400,
        )
        secret = secrets.token_urlsafe(32)
        ident = new_id("WH")
        put(
            db,
            "webhooks",
            dict(
                id=ident,
                url=data.url,
                events=data.events,
                active=True,
                secretHint=secret[-4:],
                encryptedSecret=cipher().encrypt(secret.encode()).decode(),
                createdAt=iso(),
            ),
            party.id,
        )
        return {"id": ident, "url": data.url, "events": data.events, "secret": secret}
    row = needed(db, "webhooks", data.id, True)
    require(row.owner_id == party.id)
    if action == "setActive":
        row.data = {**row.data, "active": data.active}
    elif action == "delete":
        db.delete(row)
    return {"ok": True}
