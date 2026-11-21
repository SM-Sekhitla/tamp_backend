from app.api.deps import is_admin, role
from app.controllers.auth import create_party, find_email, set_session
from app.core.errors import require
from datetime import timedelta

from app.core.security import iso, new_id, now
from app.db.models import User
from app.services.email import consume_code


def role_for(kind, driver=None):
    if kind == "DRIVER":
        return "TRANSPORTER" if (driver or {}).get("workType") == "OWNER" else "DRIVER"
    return "TRANSPORTER" if kind == "CARRIER" else "FREIGHT_OWNER"


PROVINCE_CENTRES = {
    "GP": (-26.2041, 28.0473),
    "KZN": (-29.8587, 31.0218),
    "WC": (-33.9249, 18.4241),
    "EC": (-32.2968, 26.4194),
    "FS": (-29.0852, 26.1596),
    "NW": (-25.8650, 25.6442),
    "LP": (-23.8962, 29.4486),
    "MP": (-25.4753, 30.9694),
    "NC": (-28.7282, 24.7499),
}


def create_owner_operator_truck(db, user, driver):
    """Materialise the single vehicle captured during owner-operator signup."""
    from app.services.fleet import sync_vehicle_for_truck
    from app.services.records import put

    captured = driver["truck"]
    province = user.data["province"]
    lat, lng = PROVINCE_CENTRES.get(province, (-30.5595, 22.9375))
    created = now()
    truck = {
        "id": new_id("TR"),
        "transporterId": user.id,
        "driverId": user.id,
        "registration": captured["registration"].strip().upper(),
        "bodyType": captured["bodyType"],
        "payloadCapacityKg": captured["capacityT"] * 1000,
        "currentLocation": {
            "label": f"{province} — update exact vehicle location",
            "province": province,
            "lat": lat,
            "lng": lng,
        },
        "availableFrom": created.isoformat(),
        "availableTo": (created + timedelta(days=30)).isoformat(),
        "preferredLanes": [],
        "status": "OFFLINE",
        "createdAt": created.isoformat(),
        "alwaysAvailable": False,
        "photos": [],
        "documents": [],
        "documentReviews": {},
        "vehicleVerification": "PENDING",
    }
    sync_vehicle_for_truck(db, user, truck)
    put(db, "trucks", truck, user.id)
    return truck


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
        owner_operator = (
            d["userType"] == "DRIVER"
            and (d.get("driver") or {}).get("workType") == "OWNER"
        )
        user.data = {**user.data, "ownerOperator": owner_operator}
        user.onboarding = {
            k: d[k] for k in ["userType", "business", "driver", "documents"] if k in d
        }
        if owner_operator:
            truck = create_owner_operator_truck(db, user, d["driver"])
            user.data = {**user.data, "ownerOperatorTruckId": truck["id"]}
        # The email code has been consumed successfully, so establish the
        # pending account's restricted session. This lets registration upload
        # private KYC files immediately without exposing an anonymous upload
        # endpoint or bypassing the normal ownership checks.
        set_session(db, user, response, request)
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
        user = db.get(User, d["partyId"])
        require(user is not None, "User not found", 404)
        return user.onboarding
    if action == "setVerification":
        role(party, "ADMIN")
        from app.services.domain import lock_domain, refresh_suggestions

        lock_domain(db)
        user = db.get(User, d["partyId"])
        require(user is not None, "User not found", 404)
        if d["status"] == "VERIFIED":
            submitted_documents = (user.onboarding or {}).get("documents", [])
            if submitted_documents:
                require(
                    all(
                        isinstance(document, dict)
                        and document.get("url")
                        and document.get("reviewStatus") == "APPROVED"
                        for document in submitted_documents
                    ),
                    "Approve every registration document before verifying this account",
                    409,
                )
        user.data = {**user.data, "verification": d["status"]}
        if d["status"] == "VERIFIED":
            from app.services.email import send_account_verified

            send_account_verified(user.email, user.data.get("contactName", "TAMP member"))
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
    if action == "attachRegistrationDocuments":
        from app.services.domain import audit
        from app.services.storage import own_upload

        submitted = d["documents"]
        for document in submitted:
            own_upload(db, party, document["url"], "KYC")
        uploaded_at = iso()
        documents = [
            {
                **document,
                "id": document.get("id") or new_id("RD"),
                "category": document.get("category") or "REGISTRATION",
                "status": document.get("status") or "PENDING",
                "reviewStatus": document.get("reviewStatus") or "PENDING",
                "uploadedAt": document.get("uploadedAt") or uploaded_at,
                "uploadedBy": document.get("uploadedBy") or party.id,
            }
            for document in submitted
        ]
        party.onboarding = {
            **(party.onboarding or {}),
            "documents": documents,
        }
        if party.data.get("role") == "DRIVER":
            from app.services.driver_compliance import sync_registration_documents

            sync_registration_documents(db, party, documents=documents)
        if party.data.get("ownerOperator"):
            from app.services.records import get, put

            truck_row = get(db, "trucks", party.data.get("ownerOperatorTruckId"))
            if truck_row:
                from app.services.fleet import sync_vehicle_for_truck

                vehicle_labels = {
                    "Vehicle licence disc (registration)",
                    "Roadworthy certificate",
                    "Vehicle insurance certificate",
                }
                vehicle_documents = [
                    document for document in documents if document["label"] in vehicle_labels
                ]
                updated_truck = {
                    **truck_row.data,
                    "documents": [document["url"] for document in vehicle_documents],
                    "documentReviews": {
                        document["url"]: {"reviewStatus": "PENDING"}
                        for document in vehicle_documents
                    },
                }
                sync_vehicle_for_truck(db, party, updated_truck, truck_row.data)
                put(db, "trucks", updated_truck, party.id)
        audit(
            db,
            party,
            "REGISTRATION_DOCUMENTS_ATTACHED",
            "USER",
            party.id,
            f"{len(documents)} registration documents attached",
        )
        return party.onboarding
    raise AssertionError(action)
