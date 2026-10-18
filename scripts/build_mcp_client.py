"""Package current source for a remote MCP client; no runtime state."""
from pathlib import Path
import json
import zipfile

root = Path(__file__).resolve().parents[1]
output = root / 'setup-runs/freevideo-mcp-macos.zip'
output.parent.mkdir(parents=True, exist_ok=True)
config_script = '''import json
from pathlib import Path
import sys

if sys.version_info < (3, 12):
    raise SystemExit("Use Python 3.12 or newer to run this client.")
root = Path(__file__).resolve().parent
credentials = Path.home() / ".config/freevideo"
for name in ("access-key.txt", "FreeVideo-LAN-CA.crt"):
    if not (credentials / name).is_file():
        raise SystemExit("Missing file: " + str(credentials / name))
args = ["-B", "-m", "freevideo_engine.agent.mcp", "--root", str(root),
        "--url", "https://192.168.168.102:8443", "--token-file", str(credentials / "access-key.txt"),
        "--ca-file", str(credentials / "FreeVideo-LAN-CA.crt")]
print("[mcp_servers.freevideo]")
print("command = " + json.dumps(sys.executable))
print("args = " + json.dumps(args))
print("cwd = " + json.dumps(str(root)))
print("startup_timeout_sec = 90")
print("tool_timeout_sec = 120")
print("enabled = true")
'''
readme = '''FreeVideo MCP client for macOS

Automatic installation: open Install-FreeVideo-MCP.command after extracting
the complete archive. It installs its own Python and dependencies, checks MCP,
and updates Codex configuration with a backup. See INSTALL-MAC.txt (Russian).
The manual instructions below remain available as an alternative.

This archive contains the current local Python sources, including HTTPS LAN
authentication. It does not contain models, credentials, logs or videos.
Only the MCP client is started. Python 3.12 is recommended.
Client dependencies: NumPy and Pillow (see requirements-mcp.txt).
No GPU dependencies are required for MCP control.

1. Extract freevideo-mcp into ~/.local/share/ on the Mac.
2. Keep access-key.txt and FreeVideo-LAN-CA.crt in ~/.config/freevideo/.
3. Create a private Python environment and install the client dependencies:
   cd ~/.local/share/freevideo-mcp
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-mcp.txt
   .venv/bin/python print_codex_config.py
   Use your Python 3.12 executable if python3 is an older version.
4. Add the printed block to ~/.codex/config.toml. Keep existing configuration.
   Replace an existing [mcp_servers.freevideo] block rather than duplicating it.
   The helper only prints configuration and never modifies your Codex settings.
5. Restart Codex and open a local chat. Ask it to call freevideo_status and
   freevideo_prompt_guide without generating a video.

The server on Windows must be running and reachable over the LAN.
The MCP transport is STDIO on the Mac; its API connection to Windows is HTTPS.
Do not configure the FreeVideo web URL as a Streamable HTTP MCP endpoint.
Credentials remain in their private file, outside the source package.

To generate from Codex, ask for a video and mention FreeVideo.
To converse in the Autopilot page, ask Codex to listen using freevideo_wait
without a job ID and reply to the current session with its session tools.
An MCP connection does not itself keep an LLM turn active indefinitely.

Sources: https://learn.chatgpt.com/docs/extend/mcp?surface=cli
'''
with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for source in sorted((root / 'freevideo_engine').rglob('*.py')):
        archive.write(source, 'freevideo-mcp/' + source.relative_to(root).as_posix())
    archive.write(root / 'LICENSE', 'freevideo-mcp/LICENSE')
    archive.writestr('freevideo-mcp/print_codex_config.py', config_script)
    archive.writestr('freevideo-mcp/README.txt', readme)
    archive.writestr('freevideo-mcp/requirements-mcp.txt', 'numpy>=2\npillow>=11\n')
    for name in ('Install-FreeVideo-MCP.command', 'install_mcp.py', 'requirements-installer.txt', 'INSTALL-MAC.txt'):
        source = root / 'scripts/mac_mcp' / name
        info = zipfile.ZipInfo('freevideo-mcp/' + name)
        info.create_system = 3
        info.external_attr = (0o100755 if name.endswith('.command') else 0o100644) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, source.read_text(encoding='utf-8').replace('\r\n', '\n').encode('utf-8'))
print(json.dumps({'archive': str(output), 'bytes': output.stat().st_size}))
