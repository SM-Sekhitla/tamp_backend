import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from pwdlib import PasswordHash

from app.core.config import settings

passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))


def now():
    return datetime.now(timezone.utc)


def iso():
    return now().isoformat()


def token_hash(value):
    return hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def new_id(prefix):
    return prefix + "-" + secrets.token_hex(12)


def verify_password(password, hashed):
    try:
        return passwords.verify(password, hashed or DUMMY_HASH)
    except (ValueError, TypeError):
        return False
