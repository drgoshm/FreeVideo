"""Real HTTPS/HTTP/WebSocket checks and forged-proxy regression tests (no GPU)."""
import asyncio
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, CookieJar, WSMsgType, web
from aiohttp.test_utils import TestServer, make_mocked_request
from freevideo_engine import proxy_identity
from freevideo_engine.access_proxy import forwarded
from freevideo_engine.agent import access
from freevideo_engine.agent.jobs import JobError
from freevideo_engine.agent.lan_guide import remote_guide
from freevideo_engine.lan_gateway import COOKIE, Sessions, create, private_peer
from setup_lan_access import prepare

KEY, HOP = 'test-key-' + 'a' * 40, 'test-hop-' + 'b' * 40


class Authorization(unittest.TestCase):
    def request(self, secret=HOP, auth='browser', scheme='https', peer='192.168.1.20'):
        headers = {'Host': '192.168.1.10:8443', proxy_identity.HEADER: secret,
                   proxy_identity.PEER: peer, proxy_identity.ORIGIN: scheme + '://192.168.1.10:8443',
                   proxy_identity.LAN_AUTH: auth}
        app = web.Application()
        app[proxy_identity.app_key()] = HOP
        req = make_mocked_request('GET', '/freevideo/agent', headers=headers, app=app)
        req.transport.get_extra_info.side_effect = lambda name: ('127.0.0.1', 8188)
        return req

    def test_only_authenticated_https_proxy_can_delegate_local_controls(self):
        with patch.object(access, 'lan_enabled', return_value=False):
            access.check(self.request(), local=True)
            access.check(self.request(auth='agent'), local=True)
            for req in (self.request(secret='forged'), self.request(auth=''), self.request(scheme='http'),
                        self.request(peer='8.8.8.8'), self.request(peer='169.254.1.1')):
                with self.assertRaises(JobError):
                    access.check(req, local=True)

    def test_legacy_proxy_strips_new_proof(self):
        req = self.request()
        self.assertNotIn(proxy_identity.LAN_AUTH.lower(), {name.lower() for name, _ in forwarded(req)})

    def test_private_ranges_and_session_expiry(self):
        for value in ('192.168.1.2', '10.2.3.4', '172.16.5.1', '127.0.0.1', '::1', 'fd12::1'):
            self.assertTrue(private_peer(value))
        for value in ('0.0.0.0', '169.254.1.1', '8.8.8.8', '224.0.0.1', '100.64.1.1', 'bad'):
            self.assertFalse(private_peer(value))
        now = [0]
        sessions = Sessions(lambda: now[0])
        key = sessions.issue('10.0.0.2', 'server:8443')
        self.assertTrue(sessions.get(key, '10.0.0.2', 'server:8443'))
        self.assertFalse(sessions.get(key, '10.0.0.3', 'server:8443'))
        key = sessions.issue('10.0.0.2', 'server:8443')
        now[0] = 43201
        self.assertIsNone(sessions.get(key, '10.0.0.2', 'server:8443'))

    def test_no_server_private_paths_in_remote_transport_instructions(self):
        value = remote_guide(dict(api='https://server:8443/freevideo/agent/v1', token_file='SECRET_SERVER_PATH',
                                 command=['SECRET_SERVER_PATH'], claude_code='SECRET_SERVER_PATH',
                                 codex='SECRET_SERVER_PATH', mcp_json={'path': 'SECRET_SERVER_PATH'}))
        self.assertNotIn('SECRET_SERVER_PATH', json.dumps(value))
        self.assertIn('ssl.create_default_context', value['http_example'])

    def test_agent_client_refuses_plaintext_remote_credentials(self):
        from freevideo_engine.agent.client import Client
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            Client('http://192.168.1.10:8443', token=KEY)


