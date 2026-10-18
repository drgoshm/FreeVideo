"""Prepare private TLS/key files once. Does not install a CA into any trust store."""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from freevideo_engine.agent.private_file import create


def private_write(path, data):
    with os.fdopen(create(path), 'wb') as stream:
        stream.write(data)


def prepare(root, hosts, port=8443):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    folder = root / 'lan-access'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = ('config.json', 'server.pem', 'server-key.pem', 'FreeVideo-LAN-CA.crt', 'access-key.txt')
    if any((folder / name).exists() for name in files):
        raise ValueError('LAN configuration already exists; preserve or back it up before provisioning a replacement')
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'FreeVideo LAN ' + secrets.token_hex(4))])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(days=730))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    names = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'FreeVideo LAN')]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, None, None), critical=True)
            .sign(ca_key, hashes.SHA256()))
    # The CA signing key is discarded. Only this one server certificate can be used.
    private_write(folder / 'server-key.pem', key.private_bytes(serialization.Encoding.PEM,
                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    private_write(folder / 'access-key.txt', (secrets.token_urlsafe(32) + '\n').encode('ascii'))
    (folder / 'server.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / 'FreeVideo-LAN-CA.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    config = dict(enabled=True, listen='0.0.0.0', port=port, hosts=hosts,
                  ca_sha256=ca.fingerprint(hashes.SHA256()).hex(), certificate_expires=cert.not_valid_after_utc.isoformat())
    (folder / 'config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    return config


def rotate_key(root):
    folder = root / 'lan-access'
    if not (folder / 'config.json').is_file():
        raise ValueError('Prepare LAN access first')
    temporary = folder / ('.access-key-' + secrets.token_hex(8))
    private_write(temporary, (secrets.token_urlsafe(32) + '\n').encode('ascii'))
    os.replace(temporary, folder / 'access-key.txt')
    return {'key_rotated': True, 'next': 'Restart FreeVideo to revoke the old key and all browser sessions.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--host', action='append')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--rotate-key', action='store_true')
    args = parser.parse_args()
    if not args.rotate_key and not args.host:
        parser.error('--host is required for initial setup')
    print(json.dumps(rotate_key(args.root) if args.rotate_key else prepare(args.root, args.host, args.port), indent=2))
