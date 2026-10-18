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


def authenticated_lan(request):
    """Only the TLS gateway holding this process's ephemeral hop secret can grant control."""
    if (not proxy_identity.trusted(request) or
            request.headers.get(proxy_identity.LAN_AUTH) not in ('browser', 'agent')):
        return False
    try:
        peer = request.transport.get_extra_info('peername')[0]
        origin = urlsplit(request.headers.get(proxy_identity.ORIGIN, ''))
        address = peer_address(request)
        return (ipaddress.ip_address(peer).is_loopback and origin.scheme == 'https'
                and address is not None and address.is_private and not address.is_unspecified
                and not address.is_multicast and not address.is_link_local)
    except (ValueError, TypeError, IndexError, AttributeError):
        return False


def check(request, *, local=False):
    address = peer_address(request)
    proxied = proxy_identity.HEADER in request.headers
    control = authenticated_lan(request)
    # The ordinary product proxy remains excluded from local controls. Only the
    # authenticated HTTPS gateway explicitly delegates an operator's session.
    if (proxied and (not proxy_identity.trusted(request) or (local and not control) or not (control or lan_enabled()))
            or address is None or not address.is_loopback and
            ((local and not control) or not address.is_private or not (control or lan_enabled()))):
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
