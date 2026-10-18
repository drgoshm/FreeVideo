"""Install the remote MCP client, verify it, and transactionally edit Codex TOML."""
import argparse
from collections.abc import MutableMapping
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit
import uuid

import tomlkit

HTTP_FIELDS = ('url', 'bearer_token_env_var', 'http_headers', 'env_http_headers',
               'http_headers_helper', 'oauth', 'auth', 'experimental_environment')


def without_freevideo(value):
    value = copy.deepcopy(value)
    servers = value.get('mcp_servers')
    if isinstance(servers, dict):
        servers.pop('freevideo', None)
        if not servers:
            value.pop('mcp_servers', None)
    return value


def updated_config(text, settings):
    """Preserve comments/formatting with TOMLKit and independently verify semantics."""
    before = tomllib.loads(text)
    document = tomlkit.parse(text)
    if 'mcp_servers' not in document:
        document['mcp_servers'] = tomlkit.table()
    servers = document['mcp_servers']
    if not isinstance(servers, MutableMapping):
        raise ValueError('mcp_servers must be a TOML table; configuration was not changed')
    if 'freevideo' not in servers:
        servers['freevideo'] = tomlkit.table()
    server = servers['freevideo']
    if not isinstance(server, MutableMapping):
        raise ValueError('mcp_servers.freevideo must be a TOML table')
    for field in HTTP_FIELDS:
        server.pop(field, None)
    for name, value in settings.items():
        server[name] = value
    server.setdefault('startup_timeout_sec', 90)
    server.setdefault('tool_timeout_sec', 120)
    result = tomlkit.dumps(document)
    after = tomllib.loads(result)
    if without_freevideo(before) != without_freevideo(after):
        raise ValueError('Unrelated settings changed; refusing to save')
    return result


def secure_write(path, content):
    """Create, never overwrite, a private file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def save_config(path, settings):
    # Preserve a user-managed symlink rather than replacing the link itself.
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.exists() else None
    text = (original or b'').decode('utf-8')
    result = updated_config(text, settings).encode('utf-8')
    if result == original:
        return None
    backup = None
    if original is not None:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        backup = path.with_name(path.name + '.freevideo-backup-' + stamp)
        secure_write(backup, original)
    descriptor, temp = tempfile.mkstemp(prefix='.freevideo-', suffix='.toml', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        current = path.read_bytes() if path.exists() else None
        if current != original:
            raise RuntimeError('Codex configuration changed during installation; retry after closing its settings')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return backup


def prepare_credentials(home, source):
    folder = home / '.config/freevideo'
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    paths = []
    for name in ('access-key.txt', 'FreeVideo-LAN-CA.crt'):
        target = folder / name
        if not target.is_file():
            candidate = next((p for p in (source / name, home / 'Downloads' / name) if p.is_file()), None)
            if candidate is None:
                raise ValueError('Missing file: ' + str(target))
            secure_write(target, candidate.read_bytes())
        target.chmod(0o600)
        paths.append(target)
    key, ca = paths
    value = key.read_text(encoding='ascii').strip()
    if len(value) < 32 or len(value) > 128 or any(c.isspace() for c in value):
        raise ValueError('Invalid access-key.txt; copy the original LAN key file again')
    ssl.create_default_context(cafile=str(ca))
    return key, ca


def verify_client(python, release, arguments):
    messages = [
        dict(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': '2025-06-18', 'capabilities': {}}),
        dict(jsonrpc='2.0', id=2, method='tools/list', params={}),
        dict(jsonrpc='2.0', id=3, method='tools/call', params={'name': 'freevideo_prompt_guide', 'arguments': {}}),
    ]
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    try:
        run = subprocess.run([str(python), *arguments], cwd=release, env=environment,
            input=''.join(json.dumps(row) + '\n' for row in messages),
            text=True, encoding='utf-8', capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError('MCP check timed out. Start FreeVideo on Windows and check the LAN connection.') from None
    # Do not echo arbitrary process output: it could contain private data.
    try:
        answers = {row['id']: row for row in map(json.loads, run.stdout.splitlines())}
        if run.returncode or answers[1]['result']['serverInfo']['name'] != 'freevideo':
            raise ValueError
        names = {tool['name'] for tool in answers[2]['result']['tools']}
        if not {'freevideo_generate', 'freevideo_wait', 'freevideo_status'} <= names:
            raise ValueError
        if answers[3]['result'].get('isError'):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('MCP check failed. Check the server URL, LAN key and CA certificate. Codex settings were not changed.') from None
    return len(names)


def install(source, release, url, config, home, python):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Use an HTTPS server origin, for example https://192.168.168.102:8443')
    source, release = source.resolve(), release.resolve()
    if not (source / 'freevideo_engine/agent/mcp.py').is_file():
        raise ValueError('The archive must be fully extracted before installation')
    key, ca = prepare_credentials(home, source)
    shutil.copytree(source / 'freevideo_engine', release / 'freevideo_engine',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(source / 'LICENSE', release / 'LICENSE')
    arguments = ['-B', '-X', 'utf8', '-m', 'freevideo_engine.agent.mcp', '--root', str(release),
                 '--url', url.rstrip('/'), '--token-file', str(key), '--ca-file', str(ca)]
    print('Checking MCP and the authenticated HTTPS connection...', flush=True)
    count = verify_client(python, release, arguments)
    settings = dict(command=str(python), args=arguments, cwd=str(release), enabled=True)
    backup = save_config(config, settings)
    print('MCP verified: %d tools. Codex configuration: %s' % (count, config))
    if backup:
        print('Backup: ' + str(backup))
    print('Done. Restart Codex on this Mac, open a new local chat, and ask it to check FreeVideo via MCP.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--url', default='https://192.168.168.102:8443')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This installer is for macOS only')
    config = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser() / 'config.toml'
    try:
        install(args.source, args.release, args.url, config, Path.home(), Path(sys.executable).absolute())
    except Exception as error:
        print('Installation stopped: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
