from dataclasses import dataclass

from app.controllers import (
    asset_hire,
    passenger_charter,
    auth,
    commands,
    documents,
    finance,
    freight_workspace,
    messages,
    operations,
    parties,
    passenger,
    utilities,
    webhooks,
)
from app.controllers import dispatch as dispatch_controller
from app.controllers import organisations as organisations_controller
from app.controllers import vehicles as vehicles_controller
from app.schemas import asset_hire as assets
from app.schemas import auth as a
from app.schemas import domain as d
from app.schemas import passenger as p
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
        ("verifyMfa", a.MfaVerify, True, True),
        ("logout", a.Empty, True, False),
        ("sessions", a.Empty, False, False),
        ("revokeSession", a.SessionId, True, False),
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
        ("attachRegistrationDocuments", a.RegistrationDocuments, True, False),
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
        ("attachRegistrationDocuments", a.RegistrationDocuments, True, False),
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
        ("setVerification", a.OrganisationVerification, True, False),
        ("setMemberRoles", a.OrganisationMemberRoles, True, False),
        ("removeMember", a.OrganisationMemberRemove, True, False),
        ("switch", a.OrganisationId, True, False),
    ],
)
group("vehicles", [("setVerification", a.VehicleVerification, True, False)])
group("documents", [("list", a.Empty, False, False), ("review", a.DocumentReview, True, False)])
group(
    "commands",
    [
        (n, d.OfferPrepare if n == "prepareOffer" else d.Command, True, False)
        for n in ["prepareOffer", "requestJob", "advanceTrip", "acceptMatch", "confirmMatch", "acceptRequest", "declineMatch"]
    ] + [
        ("reportIncident", d.IncidentReport, True, False),
        ("cancelTrip", d.TripCancellation, True, False),
    ],
)
group("snapshot", [("load", a.Empty, False, False), ("sync", d.Delta, True, False)])
group("loads", [("list", a.Empty, False, False), ("create", d.LoadInput, True, False)])
group("operations", [("incidentAction", d.IncidentAction, True, False)])
group("finance", [("invoiceAction", d.InvoiceAction, True, False)])
group(
    "freightWorkspace",
    [
        ("list", a.Empty, False, False),
        ("saveCustomer", d.FreightCustomerUpsert, True, False),
        ("saveLocation", d.SavedLocationUpsert, True, False),
        ("saveTemplate", d.LoadTemplateUpsert, True, False),
        ("delete", d.FreightWorkspaceDelete, True, False),
    ],
)
group("assetHire", [
    ("list", a.Empty, False, False),
    ("save", assets.Asset, True, False),
    ("request", assets.Booking, True, False),
    ("transition", assets.Action, True, False),
    ("move", assets.Move, True, False),
    ("handover", assets.Handover, True, False),
    ("issueInvoice", assets.RentalInvoice, True, False),
    ("recordPayment", assets.RentalPayment, True, False),
])
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
        # Score is read-only, but its full domain payload can exceed safe URL
        # lengths. Register it as POST so the payload is carried in the body.
        ("score", d.Score, True, False),
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
    "geocode",
    [
        ("autocomplete", d.Autocomplete, False, True),
        ("details", d.Details, False, True),
        ("reverse", d.ReverseGeocode, False, True),
    ],
)
group("passenger", [
    ("catalogue", a.Empty, False, False),
    ("fleet", a.Empty, False, False),
    ("save", p.PassengerVehicle, True, False),
    ("reviews", a.Empty, False, False),
    ("review", p.Review, True, False),
])
group("charter", [
    ("rate", p.CharterRating, True, False),
    ("driverJobs", a.Empty, False, False),
    ("respondJob", p.CharterJobResponse, True, False),
    ("advanceJob", p.CharterLegProgress, True, False),
    ("publishPosition", p.CharterPosition, True, False),
    ("live", p.CharterLiveView, False, False),
    ("driverOptions", p.CharterDriverOptions, False, False),
    ("reassignDriver", p.CharterReassignDriver, True, False),
    ("list", a.Empty, False, False),
    ("search", p.CharterSearch, True, False),
    ("requestGroup", p.CharterGroupRequest, True, False),
    ("transitionGroup", p.CharterGroupAction, True, False),
    ("replacementOptions", p.CharterReplacement, False, False),
    ("replaceProvider", p.CharterReplacement, True, False),
    ("request", p.CharterRequest, True, False),
    ("recommend", p.CharterRecommendation, False, False),
    ("quote", p.CharterQuote, True, False),
    ("transition", p.CharterAction, True, False),
])
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
        "assetHire": asset_hire,
        "passenger": passenger,
        "charter": passenger_charter,
        "auth": auth,
        "parties": parties,
        "users": parties,
        "commands": commands,
        "operations": operations,
        "finance": finance,
        "freightWorkspace": freight_workspace,
        "messages": messages,
        "dispatch": dispatch_controller,
        "documents": documents,
        "webhooks": webhooks,
        "organisations": organisations_controller,
        "vehicles": vehicles_controller,
    }
    if prefix in controllers:
        return controllers[prefix].handle(action, data, db, request, response, party)
    if name == "snapshot.load":
        return snapshot(db, party)
    if name == "snapshot.sync":
        return sync(db, party, data)
    return utilities.handle(name, data, db, request, response, party)
