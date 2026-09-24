import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage

import httpx

from app.core.config import settings
from app.core.errors import AppError, require
from app.core.security import now, token_hash
from app.db.models import EmailCode


def mailjet_configured():
    return all(
        (
            settings.mailjet_api_key,
            settings.mailjet_secret_key,
            settings.mailjet_from_email,
        )
    )


def send_with_mailjet(email, code):
    payload = {
        "Messages": [
            {
                "From": {
                    "Email": settings.mailjet_from_email,
                    "Name": settings.mailjet_from_name,
                },
                "To": [{"Email": email}],
                "Subject": "Your TAMP verification code",
                "TextPart": (f"Your TAMP verification code is {code}. It expires in 10 minutes."),
            }
        ]
    }
    try:
        response = httpx.post(
            "https://api.mailjet.com/v3.1/send",
            auth=(settings.mailjet_api_key, settings.mailjet_secret_key),
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AppError("Email delivery unavailable", 503) from exc


def issue_code(db, key, email):
    code = f"{secrets.randbelow(1000000):06d}"
    record = db.get(EmailCode, key)
    if record is None:
        record = EmailCode(key=key)
        db.add(record)
    record.digest, record.expires, record.attempts, record.target = (
        token_hash(key + code),
        now() + timedelta(minutes=10),
        0,
        email,
    )
    if mailjet_configured():
        send_with_mailjet(email, code)
        return {"sent": True, "live": True}
    if settings.smtp_host:
        message = EmailMessage()
        message["Subject"] = "Your TAMP verification code"
        message["From"], message["To"] = settings.smtp_from, email
        message.set_content(f"Your TAMP verification code is {code}. It expires in 10 minutes.")
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise AppError("Email delivery unavailable", 503) from exc
        return {"sent": True, "live": True}
    require(
        settings.environment != "production" and settings.dev_email_codes,
        "Configure Mailjet or SMTP to send verification emails",
        503,
    )
    return {"sent": True, "live": False, "devCode": code}


def consume_code(db, key, code):
    from sqlalchemy import select

    record = db.scalar(select(EmailCode).where(EmailCode.key == key).with_for_update())
    require(
        record is not None and record.expires > now() and record.attempts < 5,
        "Invalid or expired verification code",
        400,
    )
    if not secrets.compare_digest(record.digest, token_hash(key + code)):
        record.attempts += 1
        db.commit()  # Persist failed attempts even though the request is rejected.
        raise AppError("Invalid or expired verification code", 400)
    target = record.target
    db.delete(record)
    return target
