import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from html import escape
from urllib.parse import urlencode

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


def _email_html(*, eyebrow, heading, message, code=None, button_label=None, button_url=None, note=None):
    code_block = (
        f'<div style="margin:24px 0;padding:18px;text-align:center;background:#111318;border:1px solid #34363d;border-radius:12px;'
        f'font:700 30px/1 monospace;letter-spacing:8px;color:#f5b800">{escape(code)}</div>'
        if code else ""
    )
    button = (
        f'<div style="margin:28px 0 8px;text-align:center"><a href="{escape(button_url)}" style="display:block;padding:16px 24px;background:#f5b800;'
        f'border-radius:10px;color:#111318;text-decoration:none;font-size:15px;font-weight:900;letter-spacing:.3px">{escape(button_label)} &nbsp;→</a></div>'
        if button_label and button_url else ""
    )
    note_block = (
        f'<div style="margin:22px 0 0;padding:14px 16px;background:#20232b;border-left:3px solid #f5b800;border-radius:8px;'
        f'color:#d9dae0;font-size:13px;line-height:1.6">{escape(note)}</div>' if note else ""
    )
    return f"""<!doctype html><html><body style="margin:0;background:#0b0d11;color:#f7f7f7;font-family:Arial,sans-serif">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td style="padding:36px 16px">
    <table role="presentation" width="100%" style="max-width:560px;margin:auto;background:#181a20;border:1px solid #30323a;border-radius:18px;overflow:hidden">
    <tr><td style="height:6px;background:#f5b800"></td></tr><tr><td style="padding:34px">
    <div style="color:#ffffff;font-size:28px;font-weight:900;letter-spacing:-2px">TAMP<span style="color:#f5b800">.</span></div>
    <div style="margin-top:30px;color:#f5b800;font-size:11px;font-weight:800;letter-spacing:3px;text-transform:uppercase">{escape(eyebrow)}</div>
    <h1 style="margin:10px 0 12px;color:#ffffff;font-size:26px;line-height:1.2">{escape(heading)}</h1>
    <p style="margin:0;color:#b9bbc3;font-size:15px;line-height:1.7">{escape(message)}</p>{code_block}{button}
    {note_block}
    <p style="margin:26px 0 0;color:#777a84;font-size:12px;line-height:1.6">If you did not request this, you can safely ignore this email. Never share a verification code or password.</p>
    </td></tr></table></td></tr></table></body></html>"""


def send_email(email, subject, text, html):
    if mailjet_configured():
        payload = {
            "Messages": [{
                "From": {"Email": settings.mailjet_from_email, "Name": settings.mailjet_from_name},
                "To": [{"Email": email}], "Subject": subject, "TextPart": text, "HTMLPart": html,
            }]
        }
        try:
            response = httpx.post("https://api.mailjet.com/v3.1/send", auth=(settings.mailjet_api_key, settings.mailjet_secret_key), json=payload, timeout=10)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError("Email delivery unavailable", 503) from exc
        return True
    if settings.smtp_host:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = subject, settings.smtp_from, email
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_starttls: smtp.starttls()
                if settings.smtp_username: smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise AppError("Email delivery unavailable", 503) from exc
        return True
    return False


def send_account_verified(email, name):
    send_email(
        email, "Your TAMP account is verified",
        "Your TAMP account has been verified. You can now access the approved platform features.",
        _email_html(eyebrow="Account approved", heading=f"Welcome aboard, {name}", message="Your account and submitted information have been verified. You can now sign in and use the approved TAMP platform features.", button_label="Sign in to TAMP", button_url=settings.frontend_url.rstrip("/") + "/"),
    )


def issue_code(db, key, email, *, purpose="verification", name=None, expiry_minutes=10):
    code = f"{secrets.randbelow(1000000):06d}"
    record = db.get(EmailCode, key)
    if record is None:
        record = EmailCode(key=key)
        db.add(record)
    record.digest, record.expires, record.attempts, record.target = (
        token_hash(key + code),
        now() + timedelta(minutes=expiry_minutes),
        0,
        email,
    )
    reset_url = settings.frontend_url.rstrip("/") + "/forgot-password?" + urlencode({"email": email, "code": code})
    is_reset = purpose in {"reset", "invite"}
    subject = "Create your TAMP password" if purpose == "invite" else ("Reset your TAMP password" if is_reset else "Your TAMP verification code")
    heading = "Create your password" if purpose == "invite" else ("Reset your password" if is_reset else "Verify your email")
    expiry_text = f"{expiry_minutes} minutes" if expiry_minutes < 60 else f"{expiry_minutes // 60} hours"
    text = (
        f"{name or 'Your organisation'} invited you to TAMP. Create your private password within {expiry_text}: {reset_url}"
        if purpose == "invite"
        else f"Your TAMP code is {code}. It expires in {expiry_text}." + (f" Open {reset_url}" if is_reset else "")
    )
    html = _email_html(
        eyebrow="Team invitation" if purpose == "invite" else ("Password security" if is_reset else "Email verification"),
        heading=heading,
        message=(f"{name or 'Your organisation'} invited you to TAMP. Create a private password to activate your account. This invitation expires in {expiry_text}." if purpose == "invite" else (f"Use the secure button below to choose a new password. This link and code expire in {expiry_text}." if is_reset else f"Enter this one-time code in TAMP to confirm your email address. It expires in {expiry_text}.")),
        code=None if purpose == "invite" else code,
        button_label="Create my password" if purpose == "invite" else ("Reset my password" if is_reset else None),
        button_url=reset_url if is_reset else None,
        note=("For your security, no temporary password is included. Only you can create the password for this account." if purpose == "invite" else None),
    )
    if send_email(email, subject, text, html):
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
