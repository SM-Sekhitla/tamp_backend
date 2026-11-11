from datetime import date, datetime, timezone

from sqlalchemy import select

from app.core.security import iso, new_id
from app.db.models import MODELS, OrganisationMembership, UserRole


def notify_organisation(
    db,
    organisation_id,
    *,
    title,
    detail,
    subject_type,
    subject_id,
    tone="info",
    role_ids=None,
):
    """Create a separate notification for each eligible member of one organisation.

    Separate rows keep read/unread state private to each account while the
    organisation_id prevents a notification leaking into another active tenant.
    """
    member_ids = set(
        db.scalars(
            select(OrganisationMembership.user_id).where(
                OrganisationMembership.organisation_id == organisation_id,
                OrganisationMembership.status == "ACTIVE",
            )
        )
    )
    if role_ids:
        member_ids &= set(
            db.scalars(
                select(UserRole.user_id).where(
                    UserRole.organisation_id == organisation_id,
                    UserRole.role_id.in_(role_ids),
                )
            )
        )
    model = MODELS["notifications"]
    for user_id in member_ids:
        ident = new_id("NT")
        db.add(
            model(
                id=ident,
                owner_id=user_id,
                organisation_id=organisation_id,
                data={
                    "id": ident,
                    "title": title,
                    "detail": detail,
                    "at": iso(),
                    "unread": True,
                    "tone": tone,
                    "subjectType": subject_type,
                    "subjectId": subject_id,
                },
            )
        )
    db.flush()
    return len(member_ids)


def create_licence_expiry_notifications(db, *, today: date | None = None) -> int:
    """Create tenant-scoped vehicle licence reminders without duplicates.

    Every active member of the organisation receives their own notification
    row. This deliberately uses the truck record's organisation_id rather than
    the actor/session organisation, because the worker runs outside a request.
    """
    today = today or datetime.now(timezone.utc).date()
    model = MODELS["notifications"]
    created = 0

    for truck_row in db.scalars(select(MODELS["trucks"])):
        organisation_id = truck_row.organisation_id
        expiry_value = truck_row.data.get("licenceExpiry")
        if not organisation_id or not expiry_value:
            continue
        try:
            expiry = datetime.fromisoformat(expiry_value.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            continue

        days = (expiry - today).days
        if days > 30:
            continue
        if days < 0:
            title = "Vehicle licence expired"
            detail = f"{truck_row.data['registration']} licence expired on {expiry.isoformat()}."
            tone = "warning"
            reminder_key = "expired"
        else:
            title = "Vehicle licence expiring"
            detail = (
                f"{truck_row.data['registration']} licence expires on {expiry.isoformat()} "
                f"({days} day{'s' if days != 1 else ''} remaining)."
            )
            tone = "warning" if days <= 7 else "info"
            reminder_key = "30-days"

        # The key makes the worker idempotent while allowing a later 'expired'
        # reminder after the earlier 30-day warning.
        event_key = f"licence:{truck_row.id}:{expiry.isoformat()}:{reminder_key}"
        exists = db.scalar(
            select(model.id).where(
                model.organisation_id == organisation_id,
                model.data["eventKey"].as_string() == event_key,
            ).limit(1)
        )
        if exists:
            continue

        created += notify_organisation(
            db,
            organisation_id,
            title=title,
            detail=detail,
            subject_type="TRUCK",
            subject_id=truck_row.id,
            tone=tone,
        )
        # Stamp the rows created for this organisation/subject so subsequent
        # worker passes can recognise the reminder.
        for notification in db.scalars(
            select(model).where(
                model.organisation_id == organisation_id,
                model.data["subjectId"].as_string() == truck_row.id,
                model.data["title"].as_string() == title,
            )
        ):
            if "eventKey" not in notification.data:
                notification.data = {**notification.data, "eventKey": event_key}
    db.flush()
    return created
