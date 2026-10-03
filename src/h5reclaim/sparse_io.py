"""Enumerate filesystem allocations without inferring holes from file content."""

from __future__ import annotations

import ctypes
import errno
import os
from functools import lru_cache


class _AllocatedRange(ctypes.Structure):
    _fields_ = [("offset", ctypes.c_int64), ("length", ctypes.c_int64)]


class _EndOfFileInfo(ctypes.Structure):
    _fields_ = [("end_of_file", ctypes.c_int64)]


_QUERY_ALLOCATED_RANGES = (9 << 16) | (1 << 14) | (51 << 2) | 3
_SET_SPARSE = (9 << 16) | (49 << 2)
_MORE_DATA = 234
_RANGES_PER_QUERY = 256


@lru_cache(maxsize=1)
def _device_io_control():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.DeviceIoControl
    function.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                         wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    function.restype = wintypes.BOOL
    return function


def _device_io(fd, control, request, output):
    import msvcrt
    from ctypes import wintypes
    returned = wintypes.DWORD()
    ok = _device_io_control()(
        msvcrt.get_osfhandle(fd), control,
        ctypes.byref(request) if request is not None else None,
        ctypes.sizeof(request) if request is not None else 0,
        ctypes.byref(output) if output is not None else None,
        ctypes.sizeof(output) if output is not None else 0,
        ctypes.byref(returned), None)
    return bool(ok), ctypes.get_last_error() if not ok else 0, returned.value


def _io_error(error, action):
    # Valid queries on FAT/exFAT and other unsupported filesystems may return
    # these codes. Other failures must not be interpreted as sparse holes.
    failure = OSError(errno.ENOTSUP if error in (1, 50, 87) else errno.EIO,
                      f"Windows {action} failed (error {error})")
    failure.winerror = error
    return failure


def prepare_sparse_file(fd: int) -> None:
    """Mark a private writable target sparse before extending its length."""
    if os.name == "nt":
        ok, error, _ = _device_io(fd, _SET_SPARSE, None, None)
        if not ok:
            raise _io_error(error, "sparse-file setup")


@lru_cache(maxsize=1)
def _set_file_information():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.SetFileInformationByHandle
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    function.restype = wintypes.BOOL
    return function


def truncate_sparse_file(fd: int, size: int) -> None:
    """Resize a private sparse file without writing zeros into its holes.

    Flush any buffered writes and call prepare_sparse_file before extending.
    Windows CRT truncation writes zeros on extension, allocating sparse holes;
    FileEndOfFileInfo changes the length without moving the file position.
    """
    if type(size) is not int or not 0 <= size < 1 << 63:
        raise ValueError("sparse file size must be a nonnegative signed 64-bit integer")
    if os.name != "nt":
        os.ftruncate(fd, size)
        return
    import msvcrt
    info = _EndOfFileInfo(size)
    if not _set_file_information()(msvcrt.get_osfhandle(fd), 6,
                                   ctypes.byref(info), ctypes.sizeof(info)):
        raise _io_error(ctypes.get_last_error(), "sparse-file resize")


def _windows_extents(fd: int, size: int):
    cursor = 0
    while cursor < size:
        request = _AllocatedRange(cursor, size - cursor)
        ranges = (_AllocatedRange * _RANGES_PER_QUERY)()
        ok, error, returned = _device_io(fd, _QUERY_ALLOCATED_RANGES, request, ranges)
        if not ok and error != _MORE_DATA:
            raise _io_error(error, "allocated-range query")
        if returned > ctypes.sizeof(ranges) or returned % ctypes.sizeof(_AllocatedRange):
            raise OSError(errno.EIO, "Windows returned malformed allocated ranges")
        count = returned // ctypes.sizeof(_AllocatedRange)
        end = cursor
        for index in range(count):
            item = ranges[index]
            # NTFS may round the request to allocation boundaries. Intersect
            # each returned allocation with the still-unqueried file range.
            start, stop = max(cursor, item.offset), min(size, item.offset + item.length)
            if item.offset < 0 or item.length <= 0 or start < end or stop <= start:
                raise OSError(errno.EIO, "Windows returned invalid allocated ranges")
            yield start, stop
            end = stop
        if ok:
            return
        if end <= cursor:
            raise OSError(errno.EIO, "Windows allocated-range query made no progress")
        cursor = end


def has_sparse_extents() -> bool:
    return os.name == "nt" or (hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE"))


def sparse_extents(fd: int, size: int):
    """Yield ordered data ranges, preserving a buffered reader's position.

    Unsupported filesystems raise ENOTSUP/EINVAL/ENOSYS before the first
    range. Callers can then apply their bounded dense fallback.
    """
    if os.name == "nt":
        yield from _windows_extents(fd, size)
        return
    if not has_sparse_extents():
        raise OSError(errno.ENOTSUP, "sparse allocation enumeration is unavailable")
    cursor = 0
    while cursor < size:
        saved = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            try:
                start = os.lseek(fd, cursor, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    return
                raise
            if start == size:
                return
            end = min(size, os.lseek(fd, start, os.SEEK_HOLE))
        finally:
            os.lseek(fd, saved, os.SEEK_SET)
        if not cursor <= start < end <= size:
            raise OSError(errno.EIO, "filesystem returned an invalid sparse allocation")
        yield start, end
        cursor = end
