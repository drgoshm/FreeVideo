"""Own the local ComfyUI and HTTPS gateway together, including their Windows child processes."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from . import processes, proxy_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8188)
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    config = json.loads((root / 'lan-access/config.json').read_text(encoding='utf-8'))
    if not config.get('enabled') or config['port'] == args.port:
        raise ValueError('LAN gateway must be enabled on a different port from its backend')
    environment = dict(os.environ, **{proxy_identity.ENV: secrets.token_urlsafe(32)})
    logs = root / 'setup-runs'
    logs.mkdir(exist_ok=True)
    children = []
    try:
        with (logs / 'lan-gateway.log').open('w', encoding='utf-8') as log, processes.worker_signals():
            children.append(processes.popen([sys.executable, '-m', 'freevideo_engine.lan_gateway',
                '--root', str(root), '--upstream', 'http://127.0.0.1:%d' % args.port],
                cwd=str(root), env=environment, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, supervise=True, start_new_session=True))
            children.append(processes.popen([sys.executable, str(root / 'vendor/ComfyUI/main.py'),
                '--listen', '127.0.0.1', '--port', str(args.port), '--extra-model-paths-config',
                str(root / 'encoder-paths.yaml'), '--auto-launch' if args.open_browser else '--disable-auto-launch'],
                cwd=str(root), env=environment, supervise=True, start_new_session=True))
            print('LAN access: HTTPS port %s. Client instructions: LAN-ACCESS.txt' % config['port'], flush=True)
            while True:
                for index, child in enumerate(children):
                    code = child.poll()
                    if code is not None:
                        print(('HTTPS gateway' if index == 0 else 'ComfyUI') + ' exited: ' + str(code), flush=True)
                        return code or (1 if index == 0 else 0)
                time.sleep(.5)
    except KeyboardInterrupt:
        return 130
    finally:
        for child in reversed(children):
            processes.stop(child, grace=2)


if __name__ == '__main__':
    raise SystemExit(main())
