import aiohttp
import pytest
from aiohttp import web

from app.connectors.http import Http, ProviderError, TransportError


async def test_http_handles_chunked_json_and_sanitizes_error_body(client_factory):
    async def json_response(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(b'{"ok":')
        await response.write(b'true}')
        await response.write_eof()
        return response

    async def failure(_request):
        return web.Response(status=503, text="sensitive-provider-body")

    app = web.Application()
    app.router.add_get("/json", json_response)
    app.router.add_get("/failure", failure)
    client = await client_factory(app)
    async with aiohttp.ClientSession() as session:
        http = Http(session, max_bytes=16)
        assert await http.json("GET", str(client.make_url("/json"))) == {"ok": True}
        with pytest.raises(TransportError) as caught:
            await http.json("GET", str(client.make_url("/failure")))
        assert str(caught.value) == "transport_failed:503"


async def test_attachment_limits_and_http_origin_allowlist(client_factory):
    async def file(_request):
        return web.Response(body=b"%PDF-test-data")

    async def redirect(_request):
        return web.Response(status=302, headers={"Location": "http://127.0.0.2/private"})

    app = web.Application()
    app.router.add_get("/file", file)
    app.router.add_get("/redirect", redirect)
    client = await client_factory(app)
    origin = str(client.make_url("/")).rstrip("/")
    async with aiohttp.ClientSession() as session:
        http = Http(session, max_bytes=16)
        with pytest.raises(ProviderError):
            await http.download(origin + "/file", allowed_hosts={"127.0.0.1"})
        assert await http.download(origin + "/file", allowed_hosts={"127.0.0.1"},
                                   http_origin=origin) == b"%PDF-test-data"
        with pytest.raises(ProviderError):
            await http.download(origin + "/redirect", allowed_hosts={"127.0.0.1"},
                                http_origin=origin)
        http.max_bytes = 4
        with pytest.raises(ProviderError) as caught:
            await http.download(origin + "/file", allowed_hosts={"127.0.0.1"}, http_origin=origin)
        assert caught.value.status == 413
