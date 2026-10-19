from app.api.deps import is_admin
from app.core.errors import require
from app.core.security import iso, new_id
from app.services.records import needed, put, rows


def conversation_for_trip(db, trip_id):
    return next((row.data for row in rows(db, "conversations") if row.data["tripId"] == trip_id), None)


def ensure_conversation(db, trip_id, load, truck):
    current = conversation_for_trip(db, trip_id)
    if current:
        return current
    conversation = {"id": new_id("CV"), "tripId": trip_id, "loadId": load["id"], "ownerId": load["ownerId"], "carrierId": truck["transporterId"], "driverId": truck.get("driverId"), "createdAt": iso(), "lastMessageAt": iso()}
    put(db, "conversations", conversation, load["ownerId"])
    return conversation


def authorize(db, party, conversation_id):
    conversation = needed(db, "conversations", conversation_id).data
    allowed = {conversation["ownerId"], conversation["carrierId"], conversation.get("driverId")}
    require(is_admin(party) or party.id in allowed, "Conversation is not visible to this account", 403)
    return conversation


def system_message(db, conversation, text, event):
    message = {"id": new_id("MS"), "conversationId": conversation["id"], "senderId": None, "senderRole": "SYSTEM", "kind": "SYSTEM", "text": text, "attachmentUrl": None, "attachmentName": None, "event": event, "createdAt": iso(), "readBy": []}
    put(db, "messages", message)
    put(db, "conversations", {**conversation, "lastMessageAt": message["createdAt"], "lastMessage": text})
    return message
