"""HTTPS-only authenticated LAN entry point; the ComfyUI backend stays on loopback.

Browser sessions and remote agent keys are independent of the engine's bearer
token. Only this gateway can attest LAN operator access using an ephemeral
secret shared with its local backend. No credentials are accepted in URLs.
"""
import argparse
import asyncio
from collections import OrderedDict
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
from yarl import URL

from . import proxy_identity
from .access_proxy import HOP_BY_HOP

COOKIE = '__Host-freevideo_lan'
TTL = 12 * 3600
NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', 'fc00::/7', '::1/128'))
LOGIN = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FreeVideo — вход</title>
<style>body{background:#10151e;color:#eef;font:17px system-ui;display:grid;place-items:center;min-height:95vh}
main{max-width:440px;padding:32px;border:1px solid #405065;border-radius:16px}input,button{box-sizing:border-box;
width:100%;padding:12px;margin-top:16px;font:inherit}a{color:#9cf}p{line-height:1.5}</style>
<main><h1>FreeVideo</h1><p>Введите ключ доступа, полученный от владельца компьютера с FreeVideo.</p>
<form method="post" action="/_lan/login"><label>Ключ доступа<input name="key" type="password" required
maxlength="128" autocomplete="current-password"></label><button>Войти</button></form>
<p>Подключение защищено HTTPS. Сессия действует 12 часов.</p></main></html>'''
ACCOUNT = '''<!doctype html><html lang="ru"><meta charset="utf-8"><title>FreeVideo — доступ</title>
<h1>Доступ к FreeVideo</h1><p>Сессия действует до 12 часов или перезапуска сервера.</p>
<p><a href="/">Открыть FreeVideo</a> · <a href="/freevideo/agent/v1/connect">Инструкция для агента</a></p>
<form method="post" action="/_lan/logout"><button>Выйти на этом устройстве</button></form></html>'''


def same(value, expected):
    return isinstance(value, str) and hmac.compare_digest(value.encode(), expected.encode())


def private_peer(value):
    try:
        address = ipaddress.ip_address(value.split('%')[0])
        address = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
        return any(address in network for network in NETWORKS)
    except (AttributeError, ValueError):
        return False


class Sessions:
    def __init__(self, clock=time.monotonic):
        self.clock, self.values, self.failures = clock, {}, OrderedDict()

    def get(self, cookie, peer, host):
        row = self.values.get(cookie)
        if row and row[0] > self.clock() and row[1:] == (peer, host):
            return row[0]
        self.values.pop(cookie, None)
        return None

    def issue(self, peer, host):
        self.values = {k: v for k, v in self.values.items() if v[0] > self.clock()}
        if len(self.values) >= 256:
            raise web.HTTPServiceUnavailable(text='Too many active sessions')
        key = secrets.token_urlsafe(32)
        self.values[key] = (self.clock() + TTL, peer, host)
        return key

    def limited(self, peer):
        now = self.clock()
        count, deadline = self.failures.get(peer, (0, now + 60))
        if deadline <= now:
            count, deadline = 0, now + 60
        self.failures[peer] = (count + 1, deadline)
        self.failures.move_to_end(peer)
        while len(self.failures) > 1024:
            self.failures.popitem(last=False)
        return count >= 10


def create(upstream, key, *, hosts, port, hop_secret, sessions=None):
    parsed = urlsplit(upstream)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.path or parsed.query or parsed.fragment or parsed.username):
        raise ValueError('Backend must be http://127.0.0.1:PORT')
    if len(key) < 32 or len(hop_secret) < 32:
        raise ValueError('Private LAN key and ephemeral hop secret are required')
    hosts = {host.lower() for host in hosts}
    sessions = sessions or Sessions()
    client = []

    async def startup(app):
        client.append(ClientSession(auto_decompress=False, cookie_jar=DummyCookieJar(),
                                    timeout=ClientTimeout(total=None, sock_connect=10)))

    async def cleanup(app):
        await client[0].close()

    async def security_headers(request, response):
        response.headers['Cache-Control'] = 'no-store'
        # Native form POSTs can send Origin: null under no-referrer. Preserve
        # same-origin form authentication without disclosing referrers off-site.
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        for name in list(response.headers):
            if name.lower().startswith('access-control-'):
                del response.headers[name]

    def headers(request, mode, peer):
        result = []
        connection = {v.strip().lower() for v in request.headers.get('Connection', '').split(',')}
        for name, value in request.headers.items():
            lower = name.lower()
            if (lower in HOP_BY_HOP | connection or lower in ('authorization', 'x-freevideo-agent', 'forwarded')
                    or lower.startswith(('x-forwarded-', 'x-freevideo-proxy-'))):
                continue
            if lower == 'cookie':
                value = '; '.join(part for part in value.split(';') if part.split('=', 1)[0].strip() != COOKIE)
            if value:
                result.append((name, value))
        result.extend(((proxy_identity.HEADER, hop_secret), (proxy_identity.PEER, peer),
                       (proxy_identity.ORIGIN, 'https://' + request.host), (proxy_identity.LAN_AUTH, mode)))
        return result

    async def forward(request, mode, peer, expires):
        target = URL(upstream + request.raw_path, encoded=True)
        forwarded = headers(request, mode, peer)
        try:
            if request.headers.get('Upgrade', '').lower() == 'websocket':
                forwarded = [(k, v) for k, v in forwarded if not k.lower().startswith('sec-websocket-')]
                async with client[0].ws_connect(target.with_scheme('ws'), headers=forwarded, max_msg_size=32 << 20) as backend:
                    browser = web.WebSocketResponse(max_msg_size=32 << 20)
                    await browser.prepare(request)
                    async def pump(source, destination):
                        async for item in source:
                            if item.type == WSMsgType.TEXT:
                                await destination.send_str(item.data)
                            elif item.type == WSMsgType.BINARY:
                                await destination.send_bytes(item.data)
                            else:
                                break
                    tasks = [asyncio.create_task(pump(browser, backend)), asyncio.create_task(pump(backend, browser))]
                    if mode == 'browser':
                        async def watch_session():
                            while sessions.get(request.cookies.get(COOKIE, ''), peer, request.host):
                                await asyncio.sleep(1)
                        tasks.append(asyncio.create_task(watch_session()))
                    try:
                        await asyncio.wait(tasks, timeout=max(0, expires - sessions.clock()), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await browser.close()
                    return browser
            async with client[0].request(request.method, target, headers=forwarded,
                                         data=request.content if request.body_exists else None, allow_redirects=False) as backend:
                response = web.StreamResponse(status=backend.status)
                for name, value in backend.headers.items():
                    if name.lower() not in HOP_BY_HOP:
                        if name.lower() == 'set-cookie' and 'secure' not in (part.strip().lower() for part in value.split(';')):
                            value += '; Secure'
                        response.headers.add(name, value)
                await response.prepare(request)
                async for chunk in backend.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (OSError, asyncio.TimeoutError):
            raise web.HTTPBadGateway(text='FreeVideo backend is not ready') from None

    async def handle(request):
        peer = request.remote
        if not request.secure or not private_peer(peer):
            raise web.HTTPForbidden(text='HTTPS from the local network is required')
        try:
            host = urlsplit('https://' + request.host)
            if (host.hostname not in hosts or (host.port or 443) != port or host.username or host.password
                    or host.path or host.query or host.fragment):
                raise ValueError
        except ValueError:
            raise web.HTTPForbidden(text='Unrecognized Host') from None
        expected_origin = 'https://' + request.host
        origins = request.headers.getall('Origin', [])
        if origins and origins != [expected_origin]:
            raise web.HTTPForbidden(text='Cross-origin request refused')
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise web.HTTPForbidden(text='Cross-site request refused')
        # Never redeem credentials in a URL, where history and access logs retain them.
        if any(name in request.query for name in ('token', 'key', 'access_token')):
            raise web.HTTPBadRequest(text='Send credentials in a POST body or Authorization header')
        auth = request.headers.get('Authorization', '')
        bearer = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
        cookie = request.cookies.get(COOKIE, '')
        expires = sessions.get(cookie, peer, request.host)
        mode = 'browser'
        if auth:
            if not same(bearer, key):
                if sessions.limited(peer):
                    raise web.HTTPTooManyRequests(headers={'Retry-After': '60'})
                raise web.HTTPUnauthorized(text='Invalid LAN access key')
            mode, expires = 'agent', sessions.clock() + TTL
        if ((request.method not in ('GET', 'HEAD') or request.headers.get('Upgrade', '').lower() == 'websocket')
                and mode != 'agent' and origins != [expected_origin]):
            raise web.HTTPForbidden(text='Same-origin browser request required')
        if request.path == '/_lan/login' and request.method == 'POST':
            if sessions.limited(peer):
                raise web.HTTPTooManyRequests(headers={'Retry-After': '60'})
            if request.content_type != 'application/x-www-form-urlencoded':
                raise web.HTTPUnsupportedMediaType()
            if request.content_length is None or request.content_length > 4096:
                raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length or 4097)
            fields = await request.post()
            if not same(fields.get('key', ''), key):
                return web.Response(status=401, text=LOGIN, content_type='text/html')
            sessions.values.pop(cookie, None)
            cookie = sessions.issue(peer, request.host)
            response = web.Response(status=303, headers={'Location': '/'})
            response.set_cookie(COOKIE, cookie, secure=True, httponly=True, samesite='Strict', path='/', max_age=TTL)
            return response
        if not expires:
            if request.method == 'GET' and (request.path in ('/', '/_lan/login') or 'text/html' in request.headers.get('Accept', '')):
                return web.Response(status=401, text=LOGIN, content_type='text/html')
            raise web.HTTPUnauthorized(text='Log in to FreeVideo over HTTPS first')
        if request.path == '/_lan/logout' and request.method == 'POST':
            sessions.values.pop(cookie, None)
            response = web.Response(status=303, headers={'Location': '/_lan/login'})
            response.del_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
            return response
        if request.path == '/_lan/account' and request.method == 'GET':
            return web.Response(text=ACCOUNT, content_type='text/html')
        if request.path == '/_lan/status' and request.method == 'GET':
            return web.json_response(dict(authenticated=True))
        if request.path.startswith('/_lan/'):
            raise web.HTTPNotFound()
        return await forward(request, mode, peer, expires)

    app = web.Application(client_max_size=1 << 30)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.on_response_prepare.append(security_headers)
    app.router.add_route('*', '/{path:.*}', handle)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--upstream', required=True)
    args = parser.parse_args()
    folder = args.root / 'lan-access'
    config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(folder / 'server.pem', folder / 'server-key.pem')
    app = create(args.upstream, (folder / 'access-key.txt').read_text(encoding='ascii').strip(),
                 hosts=config['hosts'], port=config['port'], hop_secret=os.environ.pop(proxy_identity.ENV, ''))
    web.run_app(app, host=config['listen'], port=config['port'], ssl_context=context, access_log=None, print=None)


if __name__ == '__main__':
    main()
