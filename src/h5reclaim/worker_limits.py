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

    class StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", wintypes.LPVOID)]

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
                                      wintypes.LPVOID, ctypes.POINTER(ProcessInfo)]
    kernel.CreateProcessW.restype = wintypes.BOOL
    kernel.InitializeProcThreadAttributeList.argtypes = [wintypes.LPVOID, wintypes.DWORD,
                                                          wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel.UpdateProcThreadAttribute.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.c_size_t,
                                                 wintypes.LPVOID, ctypes.c_size_t, wintypes.LPVOID,
                                                 ctypes.POINTER(ctypes.c_size_t)]
    kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    kernel.DeleteProcThreadAttributeList.restype = None
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
    kernel.GetEnvironmentStringsW.argtypes = []
    kernel.GetEnvironmentStringsW.restype = wintypes.LPVOID
    kernel.FreeEnvironmentStringsW.argtypes = [wintypes.LPVOID]
    kernel.FreeEnvironmentStringsW.restype = wintypes.BOOL

    def checked(ok: object, action: str) -> None:
        if not ok:
            raise OSError(ctypes.get_last_error(), f"Windows {action} failed: {ctypes.WinError()}")

    if (not command or not 0 < timeout_seconds <= (2**32 - 2) / 1000
            or not 0 < memory_bytes <= ctypes.c_size_t(-1).value):
        raise ValueError("invalid Windows worker limits")
    if not all(isinstance(arg, str) and "\0" not in arg for arg in command):
        raise ValueError("invalid Windows worker command")
    # A double-NUL-terminated UTF-16 environment block is required.
    if any("\0" in key or "\0" in value or "=" in key for key, value in env.items()):
        raise ValueError("invalid worker environment")
    # Python's os.environ excludes Windows' hidden =C: drive-current-directory
    # entries. Keep them when supplying a custom environment to CreateProcessW.
    drive_entries: list[str] = []
    original_environment = kernel.GetEnvironmentStringsW()
    checked(original_environment, "GetEnvironmentStringsW")
    try:
        chars = ctypes.cast(original_environment, ctypes.POINTER(ctypes.c_wchar))
        entry_chars: list[str] = []
        for offset in range(1024 * 1024):
            char = chars[offset]
            if char == "\0":
                if not entry_chars:
                    break
                entry = "".join(entry_chars)
                if (len(entry) >= 4 and entry[0] == "=" and entry[1].isalpha()
                        and entry[2:4] == ":="):
                    drive_entries.append(entry)
                entry_chars.clear()
            else:
                entry_chars.append(char)
        else:
            raise OSError("Windows environment block exceeds the bounded scan")
    finally:
        kernel.FreeEnvironmentStringsW(original_environment)
    environment = ctypes.create_unicode_buffer(
        "\0".join(sorted(drive_entries, key=str.upper) + [
            f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())])
        + "\0\0")
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command)))
    job = kernel.CreateJobObjectW(None, None)
    checked(job, "CreateJobObjectW")
    null_device = None
    process = ProcessInfo()
    assigned = False
    attribute_list = None
    attributes_initialized = False
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
        attribute_bytes = ctypes.c_size_t()
        # First call obtains the required buffer length (ERROR_INSUFFICIENT_BUFFER).
        kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attribute_bytes))
        if not 0 < attribute_bytes.value <= 1024 * 1024:
            raise OSError("Windows worker handle-list allocation is invalid")
        attribute_list = ctypes.create_string_buffer(attribute_bytes.value)
        checked(kernel.InitializeProcThreadAttributeList(attribute_list, 1, 0,
                                                         ctypes.byref(attribute_bytes)),
                "InitializeProcThreadAttributeList")
        attributes_initialized = True
        inherited = (wintypes.HANDLE * 1)(null_device)
        checked(kernel.UpdateProcThreadAttribute(attribute_list, 0, 0x20002, inherited,
                                                 ctypes.sizeof(inherited), None, None),
                "UpdateProcThreadAttribute")
        startup = StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = null_device
        startup.StartupInfo.hStdOutput = null_device
        startup.StartupInfo.hStdError = null_device
        startup.lpAttributeList = ctypes.cast(attribute_list, wintypes.LPVOID)
        checked(kernel.CreateProcessW(command[0], cmdline, None, None, True,
                                      0x4 | 0x8000000 | 0x400 | 0x80000,
                                      # suspended, no window, Unicode env, explicit handle list
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
        if attributes_initialized:
            kernel.DeleteProcThreadAttributeList(attribute_list)
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
