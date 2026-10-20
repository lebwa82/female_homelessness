"""API-inbox bridge. This module contains no messenger or dialogue logic."""

import hashlib
import hmac
import mimetypes
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import aiohttp

from app.connectors.http import File, TransportError

RECEIPT_ATTRIBUTE = "connector_delivery_receipt"


class Chatwoot:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http
        self.base = settings.chatwoot_url.rstrip("/")
        self.root = f"{self.base}/api/v1/accounts/{settings.account_id}"
        self.headers = {"Api-Access-Token": settings.api_token.get_secret_value(),
                        "Accept-Encoding": "identity"}
        self.public = (f"{self.base}/public/api/v1/inboxes/"
                       f"{quote(settings.inbox_identifier, safe='')}/contacts")

    async def api(self, method, path, payload=None, data=None):
        return await self.http.json(method, self.root + path, headers=self.headers,
                                    payload=payload, data=data)

    async def check_inbox(self):
        inbox = await self.api("GET", f"/inboxes/{self.settings.inbox_id}")
        # Refuse a Telegram inbox even if an operator supplies the wrong ID.
        if (inbox.get("channel_type") != "Channel::Api"
                or inbox.get("inbox_identifier") != self.settings.inbox_identifier
                or inbox.get("hmac_mandatory") is not True
                or urlsplit(inbox.get("webhook_url") or "").path !=
                "/webhooks/chatwoot/" + self.settings.callback_secret.get_secret_value()):
            raise ValueError("connector requires its own HMAC-protected API inbox")

    def source(self, peer):
        return f"{self.settings.scope}:{peer}"

    async def conversation(self, peer):
        source = self.source(peer)
        signature = hmac.new(self.settings.inbox_hmac_token.get_secret_value().encode(),
                             source.encode(), hashlib.sha256).hexdigest()
        # ContactInboxWithContactBuilder reuses this exact source_id on retries.
        contact = await self.http.json("POST", self.public, payload={
            "source_id": source, "identifier": source, "identifier_hash": signature,
            "name": "ВКонтакте" if self.settings.provider == "vk" else "WhatsApp",
        })
        response = await self.api("GET", f"/contacts/{contact['id']}/conversations")
        conversations = response.get("payload")
        if not isinstance(conversations, list):
            raise TransportError(502)
        conversations = [c for c in conversations if c.get("inbox_id") == self.settings.inbox_id]
        if conversations:
            # Reuse the contact's latest thread, including resolved conversations.
            # An incoming Chatwoot message reopens it using Chatwoot's own rules.
            return max(conversations, key=lambda c: c["id"])["id"]
        conversation = await self.api("POST", "/conversations", {
            "source_id": source, "inbox_id": self.settings.inbox_id, "contact_id": contact["id"],
        })
        return conversation["id"]

    async def messages(self, conversation_id, *, before=None):
        query = f"?before={before}" if before is not None else ""
        response = await self.api("GET", f"/conversations/{conversation_id}/messages{query}")
        messages = response.get("payload")
        if not isinstance(messages, list):
            raise TransportError(502)
        return messages

    async def has_source(self, conversation_id, source):
        before = None
        # Normal retries hit Redis first; reconcile uncertain POST outcomes in
        # recent history without scanning a whole conversation on every input.
        for _ in range(2):
            messages = await self.messages(conversation_id, before=before)
            if any(m.get("source_id") == source for m in messages):
                return True
            if not messages:
                return False
            next_before = min(m["id"] for m in messages)
            if before is not None and next_before >= before:
                raise TransportError(502)
            before = next_before
        return False

    async def incoming(self, conversation_id, event, files):
        source = f"{self.settings.scope}:{event['peer']}:{event['id']}"
        if await self.has_source(conversation_id, source):
            return
        path = f"/conversations/{conversation_id}/messages"
        payload = {"content": event["text"], "message_type": "incoming", "source_id": source}
        if not files:
            await self.api("POST", path, payload)
            return
        form = aiohttp.FormData()
        for key, value in payload.items():
            form.add_field(key, value)
        for file in files:
            form.add_field("attachments[]", file.data, filename=file.name, content_type=file.mime)
        await self.api("POST", path, data=form)

    async def outgoing(self, event):
        conversation_id, message_id = event["conversation_id"], event["message_id"]
        conversation = await self.api("GET", f"/conversations/{conversation_id}")
        if conversation.get("inbox_id") != self.settings.inbox_id:
            return None
        source = (conversation.get("contact_inbox") or {}).get("source_id", "")
        prefix = self.settings.scope + ":"
        if not isinstance(source, str) or not source.startswith(prefix):
            return None
        peer = source[len(prefix):]
        if not peer.isascii() or not peer.isdigit() or int(peer) <= 0:
            return None
        messages = await self.messages(conversation_id, before=message_id + 1)
        message = next((m for m in messages if m.get("id") == message_id), None)
        if (message is None or message.get("private") is not False
                or message.get("message_type") not in (1, "outgoing")
                or (message.get("content_attributes") or {}).get("deleted")):
            return None
        return peer, message

    async def attachment(self, attachment):
        url = attachment.get("data_url") or ""
        allowed = {urlsplit(self.base).hostname}
        allowed.update(h.strip() for h in self.settings.attachment_hosts.split(",") if h.strip())
        data = await self.http.download(url, allowed_hosts=allowed, http_origin=self.base)
        name = PurePosixPath(urlsplit(url).path).name or "document.pdf"
        if data.startswith(b"%PDF-"):
            mime = "application/pdf"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            mime = mimetypes.guess_type(name)[0] or {
                "image": "image/jpeg", "audio": "audio/ogg", "video": "video/mp4"}.get(
                attachment.get("file_type"), "application/octet-stream")
        return File(name, mime, data)

    async def receipt(self, conversation_id, message_id, external_ids):
        # API message update accepts status, not source_id. Preserve the provider
        # acceptance receipt in an ordinary private Chatwoot note instead.
        messages = await self.messages(conversation_id)
        if any((m.get("content_attributes") or {}).get(RECEIPT_ATTRIBUTE, {}).get("message_id")
               == message_id for m in messages):
            return
        await self.api("POST", f"/conversations/{conversation_id}/messages", {
            "content": f"Коннектор {self.settings.provider}: провайдер принял сообщение.",
            "message_type": "outgoing", "private": True,
            "content_attributes": {RECEIPT_ATTRIBUTE: {
                "message_id": message_id, "provider": self.settings.provider,
                "external_ids": external_ids,
            }},
        })

    async def failed(self, conversation_id, message_id, code):
        await self.api("PATCH", f"/conversations/{conversation_id}/messages/{message_id}", {
            "status": "failed", "external_error": f"connector_{self.settings.provider}:{code}",
        })

    async def delivery_status(self, conversation_id, message_id, status):
        await self.api("PATCH", f"/conversations/{conversation_id}/messages/{message_id}", {
            "status": status,
        })


def outgoing_event(payload, settings):
    if (not isinstance(payload, dict) or payload.get("event") != "message_created"
            or payload.get("message_type") != "outgoing" or payload.get("private") is not False
            or (payload.get("inbox") or {}).get("id") != settings.inbox_id
            or (payload.get("account") or {}).get("id") != settings.account_id):
        return None
    identity = payload.get("id")
    conversation = (payload.get("conversation") or {}).get("id")
    if any(type(i) is not int or i <= 0 for i in (identity, conversation)):
        return None
    # Canonical content, privacy and recipient are re-read by the worker.
    return {"kind": "outgoing", "message_id": identity, "conversation_id": conversation}
