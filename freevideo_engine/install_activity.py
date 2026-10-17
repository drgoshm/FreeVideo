"""Small installation worker events: actual work, never elapsed-time counters."""
import json
import sys
import time


def emit(event, **row):
    sys.stdout.write(json.dumps(dict(event=event, **row)) + '\n')
    sys.stdout.flush()


def step(name, done=0, total=0):
    emit('installation_step', name=name, done=done, total=total)


def byte_progress(event, file, *, name='Model files', report=None):
    """Report bounded reads/writes, including both endpoints, at most twice/s."""
    last = None
    def progress(done, total):
        nonlocal last
        now = time.monotonic()
        if last is None or done == total or now - last >= .5:
            row = dict(file=str(file), name=name, done_bytes=done, total_bytes=total)
            if report is None:
                emit(event, **row)
            else:
                report(**row)
            last = now
    return progress
