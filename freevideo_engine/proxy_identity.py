"""Ephemeral identity of the product proxy's hop to the local backend.

The service supplies a fresh secret to its two children, never to arguments,
settings, receipts or logs. An unconfigured standalone proxy fails closed for
Agent endpoints. Ordinary forwarding headers never establish trust.
"""
import hmac
from functools import lru_cache

ENV = 'FREEVIDEO_PROXY_HOP_SECRET'
HEADER = 'X-FreeVideo-Proxy-Hop'
PEER = 'X-FreeVideo-Proxy-Peer'
ORIGIN = 'X-FreeVideo-Proxy-Origin'


@lru_cache(maxsize=1)
def app_key():
    from aiohttp import web
    return web.AppKey('freevideo_proxy_hop_secret', str)


def headers(request, secret):
    peer = request.transport.get_extra_info('peername') if request.transport else None
    socket = request.transport.get_extra_info('sockname') if request.transport else None
    host = ('[%s]' if ':' in socket[0] else '%s') % socket[0] if socket else ''
    origin = '%s://%s:%d' % (request.scheme, host, socket[1]) if socket else ''
    return [(HEADER, secret or 'unconfigured'), (PEER, str(peer[0]) if peer else ''), (ORIGIN, origin)]


def trusted(request):
    app = getattr(request, 'app', None)
    secret = app.get(app_key(), '') if app is not None else ''
    offered = request.headers.get(HEADER, '')
    return bool(secret and offered) and hmac.compare_digest(secret.encode(), offered.encode())
