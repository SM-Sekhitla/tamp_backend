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
