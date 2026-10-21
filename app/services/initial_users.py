import asyncio
import logging
import secrets

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.security import iso, new_id, passwords
from app.db.models import User
from app.db.session import AsyncSessionLocal

logger = logging.getLogger("tamp.bootstrap")
_BOOTSTRAP_LOCK_ID = 847220


def _generate_password():
    return secrets.token_urlsafe(18)


async def ensure_platform_super_user():
    # Create the initial platform super-user once when bootstrap is enabled.
    if not settings.bootstrap_secret:
        return None

    username = settings.default_platform_admin_username.strip().lower()
    email = settings.default_platform_admin_email.strip().lower()
    if not username or not email:
        raise RuntimeError(
            "DEFAULT_PLATFORM_ADMIN_USERNAME and DEFAULT_PLATFORM_ADMIN_EMAIL "
            "must be set when BOOTSTRAP_SECRET is configured"
        )

    generated_password = None
    try:
        async with AsyncSessionLocal.begin() as db:
            await db.execute(text(f"SELECT pg_advisory_xact_lock({_BOOTSTRAP_LOCK_ID})"))
            existing = await db.scalar(
                select(User)
                .where(
                    or_(
                        User.email == email,
                        User.data["username"].as_string() == username,
                    )
                )
                .limit(1)
            )
            if existing is not None:
                if existing.data.get("role") != "system_super_user":
                    logger.warning(
                        "Platform bootstrap identity already belongs to account id=%s "
                        "with role=%s; no account was created",
                        existing.id,
                        existing.data.get("role"),
                    )
                else:
                    logger.info(
                        "Platform system_super_user ready username=%s email=%s account_id=%s",
                        existing.data.get("username", username),
                        existing.email,
                        existing.id,
                    )
                return existing.id

            fixed_password = (settings.default_platform_admin_password or "").strip()
            password = fixed_password or _generate_password()
            generated_password = None if fixed_password else password
            ident = new_id("SUP")
            password_hash = await asyncio.to_thread(passwords.hash, password)
            db.add(
                User(
                    id=ident,
                    email=email,
                    password_hash=password_hash,
                    data={
                        "id": ident,
                        "username": username,
                        "role": "system_super_user",
                        "email": email,
                        "contactName": "Platform Administrator",
                        "companyName": "TAMP",
                        "phone": "",
                        "province": "",
                        "verification": "VERIFIED",
                        "ratingAvg": None,
                        "ratingCount": 0,
                        "suspended": False,
                        "createdAt": iso(),
                        "onboardingComplete": True,
                        "temporaryPassword": generated_password is not None,
                    },
                    onboarding={},
                )
            )
            await db.flush()
            created = await db.get(User, ident)
            from app.services.rbac import provision_user_access

            await db.run_sync(lambda sync_db: provision_user_access(sync_db, created))
    except IntegrityError:
        logger.info("Platform super-user was bootstrapped by another process")
        return None

    if generated_password is not None:
        logger.warning(
            "Bootstrapped platform system_super_user username=%s email=%s "
            "temporary_password=%s -- save it now; it will not be logged again",
            username,
            email,
            generated_password,
        )
    else:
        logger.info(
            "Bootstrapped platform system_super_user username=%s email=%s "
            "using DEFAULT_PLATFORM_ADMIN_PASSWORD",
            username,
            email,
        )
    return ident
