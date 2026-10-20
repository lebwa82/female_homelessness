"""Transport receipts and retry boundaries; canonical conversations stay in Chatwoot."""

import hashlib
import json

from app.connectors.http import ProviderError


class ConnectorService:
    def __init__(self, settings, redis, chatwoot, provider):
        self.settings, self.redis = settings, redis
        self.chatwoot, self.provider = chatwoot, provider

    def key(self, value):
        digest = hashlib.sha256(value.encode()).hexdigest()
        return f"{self.settings.scope}:receipt:{digest}"

    async def remember(self, key, value="1"):
        await self.redis.set(key, value, ex=self.settings.receipt_ttl_seconds)

    async def process(self, event):
        if event["kind"] == "incoming":
            await self.incoming(event)
        elif event["kind"] == "status":
            await self.status(event)
        else:
            await self.outgoing(event)

    async def incoming(self, event):
        key = self.key(f"incoming:{event['peer']}:{event['id']}")
        if await self.redis.get(key):
            return
        if self.settings.provider == "whatsapp":
            await self.provider.received(event)
        conversation = await self.chatwoot.conversation(event["peer"])
        files = []
        total = 0
        for description in event.get("files", []):
            try:
                file = await self.provider.inbound_file(description)
                total += len(file.data)
                if total > self.settings.max_attachment_bytes:
                    raise ProviderError(413, retryable=False)
                files.append(file)
            except ProviderError as error:
                if error.retryable:
                    raise
                # Keep the text and make a rejected media upload visible to staff.
                event = {**event, "text": event["text"] + "\n[Вложение недоступно коннектору.]"}
                break
        await self.chatwoot.incoming(conversation, event, files)
        await self.remember(key)

    async def outgoing(self, event):
        key = self.key(f"outgoing:{event['conversation_id']}:{event['message_id']}")
        if await self.redis.get(key):
            return
        canonical = await self.chatwoot.outgoing(event)
        if canonical is None:
            return
        peer, message = canonical
        receipts = []
        try:
            for index, part in enumerate(self.provider.parts(message)):
                step = f"{key}:{index}"
                identity = await self.redis.get(step)
                if not identity:
                    file = (await self.chatwoot.attachment(part["attachment"])
                            if "attachment" in part else None)
                    identity = await self.provider.send(peer, part, key=step, file=file)
                    await self.remember(step, identity)
                if self.settings.provider == "whatsapp":
                    await self.remember(self.key("mapping:" + identity), json.dumps(event))
                receipts.append(identity)
        except ProviderError as error:
            if error.retryable:
                raise
            await self.chatwoot.failed(event["conversation_id"], event["message_id"], error.status)
            await self.remember(key, "failed")
            return
        if not receipts:
            return
        await self.chatwoot.receipt(event["conversation_id"], event["message_id"], receipts)
        await self.remember(key, json.dumps(receipts))
        if self.settings.provider == "whatsapp":
            await self.publish_status(event)

    async def status(self, event):
        key = self.key("status:" + event["id"])
        # Meta may redeliver statuses out of order; never regress read/delivered.
        await self.redis.eval(
            "local rank={sent=0,delivered=1,read=2,failed=3}; "
            "local old=redis.call('GET',KEYS[1]); "
            "if not old or rank[ARGV[1]]>rank[old] then "
            "redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]); end",
            1, key, event["status"], self.settings.receipt_ttl_seconds,
        )
        mapping = await self.redis.get(self.key("mapping:" + event["id"]))
        if mapping:
            await self.publish_status(json.loads(mapping))

    async def publish_status(self, event):
        lock_key = self.key(f"status-lock:{event['conversation_id']}:{event['message_id']}")
        async with self.redis.lock(lock_key, timeout=90, blocking_timeout=1):
            await self._publish_status(event)

    async def _publish_status(self, event):
        key = self.key(f"outgoing:{event['conversation_id']}:{event['message_id']}")
        saved = await self.redis.get(key)
        if not saved or saved == "failed":
            return
        identities = json.loads(saved)
        statuses = [await self.redis.get(self.key("status:" + identity)) for identity in identities]
        # All parts must reach the recipient before claiming delivery. A single
        # failed part fails the whole Chatwoot message, including a PDF upload.
        if "failed" in statuses:
            await self.chatwoot.failed(event["conversation_id"], event["message_id"], "async")
        elif statuses and all(status == "read" for status in statuses):
            await self.chatwoot.delivery_status(event["conversation_id"], event["message_id"], "read")
        elif statuses and all(status in {"delivered", "read"} for status in statuses):
            await self.chatwoot.delivery_status(event["conversation_id"], event["message_id"], "delivered")
