"""Whole-adapter VRAM counters for an explicitly selected Windows AMD adapter.

HIP's Windows hipMemGetInfo only describes the calling process. Use the WDDM
GPU Adapter Memory counter, matched by DXGI LUID, for cross-process monitoring.
Temperature, clocks and utilization remain unavailable rather than estimated.
"""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import threading
from uuid import UUID

from .monitoring import Activity, DeviceMemory, Monitor, UnavailablePrograms


class LUID(c.Structure):
    _fields_ = [('LowPart', w.DWORD), ('HighPart', w.LONG)]


class AdapterDescription(c.Structure):
    # https://learn.microsoft.com/windows/win32/api/dxgi/ns-dxgi-dxgi_adapter_desc1
    _fields_ = [('Description', w.WCHAR * 128), ('VendorId', w.UINT),
                ('DeviceId', w.UINT), ('SubSysId', w.UINT), ('Revision', w.UINT),
                ('DedicatedVideoMemory', c.c_size_t), ('DedicatedSystemMemory', c.c_size_t),
                ('SharedSystemMemory', c.c_size_t), ('AdapterLuid', LUID), ('Flags', w.UINT)]


def method(pointer, index, result, *arguments):
    address = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents[index]
    return c.WINFUNCTYPE(result, c.c_void_p, *arguments)(address)


def adapter_memory(name):
    factory = c.c_void_p()
    guid = (c.c_ubyte * 16).from_buffer_copy(UUID('770aae78-f26f-4dba-a829-253c83d1b387').bytes_le)
    create = c.WinDLL('dxgi').CreateDXGIFactory1
    create.argtypes, create.restype = [c.c_void_p, c.POINTER(c.c_void_p)], w.LONG
    if create(c.byref(guid), c.byref(factory)):
        raise RuntimeError('Cannot enumerate DXGI adapters')
    matches = []
    try:
        index = 0
        while True:
            adapter = c.c_void_p()
            result = method(factory, 12, w.LONG, w.UINT, c.POINTER(c.c_void_p))(factory, index, c.byref(adapter))
            if result & 0xffffffff == 0x887a0002:  # DXGI_ERROR_NOT_FOUND
                break
            if result:
                raise RuntimeError('DXGI adapter enumeration failed')
            try:
                desc = AdapterDescription()
                if method(adapter, 10, w.LONG, c.POINTER(AdapterDescription))(adapter, c.byref(desc)):
                    raise RuntimeError('Cannot read DXGI adapter description')
                if desc.Description == name and desc.VendorId == 0x1002:
                    luid = desc.AdapterLuid
                    instance = f'luid_0x{luid.HighPart & 0xffffffff:08x}_0x{luid.LowPart:08x}_phys_0'
                    matches.append((instance, desc.DedicatedVideoMemory))
            finally:
                method(adapter, 2, w.ULONG)(adapter)
            index += 1
    finally:
        method(factory, 2, w.ULONG)(factory)
    if len(matches) != 1 or not matches[0][1]:
        raise RuntimeError('Expected exactly one dedicated AMD adapter named ' + name)
    return matches[0]


class CounterValue(c.Structure):
    _fields_ = [('status', w.DWORD), ('value', c.c_longlong)]


class WDDMMonitor(Monitor):
    def __init__(self, path, interval=.05, device=None, pid=None):
        name = os.environ['FREEVIDEO_WINDOWS_AMD_ADAPTER']
        instance, self.total = adapter_memory(name)
        self.device = str(device or name)
        self.source = 'Windows WDDM GPU Adapter Memory: ' + name + '/' + instance
        self.lib = c.WinDLL('pdh')
        pointer = c.POINTER(c.c_void_p)
        signatures = {
            'PdhOpenQueryW': [w.LPCWSTR, c.c_size_t, pointer],
            'PdhAddEnglishCounterW': [c.c_void_p, w.LPCWSTR, c.c_size_t, pointer],
            'PdhCollectQueryData': [c.c_void_p],
            'PdhGetFormattedCounterValue': [c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.POINTER(CounterValue)],
            'PdhCloseQuery': [c.c_void_p],
        }
        for function, arguments in signatures.items():
            entry = getattr(self.lib, function)
            entry.argtypes, entry.restype = arguments, w.LONG
        self.query, self.counter = c.c_void_p(), c.c_void_p()
        self.check(self.lib.PdhOpenQueryW(None, 0, c.byref(self.query)))
        try:
            self.check(self.lib.PdhAddEnglishCounterW(self.query,
                f'\\GPU Adapter Memory({instance})\\Dedicated Usage', 0, c.byref(self.counter)))
        except BaseException:
            self.shutdown()
            raise
        self.path, self.interval = Path(path), interval
        self.stop_event = threading.Event()
        self.peak = self.baseline = self.last = self.count = 0
        self.errors = []
        self.max_temperature = self.max_power_mw = None
        self.activity, self.busy_activity = Activity(), Activity()
        self.programs = UnavailablePrograms()

    @staticmethod
    def check(status):
        if status:
            raise RuntimeError(f'WDDM performance counter failed: 0x{status & 0xffffffff:08x}')

    def sample(self):
        self.check(self.lib.PdhCollectQueryData(self.query))
        value = CounterValue()
        self.check(self.lib.PdhGetFormattedCounterValue(self.counter, 0x400, None, c.byref(value)))
        if value.status not in (0, 1) or not 0 <= value.value <= self.total:
            raise RuntimeError('WDDM returned unavailable or invalid dedicated-memory counters')
        return value.value, self.total, None

    def auxiliary(self):
        return [None, None, None, None]

    def clock_reasons(self):
        return None

    def shutdown(self):
        if self.query:
            self.lib.PdhCloseQuery(self.query)
            self.query = c.c_void_p()


class WDDMDeviceMemory(DeviceMemory):
    """On-demand whole-adapter capacity for the live Windows memory budget."""

    def __init__(self, device=None):
        # No sampling thread or output file is opened by the monitor constructor.
        self.monitor = WDDMMonitor(os.devnull, device=device)
        self.device, self.source = self.monitor.device, self.monitor.source

    def sample(self):
        used, total, _ = self.monitor.sample()
        return dict(used_bytes=used, total_bytes=total)

    def close(self):
        self.monitor.shutdown()
