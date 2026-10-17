"""Public media identities; filesystem resolution stays behind the authenticated API."""
from urllib.parse import quote, parse_qs, urlsplit
from pathlib import PurePosixPath
import re

PREFIX = '/freevideo/agent/v1'


def view(relative, kind='input'):
    return dict(file=str(relative), url=PREFIX + '/media/' + kind + '/' + quote(str(relative), safe='/') + '/file')


def public(value, _key=None):
    """Migrate legacy media views while discarding server-local filesystem fields."""
    if isinstance(value, list):
        return [public(item, _key) for item in value]
    if isinstance(value, str):
        if _key in ('url', 'thumbnail') and value.startswith(PREFIX + '/'):
            return value
        if PurePosixPath(value).is_absolute() or re.match(r'^(?:[A-Za-z]:[\\/]|\\\\)', value):
            return '[local path]'
        return value
    if not isinstance(value, dict):
        return value
    # Migrate the URL before recursing; /view is an obsolete media URL, not
    # an absolute filesystem path to preserve.
    result = {k: v for k, v in value.items() if k != 'path'}
    url = result.get('url')
    if isinstance(url, str) and url.startswith('/view?'):
        query = parse_qs(urlsplit(url).query)
        relative = result.get('file') or '/'.join((query.get('subfolder', [''])[0], query.get('filename', [''])[0])).lstrip('/')
        kind = query.get('type', ['output'])[0]
        if relative and kind in ('input', 'output') and not PurePosixPath(relative).is_absolute() and '..' not in PurePosixPath(relative).parts:
            result.update(view(relative, kind))
        else:
            result.pop('url', None)
    return {k: public(v, k) for k, v in result.items()}
