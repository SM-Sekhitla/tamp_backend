from app.api.deps import is_admin
from app.core.errors import require
from app.core.security import iso, new_id
from app.services.domain import audit
from app.services.messaging import authorize
from app.services.records import put, rows


def handle(action, data, db, request, response, party):
    if action == "conversations":
        visible = []
        all_messages = rows(db, "messages")
        for row in rows(db, "conversations"):
            participants = {
                row.data["ownerId"],
                row.data["carrierId"],
                row.data.get("driverId"),
            }
            if not is_admin(party) and party.id not in participants:
                continue
            unread = sum(party.id not in message.data.get("readBy", []) and message.data.get("senderId") != party.id for message in all_messages if message.data["conversationId"] == row.id)
            visible.append({**row.data, "unreadCount": unread})
        return sorted(visible, key=lambda item: item["lastMessageAt"], reverse=True)
    conversation = authorize(db, party, data.conversationId)
    messages = [row.data for row in rows(db, "messages") if row.data["conversationId"] == conversation["id"]]
    if action == "list":
        return sorted(messages, key=lambda item: item["createdAt"])
    if action == "markRead":
        count = 0
        for row in rows(db, "messages"):
            if row.data["conversationId"] == conversation["id"] and party.id not in row.data.get("readBy", []):
                row.data = {**row.data, "readBy": [*row.data.get("readBy", []), party.id]}
                count += 1
        return {"updated": count}
    require(action == "send", "Unknown message action", 404)
    values = data.model_dump(mode="json", exclude_none=True)
    if values.get("attachmentUrl"):
        from app.services.storage import own_upload

        own_upload(db, party, values["attachmentUrl"], "MESSAGE")
    message = {"id": new_id("MS"), "conversationId": conversation["id"], "senderId": party.id, "senderRole": party.data["role"], "kind": "USER", "text": values.get("text", "").strip(), "attachmentUrl": values.get("attachmentUrl"), "attachmentName": values.get("attachmentName"), "createdAt": iso(), "readBy": [party.id]}
    put(db, "messages", message, party.id)
    put(db, "conversations", {**conversation, "lastMessageAt": message["createdAt"], "lastMessage": message["text"] or message.get("attachmentName") or "Attachment"})
    audit(db, party, "MESSAGE_SENT", "TRIP", conversation["tripId"], "Trip message sent")
    return message
