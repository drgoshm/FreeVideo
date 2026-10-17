"""The diagnostic summary a user may add to a suggestion (it is put in the GitHub page).

Only the redacted fields the report form already uses: version, hardware,
settings and timings. No prompt, reference or picture is read here.
"""
import json
import re

# The summary travels in a GitHub page address, which has to stay short.
MAX_DIAGNOSTICS = 3000
# Anything that looks like a file location or a long secret-like token is replaced.
# A location may contain spaces, so it is removed to the end of the line or quote.
PATH = re.compile(r'(?:[A-Za-z]:[\\/]|\\\\|~[\\/])[^\'"<>|\r\n]+|(?<![\w/.])/[\w.\-]+(?:/[^\'"<>|\r\n]+)+')
TOKEN = re.compile(r'\b[A-Za-z0-9_\-]{32,}\b')
HARDWARE_KEYS = ('gpu_name', 'vram_total', 'ram_total', 'system', 'architecture', 'driver_version',
                 'torch_version', 'cuda_version')


def scrub(value):
    return TOKEN.sub('<token>', PATH.sub('<path>', str(value)))


def summary(report, machine=None):
    """The diagnostic summary: the same redacted fields the report form uses, no prompt or picture.

    report is the parsed diagnostic report of the latest generation (or None);
    machine is the installation's machine record (or None).
    """
    out = {}
    hardware = {}
    if isinstance(machine, dict) and isinstance(machine.get('hardware'), dict):
        hardware.update({k: v for k, v in machine['hardware'].items() if k in HARDWARE_KEYS})
    analysis = report.get('analysis') if isinstance(report, dict) else None
    if isinstance(analysis, dict):
        if isinstance(analysis.get('hardware'), dict):
            hardware.update({k: v for k, v in analysis['hardware'].items() if k in HARDWARE_KEYS})
        for key in ('version', 'status', 'request_seconds', 'geometry', 'config', 'stages', 'budgets'):
            if key in analysis:
                out[key] = analysis[key]
        errors = analysis.get('errors')
        if isinstance(errors, list):
            out['errors'] = [scrub(e)[:300] for e in errors[:3]]
    if hardware:
        out['hardware'] = hardware
    # Bound the whole summary; drop the largest optional parts first.
    for key in ('stages', 'config', 'errors', 'budgets', 'geometry'):
        if len(json.dumps(out, separators=(',', ':'), ensure_ascii=False)) <= MAX_DIAGNOSTICS:
            break
        out.pop(key, None)
    return out


def collect(report_id=None):
    """The diagnostic summary for the latest generation, or for this machine alone before any."""
    report = machine = None
    try:
        from .comfy_progress import REPORTS
        with REPORTS.lock:
            token = report_id if report_id in REPORTS.runs else next(reversed(REPORTS.runs), None)
        if token:
            report = json.loads(REPORTS.snapshot(token))
    except (KeyError, OSError, RuntimeError, ValueError, TypeError):
        report = None
    try:
        from .comfy_bridge import installation
        machine = installation()[1]
    except Exception:  # Setup problems must not stop a suggestion from being sent.
        machine = None
    return summary(report, machine)
