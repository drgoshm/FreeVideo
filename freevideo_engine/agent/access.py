"""Peer and browser-origin policy, independent of bearer authentication."""
import ipaddress
from urllib.parse import urlsplit

from .jobs import JobError
from .. import proxy_identity


def peer_address(request):
    peer = request.transport.get_extra_info('peername') if request.transport else None
    try:
        address = ipaddress.ip_address(peer[0].split('%')[0])
        address = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
        if address.is_loopback and proxy_identity.trusted(request):
            address = ipaddress.ip_address(request.headers.get(proxy_identity.PEER, '').split('%')[0])
        return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
    except (TypeError, ValueError, IndexError):
        return None


def lan_enabled():
    from ..comfy_bridge import installation_root
    from ..launcher_settings import Store
    try:
        return Store(installation_root()).currently_allows('lan_access')
    except (OSError, ValueError):
        return False


def check(request, *, local=False):
    address = peer_address(request)
    proxied = proxy_identity.HEADER in request.headers
    # Even a proxy client on loopback is never a local control-plane client.
    if (proxied and (not proxy_identity.trusted(request) or local or not lan_enabled())
            or address is None or not address.is_loopback and (local or not address.is_private or not lan_enabled())):
        raise JobError('local_only', 'Peer is not permitted')
    socket = request.transport.get_extra_info('sockname') if request.transport else None
    try:
        host = urlsplit(request.scheme + '://' + request.host)
        allowed = {socket[0].split('%')[0]} if socket else set()
        if address.is_loopback:
            allowed.update(('localhost', '127.0.0.1', '::1'))
        scheme = request.scheme
        if proxied:
            proxy_origin = urlsplit(request.headers.get(proxy_identity.ORIGIN, ''))
            if (host.hostname, host.port) == (proxy_origin.hostname, proxy_origin.port):
                allowed.add(proxy_origin.hostname)
                socket = (proxy_origin.hostname, proxy_origin.port)
                scheme = proxy_origin.scheme
        if host.hostname not in allowed or host.port not in (None, socket[1]):
            raise ValueError
        if len(request.headers.getall('Origin', [])) > 1:
            raise ValueError
        origin = request.headers.get('Origin')
        if origin is not None:
            parsed = urlsplit(origin)
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            own_port = host.port or (443 if scheme == 'https' else 80)
            if (parsed.scheme != scheme or parsed.hostname != host.hostname or port != own_port
                    or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
                raise ValueError
    except (ValueError, TypeError, IndexError):
        raise JobError('origin_forbidden', 'Origin or Host does not identify this instance') from None
