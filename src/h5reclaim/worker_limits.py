"""Launch native HDF5 workers with an OS resource boundary.

POSIX workers apply RLIMIT_AS in their own process. On Windows, the parent
creates the worker suspended, attaches it to a Job Object, and only then
resumes it. The Job limits committed memory and owns the process tree. This
does not make the HDF5 parser a security sandbox.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence


def run_worker(command: Sequence[str], *, env: Mapping[str, str],
               timeout_seconds: float, memory_bytes: int) -> subprocess.CompletedProcess[bytes]:
    """Run a worker without inherited input/output and bound its lifetime."""
    if os.name != "nt":
        return subprocess.run(command, env=dict(env), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False, timeout=timeout_seconds)
    return _run_windows_job(command, env, timeout_seconds, memory_bytes)


def _run_windows_job(command: Sequence[str], env: Mapping[str, str],
                     timeout_seconds: float, memory_bytes: int) -> subprocess.CompletedProcess[bytes]:
    # Imported only on Windows. ctypes.WinDLL is not present on POSIX.
    import ctypes
    import math
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(field, ctypes.c_ulonglong) for field in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                    ("bInheritHandle", wintypes.BOOL)]

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(SecurityAttributes), wintypes.DWORD,
                                   wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID,
                                      wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.LPCWSTR,
                                      ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)]
    kernel.CreateProcessW.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def checked(ok: object, action: str) -> None:
        if not ok:
            raise OSError(ctypes.get_last_error(), f"Windows {action} failed: {ctypes.WinError()}")

    if (not command or not 0 < timeout_seconds <= 24 * 3600
            or not 0 < memory_bytes <= 16 * 1024**3):
        raise ValueError("invalid Windows worker limits")
    # A double-NUL-terminated UTF-16 environment block is required.
    if any("\0" in key or "\0" in value or "=" in key for key, value in env.items()):
        raise ValueError("invalid worker environment")
    environment = ctypes.create_unicode_buffer(
        "\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper()))
        + "\0\0")
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command)))
    job = kernel.CreateJobObjectW(None, None)
    checked(job, "CreateJobObjectW")
    null_device = None
    process = ProcessInfo()
    assigned = False
    try:
        limits = ExtendedLimit()
        # PROCESS_MEMORY caps a worker; JOB_MEMORY caps it and any descendants.
        # Closing the Job also kills descendants after an ordinary worker exit.
        limits.BasicLimitInformation.LimitFlags = 0x100 | 0x200 | 0x2000
        limits.ProcessMemoryLimit = memory_bytes
        limits.JobMemoryLimit = memory_bytes
        checked(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)),
                "SetInformationJobObject")
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, True)
        null_device = kernel.CreateFileW("NUL", 0x80000000 | 0x40000000, 3,
                                         ctypes.byref(attrs), 3, 0x80, None)
        if null_device == ctypes.c_void_p(-1).value:
            checked(False, "CreateFileW(NUL)")
        startup = StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = 0x100  # STARTF_USESTDHANDLES
        startup.hStdInput = startup.hStdOutput = startup.hStdError = null_device
        checked(kernel.CreateProcessW(None, cmdline, None, None, True,
                                      0x4 | 0x8000000 | 0x400,  # suspended, no window, Unicode env
                                      environment, None, ctypes.byref(startup), ctypes.byref(process)),
                "CreateProcessW")
        checked(kernel.AssignProcessToJobObject(job, process.hProcess), "AssignProcessToJobObject")
        assigned = True
        if kernel.ResumeThread(process.hThread) == 0xFFFFFFFF:
            checked(False, "ResumeThread")
        state = kernel.WaitForSingleObject(process.hProcess, math.ceil(timeout_seconds * 1000))
        if state == 0x102:  # WAIT_TIMEOUT: kill the whole Job before returning.
            checked(kernel.TerminateJobObject(job, 1), "TerminateJobObject")
            kernel.WaitForSingleObject(process.hProcess, 5000)
            raise subprocess.TimeoutExpired(command, timeout_seconds)
        if state != 0:  # WAIT_OBJECT_0
            checked(False, "WaitForSingleObject")
        exit_code = wintypes.DWORD()
        checked(kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)),
                "GetExitCodeProcess")
        return subprocess.CompletedProcess(command, exit_code.value)
    finally:
        if process.hProcess and not assigned:
            kernel.TerminateProcess(process.hProcess, 1)
            kernel.WaitForSingleObject(process.hProcess, 5000)
        if process.hThread:
            kernel.CloseHandle(process.hThread)
        if process.hProcess:
            kernel.CloseHandle(process.hProcess)
        if null_device is not None and null_device != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(null_device)
        kernel.CloseHandle(job)
