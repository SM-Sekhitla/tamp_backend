from app.core.errors import require
from datetime import timedelta

from app.core.config import settings
from app.core.security import now, token_hash
from app.db.models import Session, User

COOKIE = "tamp_session"


def current_party(db, request, required=True):
    raw = request.cookies.get(COOKIE, "")
    session = db.get(Session, token_hash(raw)) if raw else None
    current = now()
    idle_limit = current - timedelta(minutes=settings.session_idle_minutes)
    party = (
        db.get(User, session.user_id)
        if session and session.expires > current and session.last_seen_at > idle_limit
        else None
    )
    if party:
        db.info["active_organisation_id"] = session.active_organisation_id
        # Avoid a write on every request while still keeping an accurate idle timeout.
        if session.last_seen_at < current - timedelta(minutes=5):
            session.last_seen_at = current
    if party and party.data.get("suspended"):
        party = None
    if required:
        require(party is not None, "Sign in to continue", 401)
    return party


def is_admin(party):
    return party.data["role"] in {"ADMIN", "system_super_user"}


def party_payload(party, db=None):
    data = dict(party.data)
    if data.get("role") == "system_super_user":
        data["role"] = "ADMIN"
        data["systemRole"] = "system_super_user"
    if db is not None:
        from app.services.rbac import organisation_payloads, permission_codes

        active = db.info.get("active_organisation_id")
        data["activeOrganisationId"] = active
        data["permissions"] = sorted(permission_codes(db, party, active))
        data["organisations"] = organisation_payloads(db, party)
    return data


def role(party, *allowed):
    actual = party.data["role"]
    require(
        actual in allowed or (actual == "system_super_user" and "ADMIN" in allowed),
        "This role cannot perform that action",
    )
