"""Bounded HTTP and attachment IO with errors that never expose provider payloads."""

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp


class TransportError(RuntimeError):
    def __init__(self, status: int, *, retryable: bool = True):
        super().__init__(f"transport_failed:{status}")
        self.status, self.retryable = status, retryable


class ProviderError(TransportError):
    pass


@dataclass(frozen=True)
class File:
    name: str
    mime: str
    data: bytes


class Http:
    def __init__(self, session: aiohttp.ClientSession, *, max_bytes: int):
        self.session, self.max_bytes = session, max_bytes
        self.requests = asyncio.Semaphore(2)

    async def json(self, method, url, *, headers=None, payload=None, data=None):
        try:
            async with self.requests:
                return await self._json(method, url, headers=headers, payload=payload, data=data)
        except (aiohttp.ClientError, TimeoutError):
            raise TransportError(0) from None

    async def _json(self, method, url, *, headers=None, payload=None, data=None):
        async with self.session.request(
            method, url, headers=headers, json=payload, data=data, allow_redirects=False,
        ) as response:
            if not 200 <= response.status < 300:
                raise TransportError(response.status)
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise TransportError(502)
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError):
                raise TransportError(502) from None

    async def download(self, url, *, allowed_hosts, headers=None, http_origin=""):
        insecure = urlsplit(http_origin)
        for _ in range(5):
            parsed = urlsplit(url)
            if (parsed.scheme not in {"https", "http"} or parsed.username or parsed.password
                    or parsed.hostname not in allowed_hosts
                    or parsed.scheme == "https" and parsed.port not in {None, 443}
                    or parsed.scheme == "http" and (
                        insecure.scheme != "http" or parsed.netloc != insecure.netloc)):
                raise ProviderError(400, retryable=False)
            async with self.session.get(
                url, headers=headers, allow_redirects=False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    next_url = urljoin(url, response.headers.get("Location", ""))
                    # Never forward a bearer token through an attachment redirect.
                    if urlsplit(next_url).netloc != parsed.netloc:
                        headers = None
                    if parsed.scheme == "https" and urlsplit(next_url).scheme != "https":
                        raise ProviderError(400, retryable=False)
                    url = next_url
                    continue
                if response.status != 200:
                    raise TransportError(response.status)
                if response.content_length and response.content_length > self.max_bytes:
                    raise ProviderError(413, retryable=False)
                result = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    result.extend(chunk)
                    if len(result) > self.max_bytes:
                        raise ProviderError(413, retryable=False)
                return bytes(result)
        raise TransportError(502)


def multipart(file: File, **fields):
    form = aiohttp.FormData()
    for key, value in fields.items():
        form.add_field(key, value)
    form.add_field("file", file.data, filename=file.name, content_type=file.mime)
    return form


def chunks(text: str, limit: int = 4000):
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


def choices(message):
    attributes = message.get("content_attributes") or {}
    items = attributes.get("items") if isinstance(attributes, dict) else None
    if items is None:
        return []
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("title"), str)
        or not item["title"].strip() or not isinstance(item.get("value"), str)
        or not 1 <= len(item["value"]) <= 200 for item in items
    ):
        raise ProviderError(400, retryable=False)
    return items
