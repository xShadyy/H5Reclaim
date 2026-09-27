"""Real Windows Job Object checks, exercised by the Windows CI runner."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from h5reclaim.worker_limits import run_worker


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
