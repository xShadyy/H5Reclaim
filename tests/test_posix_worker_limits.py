"""Exercise macOS's monitor on POSIX, including real child processes."""

import errno
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from h5reclaim.worker_limits import _run_monitored_posix, memory_budget_record


@unittest.skipIf(os.name == "nt", "POSIX process-group monitor tests")
class PosixWorkerLimitTests(unittest.TestCase):
    def require_process_listing(self):
        if sys.platform.startswith("linux"):
            proc_pid = int(Path("/proc/self/stat").read_text().split(maxsplit=1)[0])
            if proc_pid != os.getpid():
                self.skipTest("process monitor needs matching /proc and PID namespaces")

    def assert_process_stopped(self, pid):
        deadline = time.monotonic() + 2
        while True:
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="],
                                    capture_output=True, text=True, timeout=2)
            status = result.stdout.strip()
            # An adopted zombie is no longer running or holding memory.
            if not status or status.startswith("Z"):
                return
            if time.monotonic() >= deadline:
                self.fail(f"worker descendant {pid} is still running: {status}")
            time.sleep(0.02)

    def child_command(self, marker, *, allocate=False, parent_exit=False):
        child = (f"from pathlib import Path; import os,time; Path({str(marker)!r}).write_text(str(os.getpid())); "
                 + ("allocation=bytearray(96*1024*1024); " if allocate else "")
                 + "time.sleep(30)")
        parent = ("import subprocess,sys,time; from pathlib import Path; "
                  f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                  f"marker=Path({str(marker)!r}); "
                  "\nwhile not marker.exists(): time.sleep(0.01)\n"
                  + ("sys.exit(0)" if parent_exit else "time.sleep(30)"))
        return [sys.executable, "-c", parent]

    def test_monitor_preserves_worker_exit_code(self):
        self.require_process_listing()
        result = _run_monitored_posix([sys.executable, "-c", "raise SystemExit(7)"],
                                      os.environ, 10, 256 * 1024**2)
        self.assertEqual(result.returncode, 7)

    def test_memory_budget_includes_children_and_cleans_them_up(self):
        self.require_process_listing()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            with self.assertRaises(OSError) as raised:
                _run_monitored_posix(self.child_command(marker, allocate=True),
                                     os.environ, 10, 64 * 1024**2)
            self.assertEqual(raised.exception.errno, errno.ENOMEM)
            self.assert_process_stopped(int(marker.read_text()))

    def test_timeout_cleans_up_child_processes(self):
        self.require_process_listing()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_monitored_posix(self.child_command(marker), os.environ, 2, 256 * 1024**2)
            self.assert_process_stopped(int(marker.read_text()))

    def test_normal_exit_also_cleans_up_child_processes(self):
        self.require_process_listing()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            result = _run_monitored_posix(self.child_command(marker, parent_exit=True),
                                          os.environ, 10, 256 * 1024**2)
            self.assertEqual(result.returncode, 0)
            self.assert_process_stopped(int(marker.read_text()))

    def test_monitor_failure_terminates_worker(self):
        real_popen = subprocess.Popen
        workers = []

        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            workers.append(process)
            return process

        with patch("h5reclaim.worker_limits.subprocess.Popen", side_effect=start), \
                patch("h5reclaim.worker_limits._process_group_rss", side_effect=OSError("monitor unavailable")):
            with self.assertRaisesRegex(OSError, "monitor unavailable"):
                _run_monitored_posix([sys.executable, "-c", "import time; time.sleep(30)"],
                                     os.environ, 10, 256 * 1024**2)
        self.assertLess(workers[0].returncode, 0)

    def test_darwin_worker_does_not_apply_unsupported_address_space_limit(self):
        code = """
import json, resource
from unittest.mock import patch
from h5reclaim.native_worker import _apply_memory_limit
before = resource.getrlimit(resource.RLIMIT_AS)
with patch('sys.platform', 'darwin'):
    applied = _apply_memory_limit(536870912)
print(json.dumps([applied, list(before), list(resource.getrlimit(resource.RLIMIT_AS))]))
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, check=True, timeout=10)
        applied, before, after = json.loads(result.stdout)
        self.assertIsNone(applied)
        self.assertEqual(before, after)
        with patch("h5reclaim.worker_limits.sys.platform", "darwin"):
            report = memory_budget_record(536870912, None)
        self.assertIsNone(report["address_space_cap_bytes"])
        self.assertEqual(report["resident_memory_limit_bytes"], 536870912)
        self.assertEqual(report["memory_enforcement"], "process_group_resident_monitor")


if __name__ == "__main__":
    unittest.main()
