"""Seam sound kept by the chain itself, one small file per block.

A block's opening seam sound lives in its shared continuation record, and the
shared store keeps only the newest records. A chain joins after its last block,
so a long chain would find its earliest records gone. The runner copies each
block's seam sound here as the block finishes; joining reads these copies.
"""
import logging
from pathlib import Path
import re

SUFFIX = '.lead.npz'


def _path(directory, chain, index):
    if not isinstance(chain, str) or not re.fullmatch(r'c-[0-9a-f]{12}', chain) or type(index) is not int or index < 0:
        raise ValueError('Invalid chain lead')
    return Path(directory) / ('%s.%d%s' % (chain, index, SUFFIX))


def save(directory, chain, index, lead, previous, output=None):
    """Keep block `index`'s seam sound with the output of the block it follows and its own output."""
    import numpy as np
    import os
    import uuid
    samples = np.ascontiguousarray(lead['samples'], dtype=np.float32)
    path = _path(directory, chain, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex[:8] + '.tmp')
    try:
        with temporary.open('wb') as stream:
            np.savez(stream, samples=samples, previous=np.array(previous if isinstance(previous, str) else ''),
                     output=np.array(output if isinstance(output, str) else ''))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(directory, chain, index, previous, output=None):
    """The lead dict the seam repair takes, or None when absent, unreadable, or kept for another
    predecessor or another render of this block."""
    import numpy as np
    from .continuation import LEAD_IN_SAMPLES, LEAD_IN_FADE, LEAD_IN_RATE
    try:
        with open(_path(directory, chain, index), 'rb') as stream, np.load(stream, allow_pickle=False) as data:
            samples, kept = data['samples'], str(data['previous'])
            own = str(data['output']) if 'output' in data.files else ''
        if kept and kept != previous or own and output is not None and own != output or samples.dtype != np.float32 or samples.shape != (LEAD_IN_SAMPLES + LEAD_IN_FADE, 2):
            return None
        return dict(samples=samples, before=LEAD_IN_SAMPLES, fade=LEAD_IN_FADE, sample_rate=LEAD_IN_RATE)
    except Exception:
        # Damaged storage (a bad checksum or archive directory included) is the same as no copy.
        return None


def has(directory, chain, index):
    try:
        return _path(directory, chain, index).is_file()
    except (OSError, ValueError):
        return False


def prune(directory, chains):
    """Drop the kept leads of chains whose record is gone; a chain that still has its record file keeps
    all of them, because a longer chain is joined again from its first block. `chains` is what the
    caller loaded; a record beyond its limit or one it could not read is still on disk."""
    chains = set(chains)
    try:
        for path in Path(directory).glob('c-*' + SUFFIX):
            chain = path.name.split('.', 1)[0]
            if chain not in chains and not (Path(directory) / (chain + '.json')).exists():
                path.unlink(missing_ok=True)
    except OSError:
        logging.warning('[FreeVideo] Chain seam sound could not be removed')
