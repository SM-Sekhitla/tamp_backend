from app.api.deps import is_admin, role
from app.controllers.auth import create_party, find_email
from app.core.errors import require
from app.core.security import iso
from app.db.models import Party
from app.services.email import consume_code


def role_for(kind, driver=None):
    if kind == "DRIVER":
        return "TRANSPORTER" if (driver or {}).get("workType") == "OWNER" else "DRIVER"
    return "TRANSPORTER" if kind == "CARRIER" else "FREIGHT_OWNER"


def handle(action, data, db, request, response, party):
    d = data.model_dump(exclude_none=True)
    if action == "register":
        email = str(d["email"]).lower()
        require(find_email(db, email) is None, "Email unavailable", 409)
        consume_code(db, "register:" + email, d["code"])
        name = (d["firstName"] + " " + d["lastName"]).strip()
        user = create_party(
            db,
            email,
            d["password"],
            role_for(d["userType"], d.get("driver")),
            name,
            (d.get("business") or {}).get("companyName") or name,
            onboardingUserType=d["userType"],
        )
        user.data = {**user.data, "phone": d["phone"], "province": d["province"]}
        user.onboarding = {
            k: d[k] for k in ["userType", "business", "driver", "documents"] if k in d
        }
        return {"id": user.id}
    if action == "list":
        from app.services.domain import snapshot

        return snapshot(db, party)["parties"]
    if action == "findDriver":
        role(party, "TRANSPORTER", "ADMIN")
        driver = find_email(db, d["email"])
        return driver.data if driver and driver.data["role"] == "DRIVER" else None
    if action == "onboarding":
        require(party.id == d["partyId"] or is_admin(party))
        user = db.get(Party, d["partyId"])
        require(user is not None, "User not found", 404)
        return user.onboarding
    if action == "setVerification":
        role(party, "ADMIN")
        from app.services.domain import lock_domain, refresh_suggestions

        lock_domain(db)
        user = db.get(Party, d["partyId"])
        require(user is not None, "User not found", 404)
        user.data = {**user.data, "verification": d["status"]}
        from app.services.domain import audit

        audit(db, party, "VERIFICATION_CHANGED", "USER", user.id, "Verification updated")
        refresh_suggestions(db)
        return {"ok": True}
    if action == "completeOnboarding":
        require(not party.data.get("onboardingComplete"), "Onboarding already complete", 409)
        party.data = {
            **party.data,
            "role": role_for(d["userType"]),
            "onboardingComplete": True,
            "onboardingUserType": d["userType"],
            **{k: d[k] for k in ["companyName", "phone", "province"]},
        }
        return party.data
    if action == "uploadKyc":
        from app.services.storage import own_upload

        own_upload(db, party, d["url"], "KYC")
        party.data = {
            **party.data,
            "verification": "PENDING",
            "kycDocument": {"name": d["name"], "url": d["url"], "uploadedAt": iso()},
        }
        return party.data
    raise AssertionError(action)
