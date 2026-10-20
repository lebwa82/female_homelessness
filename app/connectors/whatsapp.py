"""Official WhatsApp Cloud API transport, with no dependency on Telegram or the bot."""

import hashlib
import hmac
import time
from urllib.parse import urlsplit

from app.connectors.http import File, ProviderError, TransportError, choices, chunks, multipart


class WhatsApp:
    def __init__(self, settings, http, redis):
        self.settings, self.http, self.redis = settings, http, redis
        self.base = f"https://graph.facebook.com/{settings.whatsapp_api_version}"
        self.headers = {"Authorization": f"Bearer {settings.whatsapp_token.get_secret_value()}"}

    def authenticate(self, raw, headers, _payload):
        signature = headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            self.settings.whatsapp_app_secret.get_secret_value().encode(), raw, hashlib.sha256,
        ).hexdigest()
        return isinstance(signature, str) and hmac.compare_digest(signature.encode(),
                                                                expected.encode())

    def incoming(self, payload):
        if payload.get("object") != "whatsapp_business_account":
            return []
        result = []
        for entry in payload.get("entry") or []:
            if str(entry.get("id")) != self.settings.whatsapp_business_id:
                continue
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                if (change.get("field") != "messages"
                        or (value.get("metadata") or {}).get("phone_number_id")
                        != self.settings.whatsapp_phone_id):
                    continue
                for message in value.get("messages") or []:
                    peer, identity = message.get("from", ""), message.get("id", "")
                    if (not isinstance(peer, str) or not peer.isascii() or not peer.isdigit()
                            or not 5 <= len(peer) <= 20 or not isinstance(identity, str)
                            or not identity or len(identity) > 256):
                        continue
                    kind, files = message.get("type"), []
                    text = ""
                    if kind == "text":
                        text = (message.get("text") or {}).get("body", "")
                    elif kind == "interactive":
                        interactive = message.get("interactive") or {}
                        action = interactive.get(interactive.get("type")) or {}
                        text = action.get("id", "")
                    elif kind == "button":
                        text = (message.get("button") or {}).get("payload", "")
                    elif kind in {"image", "document", "audio", "video"}:
                        media = message.get(kind) or {}
                        if media.get("id"):
                            files = [{"id": str(media["id"]), "name": media.get("filename")
                                      or {"image": "image.jpg", "audio": "audio.ogg",
                                          "video": "video.mp4"}.get(kind, "document"),
                                      "mime": media.get("mime_type") or "application/octet-stream"}]
                        text = media.get("caption") or "Входящее вложение WhatsApp."
                    if not isinstance(text, str) or not text.strip():
                        continue
                    try:
                        timestamp = int(message.get("timestamp", 0))
                    except (ValueError, TypeError):
                        continue
                    if timestamp <= 0 or timestamp > time.time() + 300:
                        continue
                    result.append({"id": identity, "peer": peer, "text": text,
                                   "files": files, "timestamp": timestamp})
        return result

    def window_key(self, peer):
        # Hash phone numbers in transport keys; Chatwoot owns the actual identity.
        digest = hashlib.sha256(peer.encode()).hexdigest()
        return f"{self.settings.scope}:window:{digest}"

    def statuses(self, payload):
        if payload.get("object") != "whatsapp_business_account":
            return []
        result = []
        for entry in payload.get("entry") or []:
            if str(entry.get("id")) != self.settings.whatsapp_business_id:
                continue
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                if (change.get("field") != "messages"
                        or (value.get("metadata") or {}).get("phone_number_id")
                        != self.settings.whatsapp_phone_id):
                    continue
                for status in value.get("statuses") or []:
                    identity, state = status.get("id"), status.get("status")
                    if (isinstance(identity, str) and 0 < len(identity) <= 256
                            and state in {"sent", "delivered", "read", "failed"}):
                        result.append({"kind": "status", "id": identity, "status": state,
                                       "peer": "status:" + identity})
        return result

    async def received(self, event):
        # Never extend a service window because Meta redelivered an old message.
        key, timestamp = self.window_key(event["peer"]), int(event["timestamp"])
        await self.redis.eval(
            "local old=tonumber(redis.call('GET',KEYS[1]) or '0'); "
            "if tonumber(ARGV[1])>old then redis.call('SET',KEYS[1],ARGV[1],'EX',86400); end",
            1, key, timestamp,
        )

    async def api(self, suffix, *, payload=None, data=None, method="POST"):
        try:
            return await self.http.json(method, f"{self.base}/{suffix}", headers=self.headers,
                                        payload=payload, data=data)
        except TransportError as error:
            raise ProviderError(error.status, retryable=error.status in {0, 429}
                                or error.status >= 500) from None

    async def inbound_file(self, description):
        metadata = await self.api(description["id"], method="GET")
        url = metadata["url"]
        host = urlsplit(url).hostname or ""
        if (urlsplit(url).scheme != "https"
                or not any(host.endswith("." + domain) for domain in ("facebook.com", "fbsbx.com"))):
            raise ProviderError(400, retryable=False)
        data = await self.http.download(url, allowed_hosts={host}, headers=self.headers)
        return File(description["name"], description["mime"], data)

    def parts(self, message):
        text = message.get("content") or ""
        items = choices(message)
        parts = [{"type": "text", "text": {"body": part, "preview_url": False}}
                 for part in chunks(text) if part]
        for offset in range(0, len(items), 10):
            group = items[offset:offset + 10]
            body = "Выберите вариант:"
            if offset == 0 and parts and len(parts[-1]["text"]["body"]) <= 1024:
                body = parts.pop()["text"]["body"]
            limit = 20 if len(group) <= 3 else 24
            labels = [str(item["title"]) for item in group]
            shortened = [label[:limit] for label in labels]
            if any(len(label) > limit for label in labels) or len(set(shortened)) != len(labels):
                # Preserve the complete wording and distinguish options whose
                # beginnings coincide after Meta's mandatory label truncation.
                menu = "\n".join(f"{i + 1}. {label}" for i, label in enumerate(labels))
                if len(body + "\n" + menu) <= 1024:
                    body += "\n" + menu
                else:
                    parts.extend({"type": "text", "text": {"body": part, "preview_url": False}}
                                 for part in chunks(body + "\n" + menu))
                    body = "Выберите вариант:"
                shortened = [f"{i + 1}. {label}"[:limit] for i, label in enumerate(labels)]
            interactive = {"body": {"text": body}}
            if len(group) <= 3:
                interactive.update({"type": "button", "action": {"buttons": [
                    {"type": "reply", "reply": {"id": str(item["value"]),
                                                 "title": label}}
                    for item, label in zip(group, shortened, strict=True)]}})
            else:
                interactive.update({"type": "list", "action": {
                    "button": "Выбрать", "sections": [{"rows": [
                        {"id": str(item["value"]), "title": label}
                        for item, label in zip(group, shortened, strict=True)]}]}})
            parts.append({"type": "interactive", "interactive": interactive})
        parts.extend({"attachment": a} for a in message.get("attachments") or [])
        return parts

    async def send(self, peer, part, *, key, file=None):
        timestamp = await self.redis.get(self.window_key(peer))
        if not timestamp or time.time() - int(timestamp) >= 86400:
            # Proactive follow-ups require a separately approved Meta template.
            raise ProviderError(131047, retryable=False)
        if file is not None:
            uploaded = await self.api(
                f"{self.settings.whatsapp_phone_id}/media",
                data=multipart(file, messaging_product="whatsapp"),
            )
            kind = file.mime.split("/", 1)[0]
            if kind not in {"image", "audio", "video"}:
                kind = "document"
            media = {"id": uploaded["id"]}
            if kind == "document":
                media["filename"] = file.name
            part = {"type": kind, kind: media}
        response = await self.api(f"{self.settings.whatsapp_phone_id}/messages", payload={
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": peer,
            **part,
        })
        messages = response.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise ProviderError(502)
        return str(messages[0]["id"])
