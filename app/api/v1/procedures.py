from dataclasses import dataclass

from app.controllers import (
    auth,
    commands,
    finance,
    messages,
    operations,
    parties,
    utilities,
    webhooks,
)
from app.controllers import dispatch as dispatch_controller
from app.controllers import organisations as organisations_controller
from app.schemas import auth as a
from app.schemas import domain as d
from app.services.domain import snapshot, sync


@dataclass(frozen=True)
class Procedure:
    schema: type
    mutation: bool
    public: bool = False


REGISTRY = {}


def group(prefix, items):
    for name, schema, mutation, public in items:
        REGISTRY[prefix + "." + name] = Procedure(schema, mutation, public)


group(
    "auth",
    [
        ("me", a.Empty, False, True),
        ("login", a.Login, True, True),
        ("logout", a.Empty, True, False),
        ("changePassword", a.PasswordChange, True, False),
        ("logoutOtherSessions", a.Empty, True, False),
        ("requestPasswordReset", a.Email, True, True),
        ("resetPassword", a.PasswordReset, True, True),
        ("requestEmailChange", a.EmailChange, True, False),
        ("confirmEmailChange", a.Code, True, False),
        ("updateProfile", a.Profile, True, False),
    ],
)
group(
    "parties",
    [
        ("register", a.Register, True, True),
        ("list", a.Empty, False, False),
        ("setVerification", a.Verification, True, False),
        ("completeOnboarding", a.Onboarding, True, False),
        ("findDriver", a.Email, False, False),
        ("onboarding", a.PartyId, False, False),
        ("uploadKyc", a.Kyc, True, False),
    ],
)
group(
    "users",
    [
        ("register", a.Register, True, True),
        ("list", a.Empty, False, False),
        ("setVerification", a.Verification, True, False),
        ("completeOnboarding", a.Onboarding, True, False),
        ("findDriver", a.Email, False, False),
        ("onboarding", a.PartyId, False, False),
        ("uploadKyc", a.Kyc, True, False),
    ],
)
group(
    "organisations",
    [
        ("list", a.Empty, False, False),
        ("members", a.OrganisationId, False, False),
        ("create", a.OrganisationCreate, True, False),
        ("addMember", a.OrganisationMemberAdd, True, False),
        ("update", a.OrganisationUpdate, True, False),
        ("setMemberRoles", a.OrganisationMemberRoles, True, False),
        ("removeMember", a.OrganisationMemberRemove, True, False),
        ("switch", a.OrganisationId, True, False),
    ],
)
group(
    "commands",
    [
        (n, d.OfferPrepare if n == "prepareOffer" else d.Command, True, False)
        for n in ["prepareOffer", "advanceTrip", "acceptMatch", "confirmMatch", "acceptRequest"]
    ] + [
        ("reportIncident", d.IncidentReport, True, False),
        ("cancelTrip", d.TripCancellation, True, False),
    ],
)
group("snapshot", [("load", a.Empty, False, False), ("sync", d.Delta, True, False)])
group("loads", [("list", a.Empty, False, False), ("create", d.LoadInput, True, False)])
group("operations", [("incidentAction", d.IncidentAction, True, False)])
group("finance", [("invoiceAction", d.InvoiceAction, True, False)])
group("dispatch", [("action", d.DispatchAction, True, False)])
group(
    "messages",
    [
        ("conversations", a.Empty, False, False),
        ("list", d.ConversationId, False, False),
        ("send", d.MessageSend, True, False),
        ("markRead", d.ConversationId, True, False),
    ],
)
group("email", [("sendCode", a.Email, True, True)])
group("phone", [("check", d.Phone, False, True)])
group(
    "matching",
    [
        ("score", d.Score, False, False),
        ("bodyTypesForCargo", d.CargoInput, False, False),
        ("roadDistanceKm", d.Distance, False, False),
    ],
)
group("pricing", [("quote", d.Quote, False, False), ("quoteMoney", d.Quote, False, False)])
group(
    "notifications",
    [
        ("list", d.Limit, False, False),
        ("unreadCount", a.Empty, False, False),
        ("markRead", d.Read, True, False),
    ],
)
group(
    "geocode", [("autocomplete", d.Autocomplete, False, True), ("details", d.Details, False, True)]
)
group("routes", [("compute", d.RouteInput, False, False)])
group(
    "webhooks",
    [
        ("list", a.Empty, False, False),
        ("create", d.Hook, True, False),
        ("setActive", d.Active, True, False),
        ("delete", d.Id, True, False),
        ("deliveries", d.Limit, False, False),
    ],
)

group("tracking", [("link", d.TrackingLink, False, False), ("view", d.TrackingView, False, True)])


def dispatch(name, data, db, request, response, party):
    if name.startswith("tracking."):
        from app.services import tracking

        return (
            tracking.link(db, party, data.loadId)
            if name == "tracking.link"
            else tracking.view(db, data.token)
        )
    prefix, action = name.split(".")
    controllers = {
        "auth": auth,
        "parties": parties,
        "users": parties,
        "commands": commands,
        "operations": operations,
        "finance": finance,
        "messages": messages,
        "dispatch": dispatch_controller,
        "webhooks": webhooks,
        "organisations": organisations_controller,
    }
    if prefix in controllers:
        return controllers[prefix].handle(action, data, db, request, response, party)
    if name == "snapshot.load":
        return snapshot(db, party)
    if name == "snapshot.sync":
        return sync(db, party, data)
    return utilities.handle(name, data, db, request, response, party)
