from datetime import timedelta
from io import BytesIO

from PIL import Image
from sqlalchemy import select

from app.core.security import new_id, now
from app.db.models import Organisation, OrganisationMembership
from app.services.records import put

from .conftest import db_call, login, result, rpc
from .test_workflow import PLACE


def test_minibus_registration_review_and_publication(client, accounts):
    def setup(db):
        org = db.scalar(select(OrganisationMembership.organisation_id).where(
            OrganisationMembership.user_id == accounts["carrier"]["id"],
            OrganisationMembership.is_primary.is_(True),
        ))
        db.get(Organisation, org).verification = "VERIFIED"
        db.info["active_organisation_id"] = org
        for document in ("PRDP", "DRIVING_LICENCE"):
            put(db, "driverDocuments", {
                "id": new_id("DD"), "driverId": accounts["driver"]["id"],
                "documentType": document, "reviewStatus": "APPROVED",
                "expiresAt": (now() + timedelta(days=30)).isoformat(),
            }, accounts["carrier"]["id"])

    db_call(setup)
    login(client, "carrier")
    image = BytesIO()
    Image.new("RGB", (16, 16), "yellow").save(image, "PNG")

    def upload(kind, name):
        response = client.post("/api/uploads", data={"kind": kind},
                               files={"file": (name + ".png", image.getvalue(), "image/png")})
        assert response.status_code == 200, response.text
        return response.json()["url"]

    photo = upload("PASSENGER_PHOTO", "minibus")
    documents = {
        key: upload("PASSENGER_DOCUMENT", key)
        for key in ("licence", "roadworthy", "operatingLicence", "insurance")
    }
    start = now() + timedelta(days=1)
    expiry = (start + timedelta(days=30)).date().isoformat()
    values = {
        "type": "MINIBUS", "make": "Toyota", "model": "Hiace", "registration": "AB 12 CD GP",
        "seats": 15, "wheelchairPositions": 1, "displacedSeats": 2,
        "location": PLACE, "serviceArea": "Gauteng",
        "availability": {"start": start.isoformat(), "end": (start + timedelta(days=7)).isoformat()},
        "licenceExpiry": expiry, "permitExpiry": expiry, "photos": [photo], "documents": documents,
        "driverId": accounts["driver"]["id"],
    }
    saved = result(rpc(client, "passenger.save", values, True))
    assert saved["verification"] == "PENDING"
    assert rpc(client, "passenger.save", values, True).status_code == 409
    fleet = result(rpc(client, "passenger.fleet"))
    assert fleet["drivers"][0]["id"] == accounts["driver"]["id"]
    update = {**values, "id": saved["id"], "version": fleet["vehicles"][0]["version"], "published": True}
    assert rpc(client, "passenger.save", update, True).status_code == 409
    login(client, "owner")
    assert result(rpc(client, "passenger.catalogue")) == []
    assert client.get(photo).status_code == 403
    assert rpc(client, "passenger.reviews").status_code == 403
    login(client, "admin")
    assert client.get(documents["licence"]).status_code == 200
    result(rpc(client, "passenger.review", {
        "id": saved["id"], "version": update["version"], "status": "VERIFIED",
    }, True))
    login(client, "carrier")
    assert rpc(client, "passenger.save", update, True).status_code == 409
    update["version"] = result(rpc(client, "passenger.fleet"))["vehicles"][0]["version"]
    assert result(rpc(client, "passenger.save", update, True))["published"]
    login(client, "owner")
    [listing] = result(rpc(client, "passenger.catalogue"))
    assert listing["seats"] == 15
    assert not {"documents", "registration", "driverId"} & listing.keys()
    assert client.get(photo).status_code == 200
    assert client.get(documents["licence"]).status_code == 403
    login(client, "carrier")
    update["version"] = result(rpc(client, "passenger.fleet"))["vehicles"][0]["version"]
    assert result(rpc(client, "passenger.save", {**update, "seats": 14}, True))["verification"] == "PENDING"
    login(client, "owner")
    assert result(rpc(client, "passenger.catalogue")) == []
    assert client.get(photo).status_code == 403
