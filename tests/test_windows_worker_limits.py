"""Real Windows Job Object checks, exercised by the Windows CI runner."""

from __future__ import annotations

import os
import ctypes
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from h5reclaim.worker_limits import _run_windows_job, run_worker


class _KernelFunction:
    def __init__(self, name: str, kernel: "_FakeKernel") -> None:
        self.name = name
        self.kernel = kernel

    def __call__(self, *args: object) -> int:
        self.kernel.calls.append(self.name)
        if self.name == "CreateJobObjectW":
            return 11
        if self.name == "GetEnvironmentStringsW":
            return ctypes.addressof(self.kernel.environment_block)
        if self.name == "SetInformationJobObject":
            limits = args[2]._obj
            self.kernel.flags = limits.BasicLimitInformation.LimitFlags
            self.kernel.memory = (limits.ProcessMemoryLimit, limits.JobMemoryLimit)
        if self.name == "CreateFileW":
            return 12
        if self.name == "InitializeProcThreadAttributeList":
            args[-1]._obj.value = 128
            return 1
        if self.name == "UpdateProcThreadAttribute":
            self.kernel.attribute = args[2]
            self.kernel.inherited_handles = tuple(args[3])
        if self.name == "CreateProcessW":
            self.kernel.application_name = args[0]
            self.kernel.creation_flags = args[5]
            self.kernel.environment_seen = "".join(args[6][:])
            process = args[-1]._obj
            process.hProcess, process.hThread = 13, 14
            return 1
        if self.name == "ResumeThread":
            return 1
        if self.name == "WaitForSingleObject":
            if self.kernel.timed_out and not self.kernel.waited:
                self.kernel.waited = True
                return 0x102
            return 0
        if self.name == "GetExitCodeProcess":
            args[1]._obj.value = 17
        return 1


class _FakeKernel:
    def __init__(self, *, timed_out: bool = False) -> None:
        self.calls: list[str] = []
        self.functions: dict[str, _KernelFunction] = {}
        self.flags = 0
        self.memory = (0, 0)
        self.attribute = 0
        self.inherited_handles: tuple[int, ...] = ()
        self.application_name = None
        self.creation_flags = 0
        self.environment_block = ctypes.create_unicode_buffer("=C:=C:\\work\0PATH=C:\\Windows\0\0")
        self.environment_seen = ""
        self.timed_out = timed_out
        self.waited = False

    def __getattr__(self, name: str) -> _KernelFunction:
        if name not in self.functions:
            self.functions[name] = _KernelFunction(name, self)
        return self.functions[name]


class WindowsJobCallOrderTests(unittest.TestCase):
    def test_assignment_and_limits_precede_resume(self) -> None:
        kernel = _FakeKernel()
        with patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            result = _run_windows_job(["python", "-c", "pass"], {"PATH": "C:\\Python"},
                                      timeout_seconds=3, memory_bytes=128 * 1024**2)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(kernel.flags & (0x100 | 0x200 | 0x2000), 0x100 | 0x200 | 0x2000)
        self.assertEqual(kernel.memory, (128 * 1024**2, 128 * 1024**2))
        self.assertEqual(kernel.application_name, "python")
        self.assertEqual(kernel.attribute, 0x20002)
        self.assertEqual(kernel.inherited_handles, (12,))
        self.assertTrue(kernel.creation_flags & 0x80000)
        self.assertIn("=C:=C:\\work\0PATH=C:\\Python\0", kernel.environment_seen)
        self.assertIn("FreeEnvironmentStringsW", kernel.calls)
        self.assertLess(kernel.calls.index("SetInformationJobObject"),
                        kernel.calls.index("CreateProcessW"))
        self.assertLess(kernel.calls.index("AssignProcessToJobObject"),
                        kernel.calls.index("ResumeThread"))
        self.assertIn("DeleteProcThreadAttributeList", kernel.calls)
        self.assertEqual(kernel.calls[-1], "CloseHandle")

    def test_timeout_terminates_job(self) -> None:
        kernel = _FakeKernel(timed_out=True)
        with patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_windows_job(["python", "-c", "pass"], {"PATH": "C:\\Python"},
                                 timeout_seconds=0.1, memory_bytes=128 * 1024**2)
        self.assertIn("TerminateJobObject", kernel.calls)
        self.assertLess(kernel.calls.index("TerminateJobObject"),
                        kernel.calls.index("CloseHandle"))

    def test_invalid_or_oversized_limits_fail_before_launch(self) -> None:
        kernel = _FakeKernel()
        with patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            with self.assertRaises(ValueError):
                _run_windows_job(["python"], {"PATH": "C:\\Python"},
                                 timeout_seconds=3, memory_bytes=16 * 1024**3 + 1)
        self.assertNotIn("CreateProcessW", kernel.calls)


@unittest.skipUnless(os.name == "nt", "Windows Job Object tests")
class WindowsWorkerLimitsTests(unittest.TestCase):
    def test_worker_runs_under_job_and_returns_exit_code(self) -> None:
        result = run_worker([sys.executable, "-c", "import sys; sys.exit(17)"],
                            env=os.environ.copy(), timeout_seconds=10,
                            memory_bytes=256 * 1024**2)
        self.assertEqual(result.returncode, 17)

    def test_job_memory_limit_refuses_oversized_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            success = Path(directory) / "large-allocation-succeeded"
            script = ("from pathlib import Path; import sys; "
                      "value=bytearray(512*1024*1024); value[-1]=42; "
                      "Path(sys.argv[1]).write_text('allocated')")
            result = run_worker([sys.executable, "-c", script, str(success)],
                                env=os.environ.copy(), timeout_seconds=20,
                                memory_bytes=128 * 1024**2)
            self.assertFalse(success.exists(), f"allocation succeeded with exit {result.returncode}")

    def test_timeout_terminates_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            orphan = Path(directory) / "descendant-outlived-timeout"
            child_script = ("import subprocess, sys, time; from pathlib import Path; "
                            "subprocess.Popen([sys.executable, '-c', "
                            "'import sys, time; from pathlib import Path; '"
                            "'time.sleep(5); Path(sys.argv[1]).write_text(\"orphan\")', sys.argv[2]]); "
                            "Path(sys.argv[1]).write_text('spawned'); time.sleep(20)")
            with self.assertRaises(subprocess.TimeoutExpired):
                run_worker([sys.executable, "-c", child_script, str(ready), str(orphan)],
                           env=os.environ.copy(), timeout_seconds=3,
                           memory_bytes=256 * 1024**2)
            self.assertTrue(ready.exists(), "worker did not spawn its descendant")
            time.sleep(5.25)
            self.assertFalse(orphan.exists(), "timed-out worker left a live descendant")


if __name__ == "__main__":
    unittest.main()
