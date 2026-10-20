import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
import uuid

from aiohttp import ClientSession, web


async def smoke(provider):
    received = []
    event_id = uuid.uuid4().int % 2_000_000_000 + 1
    secret = 'x' * 40
    scope = 'vk:3' if provider == 'vk' else 'whatsapp:1234'
    peer = '42' if provider == 'vk' else '79990000001'
    source = scope + ':' + peer

    async def inbox(_request):
        return web.json_response({'channel_type': 'Channel::Api', 'inbox_identifier': 'api',
                                  'hmac_mandatory': True,
                                  'webhook_url': 'https://example.test/webhooks/chatwoot/' + secret})

    async def contact(request):
        body = await request.json()
        assert body['source_id'] == source
        assert body['identifier_hash'] == hmac.new(b'hmac', source.encode(), hashlib.sha256).hexdigest()
        return web.json_response({'id': 9})

    async def conversations(_request):
        return web.json_response({'payload': [{'id': 7, 'inbox_id': 2}]})

    async def messages(request):
        if request.method == 'POST':
            received.append(await request.json())
            return web.json_response({'id': 11})
        return web.json_response({'payload': []})

    app = web.Application()
    app.router.add_get('/api/v1/accounts/1/inboxes/2', inbox)
    app.router.add_post('/public/api/v1/inboxes/api/contacts', contact)
    app.router.add_get('/api/v1/accounts/1/contacts/9/conversations', conversations)
    app.router.add_route('*', '/api/v1/accounts/1/conversations/7/messages', messages)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 18000).start()
    env = dict(os.environ)
    env.update({
        'CONNECTOR_PROVIDER': provider, 'CONNECTOR_CHATWOOT_URL': 'http://127.0.0.1:18000',
        'CONNECTOR_ACCOUNT_ID': '1', 'CONNECTOR_INBOX_ID': '2', 'CONNECTOR_API_TOKEN': 'test',
        'CONNECTOR_INBOX_IDENTIFIER': 'api', 'CONNECTOR_INBOX_HMAC_TOKEN': 'hmac',
        'CONNECTOR_CALLBACK_SECRET': secret, 'CONNECTOR_REDIS_URL': 'unix:///run/redis/redis.sock',
        'CONNECTOR_VK_GROUP_ID': '3', 'CONNECTOR_VK_TOKEN': 'test', 'CONNECTOR_VK_SECRET': 'test',
        'CONNECTOR_VK_CONFIRMATION': 'test', 'CONNECTOR_WHATSAPP_PHONE_ID': '1234',
        'CONNECTOR_WHATSAPP_BUSINESS_ID': '5678', 'CONNECTOR_WHATSAPP_TOKEN': 'test',
        'CONNECTOR_WHATSAPP_APP_SECRET': 'test', 'CONNECTOR_WHATSAPP_VERIFY_TOKEN': 'test',
        'CONNECTOR_WHATSAPP_API_VERSION': 'v25.0',
    })
    process = await asyncio.create_subprocess_exec('python', '-m', 'app.connectors', env=env,
                                                  stdout=asyncio.subprocess.DEVNULL,
                                                  stderr=asyncio.subprocess.PIPE)
    try:
        async with ClientSession() as client:
            for _ in range(100):
                if process.returncode is not None:
                    raise AssertionError('connector exited before readiness')
                try:
                    async with client.get('http://127.0.0.1:8090/healthz') as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                await asyncio.sleep(.05)
            else:
                raise AssertionError('connector readiness timeout')
            if provider == 'vk':
                payload = {'type': 'message_new', 'group_id': 3, 'secret': 'test', 'object': {
                    'message': {'id': event_id, 'peer_id': 42, 'from_id': 42,
                                'text': 'Synthetic container input', 'out': 0}}}
                headers = {}
            else:
                payload = {'object': 'whatsapp_business_account', 'entry': [{'id': '5678',
                    'changes': [{'field': 'messages', 'value': {
                        'metadata': {'phone_number_id': '1234'}, 'messages': [{
                            'from': peer, 'id': 'wamid.container-' + str(event_id), 'type': 'text',
                            'text': {'body': 'Synthetic container input'},
                            'timestamp': str(int(time.time()))}]}}]}]}
                raw = json.dumps(payload).encode()
                headers = {'X-Hub-Signature-256': 'sha256=' + hmac.new(
                    b'test', raw, hashlib.sha256).hexdigest()}
            raw = json.dumps(payload).encode()
            async with client.post('http://127.0.0.1:8090/webhooks/' + provider,
                                   data=raw, headers=headers) as response:
                assert response.status == 200
            for _ in range(100):
                if received:
                    break
                await asyncio.sleep(.05)
            assert received and received[0]['message_type'] == 'incoming'
            assert received[0]['content'] == 'Synthetic container input'
            assert received[0]['source_id'].startswith(source + ':')
            print(provider + ': container startup, signed ingress, real Redis, Chatwoot HTTP passed', flush=True)
    finally:
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 10)
        await runner.cleanup()


async def main():
    for package in ('aiogram', 'openai', 'sqlalchemy', 'app.chatwoot'):
        assert importlib.util.find_spec(package) is None
    if len(sys.argv) != 2 or sys.argv[1] not in ('vk', 'whatsapp'):
        raise SystemExit('usage: python - vk|whatsapp < tests/connectors/container_smoke.py')
    await smoke(sys.argv[1])


if __name__ == '__main__':
    asyncio.run(main())
