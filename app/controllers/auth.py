import secrets
from datetime import timedelta

from sqlalchemy import delete, select

from app.api.deps import COOKIE, party_payload
from app.core.config import settings
from app.core.errors import require
from app.core.security import iso, new_id, now, passwords, token_hash, verify_password
from app.db.models import Party, Session
from app.services.email import consume_code, issue_code


def find_email(db, email):
    return db.scalar(select(Party).where(Party.email == str(email).lower()))


def create_party(db, email, password, role, name, company, **extra):
    ident = new_id("P")
    data = dict(
        id=ident,
        role=role,
        email=str(email).lower(),
        contactName=name,
        companyName=company,
        phone="",
        province="",
        verification="PENDING",
        ratingAvg=None,
        ratingCount=0,
        suspended=False,
        createdAt=iso(),
        onboardingComplete=True,
        **extra,
    )
    party = Party(
        id=ident,
        email=data["email"],
        password_hash=passwords.hash(password),
        data=data,
        onboarding={},
    )
    db.add(party)
    db.flush()
    return party


def set_session(db, party, response):
    token = secrets.token_urlsafe(48)
    db.add(
        Session(
            token=token_hash(token),
            party_id=party.id,
            expires=now() + timedelta(hours=settings.session_hours),
        )
    )
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_hours * 3600,
    )


def revoke(db, party, keep=None):
    stmt = delete(Session).where(Session.party_id == party.id)
    if keep:
        stmt = stmt.where(Session.token != token_hash(keep))
    return db.execute(stmt).rowcount


def handle(action, data, db, request, response, party):
    d = data.model_dump(exclude_none=True)
    if action == "me":
        return party_payload(party) if party else None
    if action == "login":
        user = find_email(db, d["email"])
        valid = verify_password(d["password"], user.password_hash if user else None)
        require(
            user is not None and valid and not user.data["suspended"],
            "Invalid email or password",
            401,
        )
        old = request.cookies.get(COOKIE)
        if old:
            db.execute(delete(Session).where(Session.token == token_hash(old)))
        set_session(db, user, response)
        return party_payload(user)
    if action == "logout":
        db.execute(
            delete(Session).where(Session.token == token_hash(request.cookies.get(COOKIE, "")))
        )
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}
    if action == "requestPasswordReset":
        email = str(d["email"]).lower()
        user = find_email(db, email)
        result = issue_code(db, "reset:" + email, email) if user else {}
        return {"sent": True, **({"devCode": result["devCode"]} if "devCode" in result else {})}
    if action == "resetPassword":
        email = str(d["email"]).lower()
        consume_code(db, "reset:" + email, d["code"])
        user = find_email(db, email)
        require(user is not None, "Invalid or expired verification code", 400)
        user.password_hash = passwords.hash(d["newPassword"])
        revoke(db, user)
        return {"ok": True}
    if action == "changePassword":
        require(
            verify_password(d["currentPassword"], party.password_hash),
            "Current password is incorrect",
            400,
        )
        party.password_hash = passwords.hash(d["newPassword"])
        count = revoke(db, party)
        set_session(db, party, response)
        return {"ok": True, "signedOut": max(0, count - 1)}
    if action == "logoutOtherSessions":
        return {"count": revoke(db, party, request.cookies.get(COOKIE))}
    if action == "requestEmailChange":
        email = str(d["newEmail"]).lower()
        require(find_email(db, email) is None, "Email unavailable", 409)
        return {**issue_code(db, "change:" + party.id, email), "newEmail": email}
    if action == "confirmEmailChange":
        email = consume_code(db, "change:" + party.id, d["code"])
        require(find_email(db, email) is None, "Email unavailable", 409)
        party.email = email
        party.data = {**party.data, "email": email}
        revoke(db, party)
        set_session(db, party, response)
        return party_payload(party)
    if action == "updateProfile":
        if d.get("avatarUrl"):
            from app.services.storage import own_upload

            own_upload(db, party, d["avatarUrl"], "AVATAR")
        party.data = {**party.data, **d}
        return party_payload(party)
    raise AssertionError(action)
