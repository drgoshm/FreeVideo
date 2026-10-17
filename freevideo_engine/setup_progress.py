"""Retain overall stages and concurrent transfer progress independently of logs."""
import math
import time

from .launcher_copy import size_text
from .model_status import NAMES


def number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


MODEL_ACTIVITIES = ('download', 'verify', 'assemble', 'copy', 'copy_bytes', 'check', 'resume')


VERB_ACTIVITIES = ('verify', 'assemble', 'copy_bytes', 'check', 'copy', 'download')
COMPONENT_NAMES = ('Video ', 'Text ', 'Sampling ', 'Image ', 'Model', 'Runtime components')


class ProgressState:
    def __init__(self):
        self.phase = {}
        self.tasks = {}
        self.last = {}
        self.model_groups = []
        self.download_notices = {}

    def update(self, event, now=None):
        now = time.monotonic() if now is None else now
        event = dict(event)
        if event.get('kind') == 'models':
            self.model_groups = event.get('groups', [])
            return
        if event.get('kind') == 'phase':
            # A parallel compiler re-sends the running phase with a larger count
            # every moment; only a different phase ends a retry notice.
            if event.get('label') != self.phase.get('label'):
                self.download_notices.clear()
            self.phase = event
            return
        key = str(event.get('key', 'current'))
        if event.get('kind') == 'progress' and key.startswith('download-patient-'):
            if event.get('state', 'running') == 'running' and event.get('detail'):
                self.download_notices[key] = event
            else:
                self.download_notices.pop(key, None)
            return
        if event.get('kind') == 'task':
            self.tasks.pop(key, None)
        previous = self.tasks.get(key, {})
        state = event.get('state', 'running')
        self.tasks[key] = dict(previous, **event, observed_at=now)
        self.tasks[key]['state'] = state
        self.last = self.tasks[key]
        # Completed events need not grow with the number of downloaded files.
        for name in list(self.tasks):
            if len(self.tasks) <= 32:
                break
            if self.tasks[name].get('state') != 'running':
                self.tasks.pop(name)

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        active = [self._view(row, now) for row in self.tasks.values() if row.get('state') == 'running']
        # Keep model work visible while a parallel compiler reports a heartbeat,
        # also in the moments a file is only being connected or verified (no
        # byte total yet); otherwise the line would alternate between the two.
        def models(row):
            return (row.get('unit') == 'bytes' or row.get('activity') in MODEL_ACTIVITIES) and row['age_seconds'] <= 30
        active.sort(key=lambda row: (models(row), number(row.get('total')) and row['total'] > 0,
                                     row.get('unit') == 'bytes'), reverse=True)
        current = active[0] if active else self._view(self.last, now)
        if self.download_notices:
            notice = next(reversed(self.download_notices.values()))
            # A temporary activity line, with the underlying stage and counters
            # retained so clearing it restores the current operation immediately.
            current = dict(current, label=current.get('label') or notice['label'],
                           detail=notice['detail'], activity='', name='')
        return dict(phase_progress=dict(self.phase), active_tasks=active, progress=current, model_groups=self.model_groups)

    @staticmethod
    def _view(row, now):
        value = dict(row)
        age = max(0., now - value.pop('observed_at', now))
        total, done = value.get('total'), value.get('done')
        valid = number(total) and total > 0 and number(done) and done <= total
        value['fraction'] = done / total if valid else None
        remaining = value.get('remaining_seconds')
        if not valid or age > 10 or not number(remaining):
            value['remaining_seconds'] = None
        value['age_seconds'] = age
        return value


def transfer_parts(done, total, rate, decimal_sizes=False):
    """"5.2 / 14.6 GiB" and "31.0 MiB/s" when known; decimal units on macOS, as Finder shows."""
    base = 1000 if decimal_sizes else 1024
    units = ('KB', 'MB', 'GB', 'TB') if decimal_sizes else ('KiB', 'MiB', 'GiB', 'TiB')
    parts = []
    if number(total) and total > 0:
        done = min(done, total) if number(done) else 0
        power = 1
        while power < 4 and total >= base ** (power + 1):
            power += 1
        digits = 1 if power >= 3 else 0
        parts.append('%.*f / %.*f %s' % (digits, done / base ** power, digits, total / base ** power, units[power - 1]))
    if number(rate) and rate >= base:
        power = 2 if rate >= base ** 2 else 1
        parts.append('%.*f %s/s' % (1 if power == 2 else 0, rate / base ** power, units[power - 1]))
    return parts


def progress_text(event, zh=False, *, decimal_sizes=False):
    """One activity line for the launcher, including known transfer counts."""
    t = lambda en, cn: cn if zh else en
    activity, name = event.get('activity'), event.get('name') or ''
    # A Chinese name follows its verb directly; a Latin one (Python) keeps a space.
    gap = ' ' if name[:1].isascii() and name[:1].isalnum() else ''
    if not zh and activity in VERB_ACTIVITIES and (
            name.startswith(COMPONENT_NAMES) or any(name == en for en, _ in NAMES.values())):
        name = name[:1].lower() + name[1:]  # In the middle of a sentence.
    if activity == 'count':
        done, total = event.get('done'), event.get('total')
        if number(done) and number(total) and total > 0 and done <= total:
            return name + t(' · Completed ', ' · 已完成 ') + '%d / %d' % (done, total)
        return name
    if activity in ('verify', 'assemble', 'copy_bytes'):
        verb = {'verify': ('Verifying ', '正在校验'), 'assemble': ('Assembling ', '正在合并'),
                'copy_bytes': ('Copying ', '正在复制')}[activity]
        # MiB rather than tenths of GiB: even slow disks change the visible text.
        parts = [t(verb[0], verb[1] + gap) + name]
        done, total = event.get('done'), event.get('total')
        if event.get('unit') == 'bytes' and number(done) and number(total) and total >= 2**20 and done <= total:
            parts.append('%d / %d %s' % (done // 2**20, total // 2**20, 'MiB'))
        return ' · '.join(parts)
    if activity == 'resume':
        return t('The connection dropped; resuming the download and keeping what was already downloaded',
                 '连接中断，正在继续下载，已下载的部分会保留')
    if activity in ('check', 'copy'):
        parts = [t('Checking ', '正在校验' + gap) + name if activity == 'check'
                 else t('Copying ', '正在复制' + gap) + name]
        done, total = event.get('done'), event.get('total')
        # Below 1 KiB the size would read "0 / 0 KiB".
        if event.get('unit') == 'bytes' and number(done) and number(total) and total >= 1024 and done <= total:
            parts.extend(transfer_parts(done, total, event.get('rate'), decimal_sizes))
        return ' · '.join(parts)
    if activity == 'note':
        if name in ('Finding existing models', '正在查找已有模型'):
            return t('Finding files', '正在查找文件')
        parts = [name]
        scanned = event.get('scanned')
        if number(scanned) and scanned > 0:
            parts.append(t('{:,} checked', '已查看 {} 项').format(scanned))
        return ' · '.join(parts + transfer_parts(0, 0, event.get('rate'), decimal_sizes))
    if activity == 'download':
        total = event.get('total') if event.get('unit') == 'bytes' else 0
        return ' · '.join([t('Downloading ', '正在下载' + gap) + name] +
                          transfer_parts(event.get('done'), total or 0, event.get('rate'), decimal_sizes))
    return ''