class HTTPSGateway(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        prepare(cls.root, ['127.0.0.1', 'localhost'])
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cls.root / 'lan-access/server.pem', cls.root / 'lan-access/server-key.pem')
        cls.trust = ssl.create_default_context(cafile=cls.root / 'lan-access/FreeVideo-LAN-CA.crt')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    async def asyncSetUp(self):
        async def backend(request):
            access.check(request, local=True)
            if request.path == '/ws':
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        await ws.send_str(msg.data)
                return ws
            if request.path == '/bytes':
                return web.Response(status=206, body=b'abc', headers={'Content-Range': 'bytes 0-2/10'})
            return web.json_response(dict(headers=dict(request.headers), body=await request.text(),
                                          enabled=True, path=request.path), headers={'Set-Cookie': 'page=test; HttpOnly; SameSite=Strict; Path=/'})
        app = web.Application()
        app[proxy_identity.app_key()] = HOP
        app.router.add_route('*', '/{path:.*}', backend)
        self.backend = TestServer(app)
        await self.backend.start_server()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.sessions = Sessions()
        self.gateway = TestServer(create(str(self.backend.make_url('')).rstrip('/'), KEY,
                                  hosts=['127.0.0.1'], port=port, hop_secret=HOP, sessions=self.sessions), port=port, scheme='https')
        await self.gateway.start_server(ssl=self.context)
        self.base = str(self.gateway.make_url('')).rstrip('/')
        self.client = ClientSession(cookie_jar=CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.gateway.close)
        self.addAsyncCleanup(self.backend.close)

    async def call(self, method, path, **kwargs):
        return await self.client.request(method, self.base + path, ssl=self.trust, allow_redirects=False, **kwargs)

    async def login(self):
        reply = await self.call('POST', '/_lan/login', data={'key': KEY}, headers={'Origin': self.base})
        self.assertEqual(reply.status, 303)
        self.assertTrue(reply.cookies[COOKIE]['secure'])
        self.assertTrue(reply.cookies[COOKIE]['httponly'])
        self.assertEqual(reply.cookies[COOKIE]['samesite'], 'Strict')
        self.assertNotEqual(reply.cookies[COOKIE].value, KEY)
        return reply.cookies[COOKIE].value

    async def test_all_backend_paths_require_login(self):
        for path in ('/', '/freevideo/agent', '/api/freevideo/agent', '/system_stats', '/ws'):
            reply = await self.call('GET', path)
            self.assertEqual(reply.status, 401, path)
        reply = await self.call('POST', '/prompt', json={}, headers={'Origin': self.base})
        self.assertEqual(reply.status, 401)

    async def test_browser_session_and_csrf(self):
        cookie = await self.login()
        reply = await self.call('GET', '/api/freevideo/agent')
        self.assertEqual(reply.status, 200)
        body = await reply.json()
        self.assertEqual(body['headers'][proxy_identity.LAN_AUTH], 'browser')
        self.assertNotIn(cookie, body['headers'].get('Cookie', ''))
        self.assertIn('Secure', reply.headers['Set-Cookie'])
        for headers in ({}, {'Origin': 'https://evil.invalid'}, {'Origin': self.base, 'Sec-Fetch-Site': 'cross-site'}):
            reply = await self.call('POST', '/freevideo/agent/chat/new', json={}, headers=headers)
            self.assertEqual(reply.status, 403)
        reply = await self.call('POST', '/freevideo/agent/chat/new', json={},
                                headers={'Origin': self.base, 'X-FreeVideo-CSRF': 'existing-csrf'})
        self.assertEqual(reply.status, 200)
        self.assertEqual((await reply.json())['headers']['X-FreeVideo-CSRF'], 'existing-csrf')

    async def test_native_login_and_logout_form_policy(self):
        # A native POST form under no-referrer sends Origin: null in browsers.
        # Check the document policy as well as the subsequent HTTP exchange;
        # HTTP clients do not apply browser referrer policies themselves.
        reply = await self.call('GET', '/')
        self.assertEqual(reply.headers['Referrer-Policy'], 'same-origin')
        self.assertIn('action="/_lan/login"', await reply.text())
        for origin in ('null', 'https://evil.invalid'):
            reply = await self.call('POST', '/_lan/login', data={'key': KEY}, headers={'Origin': origin})
            self.assertEqual(reply.status, 403)
        reply = await self.call('POST', '/_lan/login', data={'key': KEY},
                                headers={'Origin': self.base, 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(reply.status, 303)
        self.assertEqual(reply.headers['Referrer-Policy'], 'same-origin')
        reply = await self.call('GET', reply.headers['Location'])
        self.assertEqual(reply.status, 200)
        reply = await self.call('GET', '/_lan/account')
        self.assertEqual(reply.headers['Referrer-Policy'], 'same-origin')
        self.assertIn('action="/_lan/logout"', await reply.text())
        reply = await self.call('POST', '/_lan/logout', headers={'Origin': self.base})
        self.assertEqual(reply.status, 303)
        reply = await self.call('GET', '/')
        self.assertEqual(reply.status, 401)

    async def test_bearer_not_forwarded_and_spoofed_proof_replaced(self):
        reply = await self.call('GET', '/freevideo/agent/v1', headers={'Authorization': 'Bearer ' + KEY,
            proxy_identity.HEADER: 'forged', proxy_identity.LAN_AUTH: 'browser', 'X-FreeVideo-Agent': 'forged',
            'X-Forwarded-For': '8.8.8.8'})
        self.assertEqual(reply.status, 200)
        headers = (await reply.json())['headers']
        self.assertEqual(headers[proxy_identity.HEADER], HOP)
        self.assertEqual(headers[proxy_identity.LAN_AUTH], 'agent')
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('X-FreeVideo-Agent', headers)
        self.assertNotIn('X-Forwarded-For', headers)
        self.assertNotIn(KEY, json.dumps(headers))

    async def test_host_origin_and_url_credentials_rejected(self):
        await self.login()
        for path, headers, expected in (('/freevideo/agent', {'Origin': 'https://evil.invalid'}, 403),
                ('/freevideo/agent', {'Host': 'evil.invalid'}, 403), ('/?token=' + KEY, {}, 400)):
            reply = await self.call('GET', path, headers=headers)
            self.assertEqual(reply.status, expected)

    async def test_logout_revokes_cookie(self):
        cookie = await self.login()
        reply = await self.call('POST', '/_lan/logout', headers={'Origin': self.base})
        self.assertEqual(reply.status, 303)
        reply = await self.call('GET', '/freevideo/agent', headers={'Cookie': COOKIE + '=' + cookie})
        self.assertEqual(reply.status, 401)

    async def test_failed_login_throttled(self):
        for _ in range(10):
            reply = await self.call('POST', '/_lan/login', data={'key': 'wrong'}, headers={'Origin': self.base})
            self.assertEqual(reply.status, 401)
        reply = await self.call('POST', '/_lan/login', data={'key': KEY}, headers={'Origin': self.base})
        self.assertEqual(reply.status, 429)

    async def test_websocket_and_media_stream(self):
        await self.login()
        async with self.client.ws_connect(self.base + '/ws', ssl=self.trust, headers={'Origin': self.base}) as ws:
            await ws.send_str('progress')
            self.assertEqual((await ws.receive(timeout=2)).data, 'progress')
            await self.call('POST', '/_lan/logout', headers={'Origin': self.base})
            self.assertIn((await ws.receive(timeout=3)).type, (WSMsgType.CLOSE, WSMsgType.CLOSED))
        reply = await self.call('GET', '/bytes', headers={'Authorization': 'Bearer ' + KEY, 'Range': 'bytes=0-2'})
        self.assertEqual(reply.status, 206)
        self.assertEqual(await reply.read(), b'abc')
        self.assertEqual(reply.headers['Content-Range'], 'bytes 0-2/10')

    async def test_untrusted_certificate_fails(self):
        from aiohttp import ClientConnectorCertificateError
        with self.assertRaises(ClientConnectorCertificateError):
            await self.client.get(self.base, ssl=ssl.create_default_context())


if __name__ == '__main__':
    unittest.main()
