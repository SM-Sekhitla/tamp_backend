from app.core.errors import require
from app.core.security import now, token_hash
from app.db.models import Party, Session

COOKIE = "tamp_session"


def current_party(db, request, required=True):
    raw = request.cookies.get(COOKIE, "")
    session = db.get(Session, token_hash(raw)) if raw else None
    party = db.get(Party, session.party_id) if session and session.expires > now() else None
    if party and party.data.get("suspended"):
        party = None
    if required:
        require(party is not None, "Sign in to continue", 401)
    return party


def is_admin(party):
    return party.data["role"] in {"ADMIN", "system_super_user"}


def party_payload(party):
    data = dict(party.data)
    if data.get("role") == "system_super_user":
        data["role"] = "ADMIN"
        data["systemRole"] = "system_super_user"
    return data


def role(party, *allowed):
    actual = party.data["role"]
    require(
        actual in allowed or (actual == "system_super_user" and "ADMIN" in allowed),
        "This role cannot perform that action",
    )
