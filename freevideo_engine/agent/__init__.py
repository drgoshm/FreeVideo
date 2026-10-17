"""Agent-native control of FreeVideo: job specs, media tools, headless edits and MCP.

Agent features are enabled by default. Set FREEVIDEO_AGENT=0 (or false, no, off)
where ComfyUI runs and for the managed commands to disable the agent API, token,
commands and controls."""
import os

API_VERSION = 1
SWITCH = 'FREEVIDEO_AGENT'


def enabled(environ=None):
    """On by default; only FREEVIDEO_AGENT=0/false/no/off explicitly disables it."""
    value = (os.environ if environ is None else environ).get(SWITCH, '')
    return value.strip().lower() not in ('0', 'false', 'no', 'off')
