import secrets
from datetime import timedelta

from sqlalchemy import delete, select

from app.api.deps import COOKIE, party_payload
from app.core.config import settings
from app.core.errors import require
from app.core.security import (
    iso,
    new_id,
    now,
    passwords,
    token_hash,
    validate_new_password,
    verify_password,
)
from app.db.models import RateBucket, Session, User
from app.services.email import consume_code, issue_code


def find_email(db, email):
    return db.scalar(select(User).where(User.email == str(email).lower()))


def create_party(db, email, password, role, name, company, **extra):
    require(
        validate_new_password(password),
        "Use at least 12 characters with upper-case, lower-case and a number",
        400,
    )
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
    party = User(
        id=ident,
        email=data["email"],
        password_hash=passwords.hash(password),
        data=data,
        onboarding={},
    )
    db.add(party)
    db.flush()
    from app.services.rbac import provision_user_access

    provision_user_access(db, party, company)
    return party


def set_session(db, party, response, request):
    token = secrets.token_urlsafe(48)
    from app.services.rbac import primary_organisation_id, provision_user_access

    # Repair legacy accounts that pre-date organisation-scoped RBAC. Truck
    # owner administrators must retain their role assignment in their primary
    # organisation when a new session is created.
    provision_user_access(db, party, party.data.get("companyName"))

    active_organisation_id = primary_organisation_id(db, party)
    db.info["active_organisation_id"] = active_organisation_id
    current = now()
    db.add(
        Session(
            token=token_hash(token),
            user_id=party.id,
            active_organisation_id=active_organisation_id,
            expires=current + timedelta(hours=settings.session_hours),
            created_at=current,
            last_seen_at=current,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "")[:300] or None,
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
    stmt = delete(Session).where(Session.user_id == party.id)
    if keep:
        stmt = stmt.where(Session.token != token_hash(keep))
    return db.execute(stmt).rowcount


def _login_bucket(db, email):
    key = token_hash("login-account:" + str(email).strip().lower())
    bucket = db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
    current = now()
    if bucket and bucket.expires <= current:
        db.delete(bucket)
        db.flush()
        bucket = None
    return key, bucket


def _record_failed_login(db, email):
    key, bucket = _login_bucket(db, email)
    if bucket is None:
        bucket = RateBucket(
            key=key,
            count=1,
            expires=now() + timedelta(minutes=settings.auth_account_window_minutes),
        )
        db.add(bucket)
    else:
        bucket.count += 1
    db.commit()  # A rejected request must not roll back its own abuse counter.
    require(bucket.count <= settings.auth_account_attempts, "Too many sign-in attempts. Try again later", 429)


def _clear_failed_logins(db, email):
    key, _ = _login_bucket(db, email)
    db.execute(delete(RateBucket).where(RateBucket.key == key))


def handle(action, data, db, request, response, party):
    d = data.model_dump(exclude_none=True)
    if action == "me":
        return party_payload(party, db) if party else None
    if action == "login":
        _, attempt_bucket = _login_bucket(db, d["email"])
        require(
            attempt_bucket is None or attempt_bucket.count < settings.auth_account_attempts,
            "Too many sign-in attempts. Try again later",
            429,
        )
        user = find_email(db, d["email"])
        valid = verify_password(d["password"], user.password_hash if user else None)
        if user is None or not valid or user.data["suspended"]:
            _record_failed_login(db, d["email"])
            require(False, "Invalid email or password", 401)
        _clear_failed_logins(db, d["email"])
        if settings.admin_mfa_required and user.data.get("role") in {
            "ADMIN",
            "system_super_user",
        }:
            result = issue_code(
                db,
                "mfa:" + user.email,
                user.email,
                purpose="mfa",
                expiry_minutes=10,
            )
            return {
                "mfaRequired": True,
                "email": user.email,
                **({"devCode": result["devCode"]} if "devCode" in result else {}),
            }
        old = request.cookies.get(COOKIE)
        if old:
            db.execute(delete(Session).where(Session.token == token_hash(old)))
        set_session(db, user, response, request)
        return party_payload(user, db)
    if action == "verifyMfa":
        email = str(d["email"]).lower()
        consume_code(db, "mfa:" + email, d["code"])
        user = find_email(db, email)
        require(
            user is not None
            and user.data.get("role") in {"ADMIN", "system_super_user"}
            and not user.data.get("suspended"),
            "Invalid or expired verification code",
            400,
        )
        old = request.cookies.get(COOKIE)
        if old:
            db.execute(delete(Session).where(Session.token == token_hash(old)))
        set_session(db, user, response, request)
        return party_payload(user, db)
    if action == "logout":
        db.execute(
            delete(Session).where(Session.token == token_hash(request.cookies.get(COOKIE, "")))
        )
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}
    if action == "requestPasswordReset":
        email = str(d["email"]).lower()
        user = find_email(db, email)
        result = issue_code(db, "reset:" + email, email, purpose="reset") if user else {}
        return {"sent": True, **({"devCode": result["devCode"]} if "devCode" in result else {})}
    if action == "resetPassword":
        email = str(d["email"]).lower()
        consume_code(db, "reset:" + email, d["code"])
        user = find_email(db, email)
        require(user is not None, "Invalid or expired verification code", 400)
        require(
            validate_new_password(d["newPassword"]),
            "Use at least 12 characters with upper-case, lower-case and a number",
            400,
        )
        user.password_hash = passwords.hash(d["newPassword"])
        user.data = {**user.data, "temporaryPassword": False}
        revoke(db, user)
        return {"ok": True}
    if action == "changePassword":
        require(
            verify_password(d["currentPassword"], party.password_hash),
            "Current password is incorrect",
            400,
        )
        require(
            validate_new_password(d["newPassword"]),
            "Use at least 12 characters with upper-case, lower-case and a number",
            400,
        )
        party.password_hash = passwords.hash(d["newPassword"])
        party.data = {**party.data, "temporaryPassword": False}
        count = revoke(db, party)
        set_session(db, party, response, request)
        return {"ok": True, "signedOut": max(0, count - 1)}
    if action == "logoutOtherSessions":
        return {"count": revoke(db, party, request.cookies.get(COOKIE))}
    if action == "sessions":
        current_token = token_hash(request.cookies.get(COOKIE, ""))
        return [
            {
                "id": session.token,
                "current": session.token == current_token,
                "createdAt": session.created_at.isoformat(),
                "lastSeenAt": session.last_seen_at.isoformat(),
                "expiresAt": session.expires.isoformat(),
                "ipAddress": session.ip_address,
                "userAgent": session.user_agent,
            }
            for session in db.scalars(
                select(Session)
                .where(Session.user_id == party.id)
                .order_by(Session.last_seen_at.desc())
            )
        ]
    if action == "revokeSession":
        current_token = token_hash(request.cookies.get(COOKIE, ""))
        require(d["sessionId"] != current_token, "Use sign out to end this session", 409)
        count = db.execute(
            delete(Session).where(
                Session.token == d["sessionId"], Session.user_id == party.id
            )
        ).rowcount
        require(count == 1, "Session not found", 404)
        return {"ok": True}
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
        set_session(db, party, response, request)
        return party_payload(party, db)
    if action == "updateProfile":
        if d.get("avatarUrl"):
            from app.services.storage import own_upload

            own_upload(db, party, d["avatarUrl"], "AVATAR")
        party.data = {**party.data, **d}
        return party_payload(party, db)
    raise AssertionError(action)
