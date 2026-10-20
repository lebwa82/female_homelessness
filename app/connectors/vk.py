"""VK community Callback API adapter; personal chats only."""

import hashlib
import hmac
import json
from urllib.parse import urlsplit

from app.connectors.http import File, ProviderError, TransportError, choices, chunks, multipart


class VK:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def authenticate(self, _raw, _headers, payload):
        return (isinstance(payload, dict)
                and type(payload.get("group_id")) is int
                and payload["group_id"] == self.settings.vk_group_id
                and isinstance(payload.get("secret"), str)
                and hmac.compare_digest(payload["secret"].encode(),
                                        self.settings.vk_secret.get_secret_value().encode()))

    def incoming(self, payload):
        if payload.get("type") != "message_new":
            return []
        message = (payload.get("object") or {}).get("message") or {}
        peer, sender, identity = (message.get(key) for key in ("peer_id", "from_id", "id"))
        if (type(peer) is not int or not 0 < peer < 2_000_000_000
                or sender != peer or message.get("out", 0) != 0
                or type(identity) is not int or identity <= 0):
            return []
        text = message.get("text") or ""
        action = message.get("payload")
        if isinstance(action, str):
            try:
                action = json.loads(action)
            except ValueError:
                action = None
        if isinstance(action, dict) and isinstance(action.get("action"), str):
            text = action["action"][:256]
        files = []
        for attachment in message.get("attachments") or []:
            kind = attachment.get("type")
            if kind == "doc":
                doc = attachment.get("doc") or {}
                if isinstance(doc.get("url"), str):
                    files.append({"url": doc["url"], "name": doc.get("title") or "document",
                                  "mime": "application/octet-stream"})
            elif kind == "photo":
                sizes = (attachment.get("photo") or {}).get("sizes") or []
                sizes = [s for s in sizes if isinstance(s.get("url"), str)]
                if sizes:
                    photo = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                    files.append({"url": photo["url"], "name": "photo.jpg", "mime": "image/jpeg"})
        if not text and message.get("attachments"):
            text = "Входящее вложение ВКонтакте."
        if not isinstance(text, str) or not text.strip():
            return []
        return [{"id": str(identity), "peer": str(peer), "text": text, "files": files}]

    async def api(self, method, **payload):
        try:
            response = await self.http.json(
                "POST", f"https://api.vk.com/method/{method}", data={
                    "access_token": self.settings.vk_token.get_secret_value(),
                    "v": self.settings.vk_api_version, **payload,
                },
            )
        except TransportError as error:
            raise ProviderError(error.status, retryable=error.status in {0, 429}
                                or error.status >= 500) from None
        if "error" in response:
            code = int(response["error"].get("error_code", 0))
            raise ProviderError(code, retryable=code in {1, 6, 9, 10, 29})
        if "response" not in response:
            raise ProviderError(502)
        return response["response"]

    @staticmethod
    def media_host(url):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.port not in {None, 443}
                or not any(host.endswith("." + suffix) for suffix in
                           ("vk.com", "vkuser.net", "vk-cdn.net", "userapi.com"))):
            raise ProviderError(400, retryable=False)
        return host

    async def inbound_file(self, description):
        url = description["url"]
        host = self.media_host(url)
        try:
            data = await self.http.download(url, allowed_hosts={host})
        except TransportError as error:
            if 400 <= error.status < 500 and error.status != 429:
                raise ProviderError(error.status, retryable=False) from None
            raise
        return File(description["name"], description["mime"], data)

    def parts(self, message):
        parts = [{"text": text} for text in chunks(message.get("content") or "") if text]
        items = choices(message)
        # Keep inline keyboards within six rows. Each group remains a separate
        # resumable send with a stable VK random_id.
        for offset in range(0, len(items), 6):
            buttons = [[{"action": {"type": "text", "label": str(item["title"])[:40],
                                    "payload": json.dumps({"action": str(item["value"])},
                                                          ensure_ascii=False)},
                         "color": "secondary"}] for item in items[offset:offset + 6]]
            keyboard = {"one_time": False, "inline": True, "buttons": buttons}
            if offset == 0 and parts:
                parts[-1]["keyboard"] = keyboard
            else:
                parts.append({"text": "Выберите вариант:", "keyboard": keyboard})
        parts.extend({"attachment": a} for a in message.get("attachments") or [])
        return parts

    async def send(self, peer, part, *, key, file=None):
        payload = {"peer_id": peer,
                   "random_id": int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")
                   % 2_147_483_647 or 1}
        if "text" in part:
            payload["message"] = part["text"]
        if "keyboard" in part:
            payload["keyboard"] = json.dumps(part["keyboard"], ensure_ascii=False)
        if file is not None:
            server = await self.api("docs.getMessagesUploadServer", peer_id=peer, type="doc")
            url = server["upload_url"]
            self.media_host(url)
            uploaded = await self.http.json("POST", url, data=multipart(file))
            saved = await self.api("docs.save", file=uploaded["file"], title=file.name)
            doc = saved["doc"]
            payload["attachment"] = f"doc{doc['owner_id']}_{doc['id']}"
            if doc.get("access_key"):
                payload["attachment"] += f"_{doc['access_key']}"
        identity = await self.api("messages.send", **payload)
        if type(identity) is not int or identity <= 0:
            raise ProviderError(502)
        return str(identity)
