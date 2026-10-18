"""Connection instructions for an agent on another computer; no server-local paths or secrets."""


def remote_guide(value):
    value = dict(value)
    value.update(token_file='Client-local file selected by FREEVIDEO_TOKEN_FILE',
        authentication='Ask the owner for the separate LAN access key and FreeVideo-LAN-CA.crt through a trusted channel. '
                       'Store the key in a private file on this client and set FREEVIDEO_TOKEN_FILE to its path. '
                       'Set FREEVIDEO_CA_FILE to the CA file. Send the key as Authorization: Bearer over verified HTTPS. '
                       'Never use the server installation token, put the key in a URL, or disable certificate verification.',
        session='Use the HTTPS API from this computer. Never try to read paths on the FreeVideo server as local files.',
        command=[], mcp_json={},
        claude_code='On a client with this FreeVideo source: python -m freevideo_engine.agent.mcp --url ORIGIN '
                    '--token-file CLIENT_KEY_FILE --ca-file CLIENT_CA_FILE',
        codex='Use the same stdio MCP command above, with paths on the agent computer.')
    value['http_example'] = '\n'.join([
        'import json, os, pathlib, ssl, urllib.request',
        'base = ' + repr(value['api']),
        'token = pathlib.Path(os.environ["FREEVIDEO_TOKEN_FILE"]).read_text().strip()',
        'context = ssl.create_default_context(cafile=os.environ["FREEVIDEO_CA_FILE"])',
        'class NoRedirect(urllib.request.HTTPRedirectHandler):',
        '    def redirect_request(self, *args, **kwargs): raise RuntimeError("Redirect refused")',
        'opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirect())',
        'def call(method, path, body=None):',
        '    data = None if body is None else json.dumps(body).encode()',
        '    req = urllib.request.Request(base + path, data=data, method=method,',
        '        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",',
        '                 "X-FreeVideo-Client": "LAN Agent"})',
        '    with opener.open(req) as response: return json.load(response)',
        'call("POST", "/connect/hello", {"client": "LAN Agent", "via": "http"})',
        'print(call("GET", ""))',
        '# Only generate after the user requests it. Use the routes and conversation protocol below.',
    ])
    return value
