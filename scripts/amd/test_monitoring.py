"""CPU checks for AMD monitoring against the upstream worker interface."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from freevideo_engine.monitoring import DeviceMemory, Monitor
from freevideo_engine.wddm_monitoring import WDDMDeviceMemory, WDDMMonitor


class AMDMonitoring(unittest.TestCase):
    def library(self):
        lib = Mock()
        def open_query(source, user, target):
            target._obj.value = 1
            return 0
        def add_counter(query, name, user, target):
            target._obj.value = 2
            return 0
        def read_counter(counter, fmt, kind, target):
            target._obj.status, target._obj.value = 0, 400
            return 0
        lib.PdhOpenQueryW.side_effect = open_query
        lib.PdhAddEnglishCounterW.side_effect = add_counter
        lib.PdhCollectQueryData.return_value = 0
        lib.PdhGetFormattedCounterValue.side_effect = read_counter
        return lib

    def amd_context(self, library):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, FREEVIDEO_WINDOWS_AMD_ADAPTER='AMD test'))
        stack.enter_context(patch('freevideo_engine.monitoring.windows', return_value=True))
        stack.enter_context(patch('freevideo_engine.monitoring.nvml_device', side_effect=AssertionError('NVIDIA query')))
        stack.enter_context(patch('freevideo_engine.wddm_monitoring.adapter_memory', return_value=('test_luid', 1024)))
        stack.enter_context(patch('freevideo_engine.wddm_monitoring.c.WinDLL', return_value=library, create=True))
        return stack

    def test_worker_pid_and_monitor_lifecycle_use_pdh(self):
        lib = self.library()
        with self.amd_context(lib), tempfile.TemporaryDirectory() as folder:
            monitor = Monitor(Path(folder) / 'gpu.csv', device='GPU-amd', pid=123)
            self.assertIsInstance(monitor, WDDMMonitor)
            result = monitor.start().stop()
            self.assertEqual(result['gpu_peak_bytes'], 400)
            self.assertEqual(result['sampling_errors'], [])
            self.assertFalse(result['other_programs']['supported'])
            self.assertEqual(result['other_programs']['checks'], 0)
            self.assertIn('WDDM', result['source'])
            lib.PdhCloseQuery.assert_called_once()
            lib.nvmlShutdown.assert_not_called()

    def test_live_budget_reads_whole_adapter_without_nvml(self):
        lib = self.library()
        with self.amd_context(lib):
            memory = DeviceMemory('GPU-amd')
            self.assertIsInstance(memory, WDDMDeviceMemory)
            self.assertEqual(memory.sample(), {'used_bytes': 400, 'total_bytes': 1024})
            memory.close()
            memory.close()
            lib.PdhCloseQuery.assert_called_once()
            lib.nvmlShutdown.assert_not_called()

    def test_nvidia_monitor_selection_remains_unchanged(self):
        lib = Mock()
        with patch.dict(os.environ, {}, clear=True), patch('freevideo_engine.monitoring.windows', return_value=True), \
                patch('freevideo_engine.monitoring.nvml_device', return_value=(lib, 'handle', 'GPU-nvidia')) as nvml:
            monitor = Monitor('unused.csv', device='GPU-nvidia', pid=123)
            self.assertIs(type(monitor), Monitor)
            self.assertEqual(monitor.programs.pid, 123)
            monitor.shutdown()
            nvml.assert_called_once_with('GPU-nvidia')
            lib.nvmlShutdown.assert_called_once()

    def test_linux_rocm_factory_accepts_worker_pid(self):
        from freevideo_engine.rocm_monitoring import ROCmMonitor
        import inspect
        inspect.signature(ROCmMonitor.__init__).bind(None, 'unused.csv', device='GPU-123', pid=123)
        with patch.dict(os.environ, {'ROCR_VISIBLE_DEVICES': 'GPU-123'}, clear=True), \
                patch('freevideo_engine.monitoring.windows', return_value=False), \
                patch.object(ROCmMonitor, '__init__', return_value=None) as initialize:
            monitor = Monitor('unused.csv', device='GPU-123', pid=123)
            self.assertIsInstance(monitor, ROCmMonitor)
            initialize.assert_called_once_with('unused.csv', device='GPU-123', pid=123)


if __name__ == '__main__':
    unittest.main()
